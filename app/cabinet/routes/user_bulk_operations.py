"""Preview, bounded execution and durable progress for the Users page."""

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import case, select

from app.cabinet.dependencies import get_cabinet_db, require_permission
from app.cabinet.schemas.user_bulk_operations import UserBulkPreview
from app.database.database import AsyncSessionLocal, engine
from app.database.models import UserBulkNotification, UserBulkOperation, UserBulkTarget
from app.services import user_bulk_operations as service


router = APIRouter(prefix='/admin/user-bulk-operations', tags=['Cabinet Admin Bulk Actions'])
# Reading the journal exposes user IDs, so both permissions are required.
router.dependencies = [Depends(require_permission('users:read'))]


@router.post('/preview')
async def preview(
    request: UserBulkPreview,
    http_request: Request,
    admin=Depends(require_permission('bulk_actions:execute')),
    db=Depends(get_cabinet_db),
):
    if request.notify_users:
        await require_permission('users:send_message')(request=http_request, user=admin, db=db)
    operation_id = await service.preview(db, admin.id, request)
    return await service.summary(db, operation_id)


@router.get('')
async def list_operations(admin=Depends(require_permission('bulk_actions:execute')), db=Depends(get_cabinet_db)):
    ids = (
        await db.scalars(
            select(UserBulkOperation.id)
            .where(UserBulkOperation.author_id == admin.id, UserBulkOperation.confirmed_at.is_not(None))
            .order_by(
                case((UserBulkOperation.state.in_(['running', 'paused']), 0), else_=1),
                UserBulkOperation.created_at.desc(),
            )
            .limit(10)
        )
    ).all()
    return [await service.summary(db, operation_id) for operation_id in ids]


@router.get('/{operation_id}')
async def get_operation(
    operation_id: UUID, admin=Depends(require_permission('bulk_actions:execute')), db=Depends(get_cabinet_db)
):
    operation_id = str(operation_id)
    operation = await service.get_operation(db, operation_id)
    if operation.state == 'running':
        async with service.execution_lock(engine) as acquired:
            if acquired:
                await service.recover(db, operation)
    return await service.summary(db, operation_id)


@router.post('/{operation_id}/run')
async def run(
    operation_id: UUID,
    request: Request,
    admin=Depends(require_permission('bulk_actions:execute')),
    permission_db=Depends(get_cabinet_db),
):
    operation = await service.get_operation(permission_db, str(operation_id))
    if operation.params.get('notify_users'):
        await require_permission('users:send_message')(request=request, user=admin, db=permission_db)
    await permission_db.rollback()
    # Independent sessions: permission resolution never holds a DB transaction
    # throughout panel I/O. An advisory lock survives business-handler commits.
    async with service.execution_lock(engine) as acquired:
        if not acquired:
            raise HTTPException(409, 'Другая пачка уже выполняется. Обновите состояние перед продолжением.')
        async with AsyncSessionLocal() as db:
            await service.run_batch(db, str(operation_id))
            return await service.summary(db, str(operation_id))


@router.post('/{operation_id}/cancel')
async def cancel(
    operation_id: UUID, admin=Depends(require_permission('bulk_actions:execute')), db=Depends(get_cabinet_db)
):
    await service.cancel(db, str(operation_id))
    return await service.summary(db, str(operation_id))


@router.get('/{operation_id}/errors')
async def errors(
    operation_id: UUID,
    offset: int = Query(0, ge=0),
    admin=Depends(require_permission('bulk_actions:execute')),
    db=Depends(get_cabinet_db),
):
    await service.get_operation(db, str(operation_id))
    rows = (
        (
            await db.execute(
                select(
                    UserBulkTarget.user_id, UserBulkTarget.subscription_id, UserBulkTarget.state, UserBulkTarget.message
                )
                .where(UserBulkTarget.operation_id == str(operation_id), UserBulkTarget.state == 'uncertain')
                .order_by(UserBulkTarget.id)
                .offset(offset)
                .limit(50)
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


@router.get('/{operation_id}/notification-errors')
async def notification_errors(
    operation_id: UUID,
    offset: int = Query(0, ge=0),
    admin=Depends(require_permission('bulk_actions:execute')),
    db=Depends(get_cabinet_db),
):
    operation = await service.get_operation(db, str(operation_id))
    if not operation.params.get('notify_users'):
        return []
    rows = (
        (
            await db.execute(
                select(
                    UserBulkNotification.user_id,
                    UserBulkNotification.state,
                    UserBulkNotification.message,
                )
                .where(
                    UserBulkNotification.operation_id == str(operation_id),
                    UserBulkNotification.state.in_(['failed', 'limited', 'uncertain', 'skipped']),
                )
                .order_by(UserBulkNotification.id)
                .offset(offset)
                .limit(50)
            )
        )
        .mappings()
        .all()
    )
    return [dict(row, subscription_id=None) for row in rows]


@router.get('/{operation_id}/events')
async def events(
    operation_id: UUID,
    request: Request,
    admin=Depends(require_permission('bulk_actions:execute')),
    db=Depends(get_cabinet_db),
):
    await service.get_operation(db, str(operation_id))
    await db.rollback()

    async def stream():
        # Same SSE progress/complete event contract as the existing bulk endpoint,
        # with journal counters replacing ephemeral in-memory counters.
        previous = None
        while not await request.is_disconnected():
            async with AsyncSessionLocal() as session:
                snapshot = await service.summary(session, str(operation_id))
            payload = json.dumps(snapshot, default=str, ensure_ascii=False)
            terminal = snapshot['state'] in service.TERMINAL
            if payload != previous or terminal:
                event = 'complete' if terminal else 'progress'
                yield f'event: {event}\ndata: {payload}\n\n'
                previous = payload
            else:
                yield ': heartbeat\n\n'
            if terminal:
                return
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )
