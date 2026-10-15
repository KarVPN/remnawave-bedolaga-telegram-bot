"""Small PostgreSQL journal layered on the existing bulk handlers.

A request runs one bounded batch. The browser may request the next batch, but
PostgreSQL owns target membership, progress and recovery. No mutation is retried.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, case, func, insert, literal, select, text, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.crud.subscription_segments import segment_condition
from app.database.crud.user import _users_list_conditions
from app.database.models import Subscription, User, UserBulkOperation, UserBulkTarget, UserStatus
from app.services.bulk_execution_context import durable_execution
from app.services.user_bulk_filters import subscription_day_condition


# Session advisory lock, held on a dedicated connection across handler commits.
# One batch across all processes limits panel pressure and excludes concurrent resume.
LOCK_KEY = 728440155
TERMINAL = {'completed', 'cancelled', 'needs_review'}


@asynccontextmanager
async def execution_lock(engine):
    if engine.dialect.name != 'postgresql':
        raise HTTPException(503, 'Durable bulk actions require PostgreSQL')
    async with engine.connect() as conn:
        acquired = bool(await conn.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': LOCK_KEY}))
        try:
            yield acquired
        finally:
            if acquired:
                await conn.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LOCK_KEY})
                await conn.commit()


def selection_query(request):
    if request.selection_id is not None:
        return select(UserBulkTarget.user_id.label('id'), UserBulkTarget.subscription_id.label('id_1')).where(
            UserBulkTarget.operation_id == str(request.selection_id)
        )
    filters = request.filters or request.target_filters
    values = filters.model_dump(exclude_none=True) if filters else {}
    if 'status' in values:
        values['status'] = UserStatus(values['status'])
    conditions = _users_list_conditions(**values)
    if request.user_ids is not None:
        conditions.append(User.id.in_(set(request.user_ids)))
    join_conditions = [Subscription.user_id == User.id]
    if filters and filters.subscription_on_date:
        join_conditions.append(subscription_day_condition(filters.subscription_on_date))
    if filters and filters.subscription_status:
        join_conditions.append(segment_condition(filters.subscription_status, datetime.now(UTC)))
    if filters and filters.expires_within_days:
        # The shared user predicate picks users; only those expiring subscriptions
        # should be mutated when that filter is set.
        from datetime import timedelta

        from app.database.models import Tariff

        join_conditions.extend(
            [
                Subscription.status == 'active',
                Subscription.is_trial.is_not(True),
                Subscription.end_date >= datetime.now(UTC),
                Subscription.end_date <= datetime.now(UTC) + timedelta(days=filters.expires_within_days),
                (
                    Subscription.tariff_id.is_(None)
                    | ~and_(
                        Subscription.is_daily_paused.is_(False),
                        Subscription.tariff_id.in_(select(Tariff.id).where(Tariff.is_daily.is_(True))),
                    )
                ),
            ]
        )
    return select(User.id, Subscription.id).outerjoin(Subscription, and_(*join_conditions)).where(*conditions)


async def preview(db, admin_id, request):
    key = str(request.request_key)
    existing = await db.scalar(
        select(UserBulkOperation).where(UserBulkOperation.author_id == admin_id, UserBulkOperation.request_key == key)
    )
    params = {'days': request.days, 'traffic_gb': request.traffic_gb}
    # Store the selection too: a reused idempotency key cannot silently alter intent.
    intent = request.model_dump(mode='json', exclude={'request_key'})
    if existing:
        if existing.params['intent'] != intent:
            raise HTTPException(409, 'Request key already used with different parameters')
        return existing.id
    operation_id = str(uuid4())
    db.add(
        UserBulkOperation(
            id=operation_id,
            author_id=admin_id,
            request_key=key,
            action=request.action,
            params={**params, 'intent': intent},
        )
    )
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return await preview(db, admin_id, request)
    if request.selection_id is not None:
        await get_operation(db, str(request.selection_id))
    selected = selection_query(request).subquery()
    source = select(
        literal(operation_id),
        selected.c.id,
        selected.c.id_1,
        literal('pending'),
        literal(None),
        literal(datetime.now(UTC)),
    )
    await db.execute(
        insert(UserBulkTarget).from_select(
            ['operation_id', 'user_id', 'subscription_id', 'state', 'message', 'updated_at'], source
        )
    )
    # Classify skips using SQL, without loading user cards or target lists.
    await classify_skips(db, operation_id, request.action)
    await db.commit()
    return operation_id


async def classify_skips(db, operation_id, action, target_id=None):
    sub_ids = select(Subscription.id)
    checks = [
        (UserBulkTarget.subscription_id.is_(None), 'Нет подходящей подписки'),
        (~UserBulkTarget.subscription_id.in_(sub_ids), 'Подписка удалена'),
        (
            UserBulkTarget.subscription_id.in_(select(Subscription.id).where(Subscription.remnawave_id.is_(None))),
            'Нет закреплённой связи подписки с панелью',
        ),
    ]
    if action == 'add_traffic':
        checks.append(
            (
                UserBulkTarget.subscription_id.in_(select(Subscription.id).where(Subscription.traffic_limit_gb <= 0)),
                'Безлимитный трафик',
            )
        )
    reason = case(*checks, else_=None)
    target_condition = UserBulkTarget.id == target_id if target_id is not None else literal(True)
    await db.execute(
        update(UserBulkTarget)
        .where(
            UserBulkTarget.operation_id == operation_id,
            UserBulkTarget.state == 'pending',
            reason.is_not(None),
            target_condition,
        )
        .values(state='skipped', message=reason)
    )


async def get_operation(db, operation_id):
    operation = await db.get(UserBulkOperation, operation_id, populate_existing=True)
    if operation is None:
        raise HTTPException(404, 'Operation not found')
    return operation


async def recover(db, operation):
    """Only called with the global advisory lock held; no worker can still own it."""
    await db.execute(
        update(UserBulkTarget)
        .where(UserBulkTarget.operation_id == operation.id, UserBulkTarget.state == 'processing')
        .values(state='uncertain', message='Результат требует проверки после прерывания', updated_at=datetime.now(UTC))
    )
    if operation.state == 'running':
        pending = await db.scalar(
            select(func.count())
            .select_from(UserBulkTarget)
            .where(UserBulkTarget.operation_id == operation.id, UserBulkTarget.state == 'pending')
        )
        uncertain = await db.scalar(
            select(func.count())
            .select_from(UserBulkTarget)
            .where(UserBulkTarget.operation_id == operation.id, UserBulkTarget.state == 'uncertain')
        )
        operation.state = (
            'cancelled'
            if operation.cancel_requested
            else 'paused'
            if pending
            else 'needs_review'
            if uncertain
            else 'completed'
        )
        operation.updated_at = datetime.now(UTC)
    await db.commit()


async def summary(db, operation_id):
    operation = await get_operation(db, operation_id)
    by_user = (
        select(
            UserBulkTarget.user_id,
            func.count().label('items'),
            func.sum(case((UserBulkTarget.state.in_(['pending', 'processing']), 1), else_=0)).label('pending'),
            func.sum(case((UserBulkTarget.state == 'success', 1), else_=0)).label('success'),
            func.sum(case((UserBulkTarget.state == 'uncertain', 1), else_=0)).label('uncertain'),
            func.sum(case((UserBulkTarget.state == 'cancelled', 1), else_=0)).label('cancelled'),
        )
        .where(UserBulkTarget.operation_id == operation_id)
        .group_by(UserBulkTarget.user_id)
        .subquery()
    )
    row = (
        (
            await db.execute(
                select(
                    func.count().label('total'),
                    func.sum(
                        case(
                            (and_(by_user.c.pending == 0, by_user.c.cancelled > 0, by_user.c.uncertain == 0), 1),
                            else_=0,
                        )
                    ).label('cancelled'),
                    func.sum(case((by_user.c.pending == 0, 1), else_=0)).label('processed'),
                    func.sum(
                        case((and_(by_user.c.pending == 0, by_user.c.success == by_user.c['items']), 1), else_=0)
                    ).label('success'),
                    func.sum(case((and_(by_user.c.pending == 0, by_user.c.uncertain > 0), 1), else_=0)).label(
                        'uncertain'
                    ),
                    func.sum(
                        case(
                            (
                                and_(
                                    by_user.c.pending == 0,
                                    by_user.c.success == 0,
                                    by_user.c.uncertain == 0,
                                    by_user.c.cancelled == 0,
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ).label('skipped'),
                    func.sum(
                        case(
                            (
                                and_(
                                    by_user.c.pending == 0,
                                    by_user.c.success > 0,
                                    by_user.c.success < by_user.c['items'],
                                    by_user.c.uncertain == 0,
                                    by_user.c.cancelled == 0,
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ).label('partial'),
                )
            )
        )
        .mappings()
        .one()
    )
    counts = {k: int(v or 0) for k, v in row.items()}
    items = dict(
        (
            await db.execute(
                select(UserBulkTarget.state, func.count())
                .where(UserBulkTarget.operation_id == operation_id)
                .group_by(UserBulkTarget.state)
            )
        ).all()
    )
    reasons = (
        await db.execute(
            select(UserBulkTarget.message, func.count())
            .where(UserBulkTarget.operation_id == operation_id, UserBulkTarget.state == 'skipped')
            .group_by(UserBulkTarget.message)
        )
    ).all()
    subscription_count = await db.scalar(
        select(func.count(UserBulkTarget.subscription_id)).where(UserBulkTarget.operation_id == operation_id)
    )
    affected = await db.scalar(
        select(func.count(func.distinct(UserBulkTarget.user_id))).where(
            UserBulkTarget.operation_id == operation_id, UserBulkTarget.state != 'skipped'
        )
    )
    skipped_users = counts['total'] - int(affected or 0)
    return {
        'id': operation.id,
        'author_id': operation.author_id,
        'action': operation.action,
        'params': {k: operation.params[k] for k in ('days', 'traffic_gb')},
        'state': operation.state,
        'updated_at': operation.updated_at,
        'cancel_requested': operation.cancel_requested,
        **counts,
        'affected_users': int(affected or 0),
        'skipped_users': skipped_users,
        'subscriptions': int(subscription_count or 0),
        'items': items,
        'skip_reasons': [{'reason': reason, 'count': count} for reason, count in reasons],
        'error_count': counts['uncertain'],
    }


async def cancel(db, operation_id):
    operation = await db.scalar(
        select(UserBulkOperation)
        .where(UserBulkOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if operation is None:
        raise HTTPException(404, 'Operation not found')
    if operation.state in TERMINAL:
        return
    operation.cancel_requested = True
    operation.updated_at = datetime.now(UTC)
    await db.flush()  # take the operation-row lock before touching pending targets
    # A processing target is deliberately untouched: its request may have been sent.
    await db.execute(
        update(UserBulkTarget)
        .where(UserBulkTarget.operation_id == operation_id, UserBulkTarget.state == 'pending')
        .values(state='cancelled', message='Отменена оставшаяся обработка', updated_at=datetime.now(UTC))
    )
    if operation.state != 'running':
        operation.state = 'cancelled'
    await db.commit()


async def run_batch(db, operation_id):
    from app.cabinet.routes.admin_bulk_actions import _execute_for_subscription
    from app.cabinet.schemas.bulk_actions import BulkActionParams, BulkActionType

    operation = await get_operation(db, operation_id)
    await recover(db, operation)
    if operation.cancel_requested or operation.state in TERMINAL:
        return
    operation.state = 'running'
    operation.confirmed_at = operation.confirmed_at or datetime.now(UTC)
    operation.updated_at = datetime.now(UTC)
    action = BulkActionType(operation.action)
    params = BulkActionParams(**{k: operation.params[k] for k in ('days', 'traffic_gb')})
    await db.commit()
    for _ in range(settings.USERS_BULK_BATCH_SIZE):
        # Cancel and claim serialize through the operation row. Handler commits
        # must happen after releasing that row, so cancellation can still arrive.
        operation = await db.scalar(
            select(UserBulkOperation)
            .where(UserBulkOperation.id == operation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if operation.cancel_requested:
            await db.commit()
            break
        target = await db.scalar(
            select(UserBulkTarget)
            .where(UserBulkTarget.operation_id == operation_id, UserBulkTarget.state == 'pending')
            .order_by(UserBulkTarget.id)
            .limit(1)
        )
        if target is None:
            await db.commit()
            break
        await classify_skips(db, operation_id, action, target.id)
        await db.refresh(target)
        if target.state != 'pending':
            await db.commit()
            continue
        target.state = 'processing'
        target.updated_at = datetime.now(UTC)
        target_id, subscription_id, user_id = target.id, target.subscription_id, target.user_id
        await db.commit()  # durable BEFORE any mutation or panel request
        context = durable_execution.set(True)
        try:
            # Validate identity again after snapshot; deleted/reassigned targets are skipped.
            owner = await db.scalar(select(Subscription.user_id).where(Subscription.id == subscription_id))
            if owner != user_id:
                state, message = 'skipped', 'Подписка удалена или сменила владельца'
            else:
                result = await _execute_for_subscription(db, subscription_id, action, params, None, False)
                state = 'success' if result.success else 'uncertain'
                message = (
                    result.message if result.success else 'Результат требует проверки: ошибка выполнения или панели'
                )
        except Exception:
            await db.rollback()
            state, message = 'uncertain', 'Результат требует проверки: выполнение прервано'
        finally:
            durable_execution.reset(context)
        await db.execute(
            update(UserBulkTarget)
            .where(UserBulkTarget.id == target_id)
            .values(state=state, message=message, updated_at=datetime.now(UTC))
        )
        await db.execute(
            update(UserBulkOperation).where(UserBulkOperation.id == operation_id).values(updated_at=datetime.now(UTC))
        )
        await db.commit()
        if state == 'uncertain':
            # Includes 429/timeouts: never blindly retry an additive mutation.
            # Operator resumes remaining targets explicitly after panel recovery.
            break
        await asyncio.sleep(settings.USERS_BULK_ITEM_DELAY)
    operation = await get_operation(db, operation_id)
    pending = await db.scalar(
        select(func.count())
        .select_from(UserBulkTarget)
        .where(UserBulkTarget.operation_id == operation_id, UserBulkTarget.state == 'pending')
    )
    uncertain = await db.scalar(
        select(func.count())
        .select_from(UserBulkTarget)
        .where(UserBulkTarget.operation_id == operation_id, UserBulkTarget.state == 'uncertain')
    )
    operation.state = (
        'cancelled'
        if operation.cancel_requested
        else 'paused'
        if pending
        else 'needs_review'
        if uncertain
        else 'completed'
    )
    operation.updated_at = datetime.now(UTC)
    await db.commit()
