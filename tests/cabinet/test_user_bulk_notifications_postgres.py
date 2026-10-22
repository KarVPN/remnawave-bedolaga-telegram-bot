"""Cross-session guarantees for receipts, cancellation and send crash recovery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database.models import UserBulkNotification, UserBulkTarget
from app.services import user_bulk_notifications as delivery, user_bulk_operations as service
from tests.cabinet.test_user_bulk_notifications import TABLES, request
from tests.cabinet.test_user_bulk_operations import seed
from tests.fixtures.postgres_db import postgres_engine


@pytest.mark.postgres
async def test_receipt_claim_visible_before_send_and_cancel_skips_rest(postgres_database, monkeypatch):
    monkeypatch.setattr(settings, 'BOT_TOKEN', '123:test')
    monkeypatch.setattr(settings, 'USERS_BULK_ITEM_DELAY', 0)
    started, finish = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        started.set()
        await finish.wait()
        return SimpleNamespace(message_id=12)

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send), session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(delivery, 'create_bot', lambda: bot)
    async with postgres_engine(postgres_database, TABLES) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            users = await seed(db)
            op = await service.preview(db, 7, request(user_ids=[users[0].id, users[1].id]))
            await db.execute(update(UserBulkTarget).where(UserBulkTarget.operation_id == op).values(state='success'))
            await db.commit()

        async def worker():
            async with service.execution_lock(engine) as acquired:
                assert acquired
                async with maker() as db:
                    await delivery.deliver_batch(db, op)

        task = asyncio.create_task(worker())
        try:
            await asyncio.wait_for(started.wait(), 10)
            async with maker() as observer:
                assert (await delivery.counts(observer, op))['sending'] == 1
            async with maker() as canceller:
                await asyncio.wait_for(service.cancel(canceller, op), 5)
            finish.set()
            await asyncio.wait_for(task, 10)
        finally:
            finish.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with maker() as db:
            result = await delivery.counts(db, op)
            assert result['sent'] == result['skipped'] == 1
            await service.run_batch(db, op)
            assert bot.send_message.await_count == 1


@pytest.mark.postgres
async def test_new_session_recovers_sending_without_duplicate(postgres_database, monkeypatch):
    bot = SimpleNamespace(send_message=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(delivery, 'create_bot', lambda: bot)
    async with postgres_engine(postgres_database, TABLES) as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with maker() as db:
            users = await seed(db)
            op = await service.preview(db, 7, request(user_ids=[users[1].id]))
            await db.execute(update(UserBulkTarget).where(UserBulkTarget.operation_id == op).values(state='success'))
            await db.execute(
                update(UserBulkNotification).where(UserBulkNotification.operation_id == op).values(state='sending')
            )
            operation = await service.get_operation(db, op)
            operation.state = 'running'
            await db.commit()
        async with service.execution_lock(engine) as acquired:
            assert acquired
            async with maker() as restarted:
                await service.run_batch(restarted, op)
                result = await service.summary(restarted, op)
                assert result['state'] == 'needs_review'
                assert result['notifications']['uncertain'] == 1
                receipt = await restarted.scalar(
                    select(UserBulkNotification).where(UserBulkNotification.operation_id == op)
                )
                assert receipt.message is not None
        bot.send_message.assert_not_awaited()


@pytest.mark.postgres
async def test_notification_migration_upgrades_existing_journal_and_downgrades(postgres_database):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    path = Path(__file__).parents[2] / 'migrations/alembic/versions/0133_user_bulk_notifications.py'
    spec = importlib.util.spec_from_file_location('bulk_notification_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with postgres_engine(postgres_database) as engine:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await conn.execute(text('CREATE SCHEMA bulk_notification_migration_test'))
                await conn.execute(text('SET LOCAL search_path TO bulk_notification_migration_test'))
                await conn.execute(text('CREATE TABLE user_bulk_operations (id VARCHAR(36) PRIMARY KEY)'))

                def upgrade(sync_conn):
                    with Operations.context(MigrationContext.configure(sync_conn)):
                        migration.upgrade()
                    schema = inspect(sync_conn)
                    assert schema.has_table('user_bulk_notifications')
                    assert schema.get_unique_constraints('user_bulk_notifications')[0]['column_names'] == [
                        'operation_id',
                        'user_id',
                    ]

                await conn.run_sync(upgrade)

                def downgrade(sync_conn):
                    with Operations.context(MigrationContext.configure(sync_conn)):
                        migration.downgrade()
                    assert not inspect(sync_conn).has_table('user_bulk_notifications')
                    assert inspect(sync_conn).has_table('user_bulk_operations')

                await conn.run_sync(downgrade)
            finally:
                await transaction.rollback()
