"""Real PostgreSQL coverage: advisory locks survive commits, crash and cancel races."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cabinet.schemas.user_bulk_operations import UserBulkFilters, UserBulkPreview
from app.config import settings
from app.database.models import Subscription, User, UserBulkNotification, UserBulkOperation, UserBulkTarget
from app.services import user_bulk_operations as service
from tests.fixtures.postgres_db import postgres_engine


TABLES = (UserBulkTarget.__table__, UserBulkOperation.__table__, Subscription.__table__, User.__table__)


async def make_operation(db):
    owner = User(telegram_id=55391, status='active')
    db.add(owner)
    await db.flush()
    for i in range(2):
        db.add(
            Subscription(
                user_id=owner.id,
                status='active',
                start_date=datetime.now(UTC),
                end_date=datetime.now(UTC) + timedelta(days=10),
                remnawave_id=500 + i,
                remnawave_short_id=f'pgbulk{i}',
                traffic_limit_gb=100,
            )
        )
    await db.commit()
    return await service.preview(
        db,
        owner.id,
        UserBulkPreview(request_key=uuid4(), action='add_traffic', traffic_gb=50, filters=UserBulkFilters()),
    )


@pytest.mark.postgres
async def test_lock_excludes_second_worker_across_commits_and_releases(postgres_database):
    async with postgres_engine(postgres_database, TABLES) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with service.execution_lock(engine) as first:
            assert first
            async with maker() as db:
                await db.commit()  # handler commits cannot release the dedicated lock
            async with service.execution_lock(engine) as second:
                assert not second
        async with service.execution_lock(engine) as acquired:
            assert acquired


@pytest.mark.postgres
async def test_crash_recovery_and_cancellation_during_inflight_target(postgres_database, monkeypatch):
    import app.cabinet.routes.admin_bulk_actions as handlers

    monkeypatch.setattr(settings, 'USERS_BULK_ITEM_DELAY', 0.1)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def apply(db, sid, *args):
        started.set()
        await finish.wait()
        return SimpleNamespace(success=True, message='Applied')

    executor = AsyncMock(side_effect=apply)
    monkeypatch.setattr(handlers, '_execute_for_subscription', executor)
    async with postgres_engine(postgres_database, TABLES) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            op = await make_operation(db)

        async def worker():
            async with service.execution_lock(engine) as acquired:
                assert acquired
                async with maker() as db:
                    await service.run_batch(db, op)

        task = asyncio.create_task(worker())
        try:
            await asyncio.wait_for(started.wait(), 5)
            async with maker() as observer:
                progress = await service.summary(observer, op)
                assert progress['items']['processing'] == 1
                assert progress['processed'] == 0  # owner waits for all their subscriptions
            async with maker() as canceller:
                await asyncio.wait_for(service.cancel(canceller, op), 5)
            finish.set()
            await asyncio.wait_for(task, 5)
        finally:
            finish.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with maker() as db:
            result = await service.summary(db, op)
            assert result['state'] == 'cancelled'
            assert result['items'] == {'success': 1, 'cancelled': 1}
            assert executor.await_count == 1
            await service.run_batch(db, op)
            assert executor.await_count == 1


@pytest.mark.postgres
async def test_snapshot_thousands_and_resume_in_new_session(postgres_database, monkeypatch):
    import app.cabinet.routes.admin_bulk_actions as handlers

    monkeypatch.setattr(settings, 'USERS_BULK_ITEM_DELAY', 0.1)
    monkeypatch.setattr(settings, 'USERS_BULK_BATCH_SIZE', 1)
    executor = AsyncMock(return_value=SimpleNamespace(success=True, message='Applied'))
    monkeypatch.setattr(handlers, '_execute_for_subscription', executor)
    async with postgres_engine(postgres_database, TABLES) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            op = await make_operation(db)
            targets = (
                await db.scalars(
                    select(UserBulkTarget).where(UserBulkTarget.operation_id == op).order_by(UserBulkTarget.id)
                )
            ).all()
            targets[0].state = 'processing'
            operation = await service.get_operation(db, op)
            operation.state = 'running'
            await db.commit()
        # A new session simulates a restarted app: processing becomes uncertain.
        async with service.execution_lock(engine) as acquired:
            assert acquired
            async with maker() as restarted:
                await service.run_batch(restarted, op)
                result = await service.summary(restarted, op)
                assert result['state'] == 'needs_review'
                assert result['items'] == {'success': 1, 'uncertain': 1}
                assert executor.await_count == 1
        async with maker() as db:
            db.add_all(User(telegram_id=70000 + i, status='active') for i in range(3000))
            await db.commit()
            large_op = await service.preview(
                db,
                7,
                UserBulkPreview(request_key=uuid4(), action='add_traffic', traffic_gb=10, filters=UserBulkFilters()),
            )
            result = await service.summary(db, large_op)
            assert result['total'] == 3001
            assert result['items']['skipped'] == 3000
            assert len(db.identity_map) < 10  # selection inserts from SQL, no user-card materialization


@pytest.mark.postgres
async def test_commit_before_api_failure_never_reapplies_delta(postgres_database, monkeypatch):
    import app.cabinet.routes.admin_bulk_actions as handlers

    calls = []

    async def commit_then_timeout(db, subscription_id, *args):
        sub = await db.get(Subscription, subscription_id)
        sub.traffic_limit_gb += 50
        await db.commit()  # mirrors existing CRUD commit before panel sync
        calls.append(subscription_id)
        if len(calls) == 1:
            await db.rollback()
            return SimpleNamespace(success=False, message='Panel timeout after commit')
        return SimpleNamespace(success=True, message='OK')

    monkeypatch.setattr(handlers, '_execute_for_subscription', commit_then_timeout)
    monkeypatch.setattr(settings, 'USERS_BULK_ITEM_DELAY', 0.1)
    async with postgres_engine(postgres_database, TABLES) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            op = await make_operation(db)
            await service.run_batch(db, op)
        async with maker() as resumed:
            await service.run_batch(resumed, op)
            result = await service.summary(resumed, op)
            assert result['items'] == {'success': 1, 'uncertain': 1}
            assert len(calls) == len(set(calls)) == 2
            limits = (await resumed.scalars(select(Subscription.traffic_limit_gb))).all()
            assert limits == [150, 150]


@pytest.mark.postgres
async def test_migration_round_trip_matches_runtime_models(postgres_database):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    migration = importlib.import_module('migrations.alembic.versions.0132_users_bulk_operations')
    receipts = importlib.import_module('migrations.alembic.versions.0133_user_bulk_notifications')
    async with postgres_engine(postgres_database, TABLES) as engine:
        async with engine.begin() as conn:

            def migrate(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    receipts.downgrade()
                    migration.downgrade()
                    migration.upgrade()
                    receipts.upgrade()
                inspector = inspect(sync)
                assert {c['name'] for c in inspector.get_columns('user_bulk_notifications')} == set(
                    UserBulkNotification.__table__.columns.keys()
                )
                assert {c['name'] for c in inspector.get_columns('user_bulk_targets')} == set(
                    UserBulkTarget.__table__.columns.keys()
                )
                assert {c['name'] for c in inspector.get_columns('user_bulk_operations')} == set(
                    UserBulkOperation.__table__.columns.keys()
                )

            await conn.run_sync(migrate)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            op = await make_operation(db)
            assert (await service.summary(db, op))['items']['pending'] == 2
