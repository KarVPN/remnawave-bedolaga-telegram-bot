"""Persistent target selection, counters and conservative restart semantics."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.cabinet.schemas.user_bulk_operations import UserBulkFilters, UserBulkPreview
from app.config import settings
from app.database.crud.user import get_users_count
from app.database.models import Subscription, Tariff, User, UserBulkOperation, UserBulkTarget
from app.services import user_bulk_operations as service
from app.services.user_bulk_filters import day_bounds
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    UserBulkOperation.__table__,
    UserBulkTarget.__table__,
)


async def seed(db):
    users = [User(telegram_id=i + 100, username=f'user{i}', status='active') for i in range(4)]
    db.add_all(users)
    await db.flush()
    start = datetime(2026, 10, 10, 21, tzinfo=UTC)
    for i, owner in enumerate([users[0], users[0], users[1], users[2]]):
        db.add(
            Subscription(
                user_id=owner.id,
                start_date=start,
                end_date=start + timedelta(days=2),
                status='expired',
                traffic_limit_gb=0 if i == 2 else 100,
                remnawave_id=i + 1000,
                remnawave_short_id=f'bulk{i}',
            )
        )
    await db.commit()
    return users


def request(**kwargs):
    return UserBulkPreview(request_key=uuid4(), action='add_traffic', traffic_gb=50, **kwargs)


async def test_snapshot_unique_users_skips_and_idempotency(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        req = request(filters=UserBulkFilters())
        op = await service.preview(db, 7, req)
        assert await service.preview(db, 7, req) == op
        result = await service.summary(db, op)
        assert result['total'] == 4
        assert result['subscriptions'] == 4
        assert result['affected_users'] == 2
        assert result['skipped_users'] == 2
        assert result['items'] == {'pending': 3, 'skipped': 2}
        db.add(User(telegram_id=999, status='active'))
        await db.commit()
        assert (await service.summary(db, op))['total'] == 4
        with pytest.raises(HTTPException) as exc:
            await service.preview(db, 7, req.model_copy(update={'traffic_gb': 10}))
        assert exc.value.status_code == 409


async def test_date_sql_boundaries_count_snapshot_and_multisub(monkeypatch):
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Moscow')
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        day = date(2026, 10, 11)
        assert day_bounds(day) == (datetime(2026, 10, 10, 21, tzinfo=UTC), datetime(2026, 10, 11, 21, tzinfo=UTC))
        assert await get_users_count(db, subscription_on_date=day) == 3
        op = await service.preview(db, 7, request(filters=UserBulkFilters(subscription_on_date=day)))
        result = await service.summary(db, op)
        assert result['total'] == 3
        assert result['subscriptions'] == 4  # user0 appears once, two explicit targets
        assert await get_users_count(db, subscription_on_date=date(2026, 10, 13)) == 0
        assert await get_users_count(db, subscription_on_date=day, search=users[0].username) == 1


def test_dst_calendar_day(monkeypatch):
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Berlin')
    start, end = day_bounds(date(2026, 3, 29))
    assert end - start == timedelta(hours=23)


async def test_copy_selection_reclassifies_without_expanding_membership(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        op = await service.preview(db, 7, request(filters=UserBulkFilters()))
        db.add(User(telegram_id=999, status='active'))
        await db.commit()
        copied = await service.preview(
            db, 7, UserBulkPreview(request_key=uuid4(), action='extend_subscription', days=5, selection_id=op)
        )
        result = await service.summary(db, copied)
        assert result['total'] == 4
        assert result['affected_users'] == 3  # unlimited is eligible for extension


async def test_restart_never_repeats_success_or_uncertain(monkeypatch):
    import app.cabinet.routes.admin_bulk_actions as handlers

    monkeypatch.setattr(settings, 'USERS_BULK_ITEM_DELAY', 0.1)
    monkeypatch.setattr(settings, 'USERS_BULK_BATCH_SIZE', 1)
    execute = AsyncMock(return_value=SimpleNamespace(success=True, message='OK'))
    monkeypatch.setattr(handlers, '_execute_for_subscription', execute)
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        op = await service.preview(db, 7, request(user_ids=[users[0].id]))
        targets = (
            await db.scalars(
                select(UserBulkTarget).where(UserBulkTarget.operation_id == op).order_by(UserBulkTarget.id)
            )
        ).all()
        targets[0].state = 'processing'  # crash after sending the external request
        operation = await service.get_operation(db, op)
        operation.state = 'running'
        await db.commit()
        await service.run_batch(db, op)
        assert execute.await_count == 1
        assert execute.await_args.args[1] == targets[1].subscription_id
        result = await service.summary(db, op)
        assert result['items'] == {'success': 1, 'uncertain': 1}
        assert result['state'] == 'needs_review'
        assert result['processed'] == 1
        await service.run_batch(db, op)
        assert execute.await_count == 1


async def test_cancel_preserves_current_item_and_success(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        op = await service.preview(db, 7, request(filters=UserBulkFilters()))
        targets = (
            await db.scalars(
                select(UserBulkTarget).where(UserBulkTarget.operation_id == op, UserBulkTarget.state == 'pending')
            )
        ).all()
        targets[0].state = 'success'
        targets[1].state = 'processing'
        operation = await service.get_operation(db, op)
        operation.state = 'running'
        await db.commit()
        await service.cancel(db, op)
        await db.refresh(targets[0])
        await db.refresh(targets[1])
        assert targets[0].state == 'success'
        assert targets[1].state == 'processing'
        assert (
            await db.scalar(
                select(func.count())
                .select_from(UserBulkTarget)
                .where(UserBulkTarget.operation_id == op, UserBulkTarget.state == 'pending')
            )
            == 0
        )


async def test_failure_stops_batch_and_is_never_retried(monkeypatch):
    import app.cabinet.routes.admin_bulk_actions as handlers

    execute = AsyncMock(return_value=SimpleNamespace(success=False, message='API timeout'))
    monkeypatch.setattr(handlers, '_execute_for_subscription', execute)
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        op = await service.preview(db, 7, request(user_ids=[users[0].id]))
        await service.run_batch(db, op)
        result = await service.summary(db, op)
        assert execute.await_count == 1
        assert result['state'] == 'paused'
        assert result['items'] == {'pending': 1, 'uncertain': 1}
        await service.run_batch(db, op)
        assert execute.await_count == 2
        assert execute.await_args_list[0].args[1] != execute.await_args_list[1].args[1]


@pytest.mark.parametrize(
    'payload',
    [
        {'action': 'add_traffic', 'traffic_gb': 20, 'user_ids': [1]},
        {'action': 'deduct_days', 'days': -5, 'user_ids': [1]},
        {'action': 'extend_subscription', 'days': 5, 'user_ids': [1], 'filters': {}},
    ],
)
def test_reject_ambiguous_confirmation(payload):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        UserBulkPreview(request_key=uuid4(), **payload)


@pytest.mark.parametrize('package', [10, 50, 100])
async def test_traffic_packages_reuse_existing_handler(monkeypatch, package):
    import app.cabinet.routes.admin_bulk_actions as handlers
    from app.cabinet.schemas.bulk_actions import BulkActionParams
    from app.services.bulk_execution_context import durable_execution

    sub = SimpleNamespace(id=41, status='expired', remnawave_id=112)
    owner = SimpleNamespace(id=7, username='owner')
    add = AsyncMock()
    reactivate = AsyncMock()
    sync = AsyncMock(return_value={})
    monkeypatch.setattr(handlers, 'add_subscription_traffic', add)
    monkeypatch.setattr(handlers, 'reactivate_subscription', reactivate)
    monkeypatch.setattr(handlers, '_legacy_sync_subscription_to_panel', sync)
    token = durable_execution.set(True)
    try:
        result = await handlers._do_add_traffic(AsyncMock(), owner, BulkActionParams(traffic_gb=package), False, sub)
    finally:
        durable_execution.reset(token)
    assert result.success
    assert add.await_args.args[2] == package
    reactivate.assert_awaited_once()
    assert sync.await_args.kwargs['pinned_subscription_identity'] is True


async def test_deduction_expires_and_syncs_explicit_subscription(monkeypatch):
    import app.cabinet.routes.admin_bulk_actions as handlers
    from app.cabinet.schemas.bulk_actions import BulkActionParams

    sub = SimpleNamespace(id=8, status='active', end_date=datetime.now(UTC) + timedelta(days=2))

    async def extend(db, target, days):
        target.end_date += timedelta(days=days)

    change = AsyncMock(side_effect=extend)
    sync = AsyncMock()
    monkeypatch.setattr(handlers, 'extend_subscription', change)
    monkeypatch.setattr(handlers, '_sync_subscription_to_panel', sync)
    owner = SimpleNamespace(id=7)
    result = await handlers._do_deduct_days(AsyncMock(), owner, BulkActionParams(days=5), False, sub)
    assert result.success
    assert sub.status == 'expired'
    assert change.await_args.args[2] == -5
    assert sync.await_args.args[2] is sub


async def test_strict_panel_error_cannot_be_reported_as_success(monkeypatch):
    import app.cabinet.routes.admin_bulk_actions as handlers
    from app.services.bulk_execution_context import durable_execution

    monkeypatch.setattr(handlers, '_legacy_sync_subscription_to_panel', AsyncMock(return_value={'error': 'timeout'}))
    token = durable_execution.set(True)
    try:
        with pytest.raises(RuntimeError, match='verification'):
            await handlers._sync_subscription_to_panel(AsyncMock(), SimpleNamespace(id=1), SimpleNamespace(id=2))
    finally:
        durable_execution.reset(token)


async def test_manual_selection_respects_date_targets(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        req = request(user_ids=[users[0].id], target_filters=UserBulkFilters(subscription_on_date=date(2026, 10, 20)))
        op = await service.preview(db, 7, req)
        result = await service.summary(db, op)
        assert result['total'] == 0


async def test_permission_dependencies_cover_every_route():
    from app.cabinet.routes.user_bulk_operations import router

    for route in router.routes:
        dependencies = list(route.dependant.dependencies)
        for dependency in dependencies:
            dependencies.extend(dependency.dependencies)
        permissions = []
        for dep in dependencies:
            if dep.call and getattr(dep.call, '__closure__', None):
                for cell in dep.call.__closure__:
                    value = cell.cell_contents
                    if isinstance(value, tuple):
                        permissions.extend(x for x in value if isinstance(x, str))
        assert 'users:read' in permissions, route.path
        assert 'bulk_actions:execute' in permissions, route.path


async def test_list_date_filter_returns_matching_subscription_ids(monkeypatch):
    from app.cabinet.routes import admin_users
    from app.services import panel_online
    from tests.cabinet.test_admin_users_list_filters import _list

    monkeypatch.setattr(panel_online, 'get_online_snapshot', AsyncMock(return_value=None))
    monkeypatch.setattr(admin_users, 'get_users_spending_stats', AsyncMock(return_value={}))
    monkeypatch.setattr(settings, 'TIMEZONE', 'Europe/Moscow')
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)
        result = await _list(db, subscription_on_date=date(2026, 10, 11))
        assert result.total == len(result.users) == 3
        assert sorted(len(item.estimated_date_subscription_ids) for item in result.users) == [1, 1, 2]


async def test_expiration_snapshot_targets_only_matching_subscriptions_without_tariff(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        subs = (
            await db.scalars(select(Subscription).where(Subscription.user_id == users[0].id).order_by(Subscription.id))
        ).all()
        for index, sub in enumerate(subs):
            sub.status = 'active'
            sub.is_trial = False
            sub.end_date = datetime.now(UTC) + timedelta(days=3 if index == 0 else 30)
        await db.commit()
        assert await get_users_count(db, expires_within_days=7) == 1
        op = await service.preview(db, 7, request(filters=UserBulkFilters(expires_within_days=7)))
        result = await service.summary(db, op)
        assert result['total'] == 1
        assert result['subscriptions'] == 1
        assert result['items']['pending'] == 1
