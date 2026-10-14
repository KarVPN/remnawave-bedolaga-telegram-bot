"""Machine API support tickets for one concrete person (ticket #192, variant A).

Why this exists: the app used to reach support through the cabinet ticket routes,
which are authorised by the cabinet *user* JWT. The app keeps that token in
memory for 15 minutes only, and a person who signed in through the bot has no
such token at all — hence "I am already signed in and using the VPN, why should I
sign in to the cabinet again". Here our service talks to our service: the caller
presents the machine key (`X-API-Key`, the same `WEB_API_DEFAULT_TOKEN` used by
the rest of the machine API) and names the person by a legacy `telegram_id` or
the exact cabinet `users.id` for email-only profiles.

Ownership: a ticket can be read and continued only by the person it belongs to.
Someone else's ticket answers "not found", never "forbidden" — we do not confirm
that foreign tickets exist.

Messages written here are always from the person (`is_from_admin = False`).
Admin abilities (reply as support, statuses, priorities, reply-block management)
stay in the admin machine API (`app/webapi/routes/tickets.py`) and are
deliberately absent from this router.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Security, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ticket import TicketCRUD, TicketMessageCRUD
from app.database.crud.ticket_notification import TicketNotificationCRUD
from app.database.crud.user import get_user_by_id, get_user_by_telegram_id
from app.database.models import Ticket, TicketMessage, TicketStatus, User
from app.services.ticket_media_service import (
    MAX_TICKET_MEDIA_BYTES,
    media_kind,
    upload_ticket_media,
    upload_ticket_media_to_admin_chat,
)

from ..dependencies import get_db_session, require_api_token
from ..schemas.user_tickets import (
    UserTicketCreateRequest,
    UserTicketDetailResponse,
    UserTicketListResponse,
    UserTicketMediaUploadResponse,
    UserTicketMessageCreateRequest,
    UserTicketMessageResponse,
    UserTicketSummaryResponse,
)


router = APIRouter()
logger = structlog.get_logger(__name__)


def _serialize_message(message: TicketMessage) -> UserTicketMessageResponse:
    from app.cabinet.routes.tickets import _message_to_response

    return UserTicketMessageResponse.model_validate(_message_to_response(message).model_dump())


def _serialize_summary(ticket: Ticket) -> UserTicketSummaryResponse:
    messages = list(ticket.messages or [])
    last_message = max(messages, key=lambda message: message.created_at) if messages else None

    return UserTicketSummaryResponse(
        id=ticket.id,
        title=ticket.title or f'Ticket #{ticket.id}',
        status=ticket.status,
        priority=ticket.priority or 'normal',
        created_at=ticket.created_at,
        updated_at=ticket.updated_at or ticket.created_at,
        closed_at=ticket.closed_at,
        messages_count=len(messages),
        last_message=_serialize_message(last_message) if last_message else None,
    )


def _serialize_detail(ticket: Ticket) -> UserTicketDetailResponse:
    summary = _serialize_summary(ticket)
    messages = sorted(ticket.messages or [], key=lambda message: message.created_at)

    return UserTicketDetailResponse(
        **summary.model_dump(),
        is_reply_blocked=ticket.is_user_reply_blocked,
        messages=[_serialize_message(message) for message in messages],
    )


async def _require_person(db: AsyncSession, telegram_id: int) -> User:
    """The person the caller speaks for; an unknown telegram_id is a 404."""
    user = await get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')
    return user


async def _require_person_by_id(db: AsyncSession, user_id: int) -> User:
    """Resolve one exact cabinet ``users.id``; Telegram IDs are never tried."""
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'User not found')
    return user


async def _require_own_ticket(db: AsyncSession, user: User, ticket_id: int, *, load_messages: bool = True) -> Ticket:
    """Someone else's ticket is indistinguishable from a missing one."""
    ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=load_messages, load_user=False)
    if not ticket or ticket.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Ticket not found')
    return ticket


async def _announce_new_ticket(ticket: Ticket, db: AsyncSession) -> None:
    """Moderators learn about a person's ticket the same way as from the cabinet.

    Failures never break ticket creation: the ticket itself is already stored.
    """
    try:
        from app.handlers.tickets import notify_admins_about_new_ticket

        await notify_admins_about_new_ticket(ticket, db)
    except Exception as error:
        logger.error('Failed to notify admins about a new machine-created ticket', ticket_id=ticket.id, error=error)

    try:
        notification = await TicketNotificationCRUD.create_admin_notification_for_new_ticket(db, ticket)
        if notification:
            from app.cabinet.routes.websocket import notify_admins_new_ticket

            await notify_admins_new_ticket(ticket.id, ticket.title, ticket.user_id)
    except Exception as error:
        logger.error('Failed to create cabinet notification for a new machine-created ticket', error=error)


async def _announce_ticket_reply(
    ticket: Ticket,
    message_text: str,
    db: AsyncSession,
    *,
    media_file_id: str | None = None,
    media_type: str | None = None,
) -> None:
    """Same two channels as the cabinet user route: Telegram + cabinet inbox."""
    try:
        from app.handlers.tickets import notify_admins_about_ticket_reply

        await notify_admins_about_ticket_reply(
            ticket,
            message_text,
            db,
            media_file_id=media_file_id,
            media_type=media_type,
        )
    except Exception as error:
        logger.error(
            'Failed to notify admins about a ticket reply from the machine API', ticket_id=ticket.id, error=error
        )

    try:
        notification = await TicketNotificationCRUD.create_admin_notification_for_user_reply(db, ticket, message_text)
        if notification:
            from app.cabinet.routes.websocket import notify_admins_ticket_reply

            await notify_admins_ticket_reply(ticket.id, (message_text or '')[:100], ticket.user_id)
    except Exception as error:
        logger.error('Failed to create cabinet notification for a ticket reply from the machine API', error=error)


@router.post(
    '/by-telegram-id/{telegram_id}/media',
    response_model=UserTicketMediaUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_person_ticket_media(
    telegram_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    file: UploadFile = File(..., description='The file to attach to the ticket'),
    caption: str | None = Form(None, max_length=1000, description='Optional caption'),
) -> UserTicketMediaUploadResponse:
    """Upload one attachment and return the Telegram triple for the ticket.

    The file is uploaded to Telegram once to obtain a reusable `file_id` and the
    temporary message is removed (see `app/services/ticket_media_service.py`):
    the caller then passes `media_file_id` into ticket creation or a message, and
    the support announcement delivers the attachment exactly once.
    """
    if not settings.is_support_tickets_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Support tickets are disabled')

    await _require_person(db, telegram_id)

    kind = media_kind(file.content_type, file.filename)
    if kind is None:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, 'Unsupported file type')

    data = await file.read(MAX_TICKET_MEDIA_BYTES + 1)
    if len(data) > MAX_TICKET_MEDIA_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f'File is too large (limit {MAX_TICKET_MEDIA_BYTES // (1024 * 1024)} MiB)',
        )
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'File is empty')

    try:
        file_id = await upload_ticket_media(
            telegram_id=telegram_id,
            filename=file.filename or 'upload',
            data=data,
            media_type=kind,
            caption=caption,
        )
    except Exception as error:
        logger.error('Failed to upload ticket media', telegram_id=telegram_id, error=error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            'Support media upload is temporarily unavailable',
        ) from error

    return UserTicketMediaUploadResponse(media_type=kind, media_file_id=file_id, media_caption=caption)


@router.get('/by-telegram-id/{telegram_id}', response_model=UserTicketListResponse)
async def list_person_tickets(
    telegram_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: TicketStatus | None = Query(default=None, alias='status'),
) -> UserTicketListResponse:
    """Tickets of one person, most recently updated first."""
    if not settings.is_support_tickets_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Support tickets are disabled')

    user = await _require_person(db, telegram_id)
    status_value = status_filter.value if status_filter else None

    tickets = await TicketCRUD.get_user_tickets(
        db,
        user_id=user.id,
        status=status_value,
        limit=limit,
        offset=offset,
        load_messages=True,
    )
    total = await TicketCRUD.count_user_tickets_by_statuses(
        db,
        user_id=user.id,
        statuses=[status_value] if status_value else [],
    )

    return UserTicketListResponse(
        items=[_serialize_summary(ticket) for ticket in tickets],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    '/by-telegram-id/{telegram_id}',
    response_model=UserTicketDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_person_ticket(
    telegram_id: int,
    payload: UserTicketCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserTicketDetailResponse:
    """Create a ticket on behalf of the person, with their first message."""
    if not settings.is_support_tickets_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Support tickets are disabled')

    user = await _require_person(db, telegram_id)

    ticket = await TicketCRUD.create_ticket(
        db,
        user_id=user.id,
        title=payload.title,
        message_text=payload.message,
        media_type=payload.media_type,
        media_file_id=payload.media_file_id,
        media_caption=payload.media_caption,
        **(
            {'media_items': [item.model_dump(exclude={'token'}) for item in payload.media_items]}
            if payload.media_items
            else {}
        ),
    )

    await _announce_new_ticket(ticket, db)

    created = await TicketCRUD.get_ticket_by_id(db, ticket.id, load_messages=True, load_user=False)
    return _serialize_detail(created or ticket)


@router.get('/by-telegram-id/{telegram_id}/{ticket_id}', response_model=UserTicketDetailResponse)
async def get_person_ticket(
    telegram_id: int,
    ticket_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserTicketDetailResponse:
    """One ticket of the person, with all messages in chronological order."""
    user = await _require_person(db, telegram_id)
    ticket = await _require_own_ticket(db, user, ticket_id, load_messages=True)
    return _serialize_detail(ticket)


@router.post(
    '/by-telegram-id/{telegram_id}/{ticket_id}/messages',
    response_model=UserTicketMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_person_ticket_message(
    telegram_id: int,
    ticket_id: int,
    payload: UserTicketMessageCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserTicketMessageResponse:
    """Continue the person's ticket with a message from the person."""
    user = await _require_person(db, telegram_id)
    ticket = await _require_own_ticket(db, user, ticket_id, load_messages=False)

    if ticket.status == TicketStatus.CLOSED.value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Cannot add message to closed ticket')

    # Кабинетные роуты проверяли `hasattr(ticket, 'is_reply_blocked')`, а у модели свойство
    # называется `is_user_reply_blocked`, — то есть проверка там мертва, хотя задумана была
    # (та же формулировка отказа). Чат бота такую блокировку соблюдает. Новый машинный вход
    # не должен давать её обойти, поэтому здесь проверяется настоящее свойство.
    if ticket.is_user_reply_blocked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Replies to this ticket are blocked')

    message = await TicketMessageCRUD.add_message(
        db,
        ticket_id=ticket.id,
        user_id=user.id,
        message_text=payload.message,
        is_from_admin=False,
        media_type=payload.media_type,
        media_file_id=payload.media_file_id,
        media_caption=payload.media_caption,
        **(
            {'media_items': [item.model_dump(exclude={'token'}) for item in payload.media_items]}
            if payload.media_items
            else {}
        ),
    )

    await _announce_ticket_reply(
        ticket,
        payload.message,
        db,
        media_file_id=payload.media_file_id,
        media_type=payload.media_type,
    )

    return _serialize_message(message)


@router.post(
    '/by-user-id/{user_id}/media',
    response_model=UserTicketMediaUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_account_ticket_media(
    user_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    file: UploadFile = File(..., description='The file to attach to the ticket'),
    caption: str | None = Form(None, max_length=1000, description='Optional caption'),
) -> UserTicketMediaUploadResponse:
    """Upload for a cabinet account, including one without a Telegram link."""
    if not settings.is_support_tickets_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Support tickets are disabled')

    user = await _require_person_by_id(db, user_id)
    kind = media_kind(file.content_type, file.filename)
    if kind is None:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, 'Unsupported file type')

    data = await file.read(MAX_TICKET_MEDIA_BYTES + 1)
    if len(data) > MAX_TICKET_MEDIA_BYTES:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f'File is too large (limit {MAX_TICKET_MEDIA_BYTES // (1024 * 1024)} MiB)',
        )
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'File is empty')

    try:
        if user.telegram_id is None:
            file_id = await upload_ticket_media_to_admin_chat(
                filename=file.filename or 'upload',
                data=data,
                media_type=kind,
            )
        else:
            file_id = await upload_ticket_media(
                telegram_id=user.telegram_id,
                filename=file.filename or 'upload',
                data=data,
                media_type=kind,
                caption=caption,
            )
    except ValueError as error:
        logger.error('No ticket upload destination is configured', user_id=user.id, error=error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, 'Support media upload is unavailable') from error
    except Exception as error:
        logger.error('Failed to upload account ticket media', user_id=user.id, error=error)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            'Support media upload is temporarily unavailable',
        ) from error

    return UserTicketMediaUploadResponse(media_type=kind, media_file_id=file_id, media_caption=caption)


@router.get('/by-user-id/{user_id}', response_model=UserTicketListResponse)
async def list_account_tickets(
    user_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: TicketStatus | None = Query(default=None, alias='status'),
) -> UserTicketListResponse:
    """List tickets owned by one exact cabinet account ID."""
    if not settings.is_support_tickets_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Support tickets are disabled')

    user = await _require_person_by_id(db, user_id)
    status_value = status_filter.value if status_filter else None
    tickets = await TicketCRUD.get_user_tickets(
        db,
        user_id=user.id,
        status=status_value,
        limit=limit,
        offset=offset,
        load_messages=True,
    )
    total = await TicketCRUD.count_user_tickets_by_statuses(
        db,
        user_id=user.id,
        statuses=[status_value] if status_value else [],
    )
    return UserTicketListResponse(
        items=[_serialize_summary(ticket) for ticket in tickets],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    '/by-user-id/{user_id}',
    response_model=UserTicketDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_account_ticket(
    user_id: int,
    payload: UserTicketCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserTicketDetailResponse:
    """Create a ticket for an exact cabinet account ID."""
    if not settings.is_support_tickets_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Support tickets are disabled')

    user = await _require_person_by_id(db, user_id)
    ticket = await TicketCRUD.create_ticket(
        db,
        user_id=user.id,
        title=payload.title,
        message_text=payload.message,
        media_type=payload.media_type,
        media_file_id=payload.media_file_id,
        media_caption=payload.media_caption,
        **(
            {'media_items': [item.model_dump(exclude={'token'}) for item in payload.media_items]}
            if payload.media_items
            else {}
        ),
    )
    await _announce_new_ticket(ticket, db)
    created = await TicketCRUD.get_ticket_by_id(db, ticket.id, load_messages=True, load_user=False)
    return _serialize_detail(created or ticket)


@router.get('/by-user-id/{user_id}/{ticket_id}', response_model=UserTicketDetailResponse)
async def get_account_ticket(
    user_id: int,
    ticket_id: int,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserTicketDetailResponse:
    """Read a ticket only when its owner matches the exact cabinet account."""
    user = await _require_person_by_id(db, user_id)
    ticket = await _require_own_ticket(db, user, ticket_id, load_messages=True)
    return _serialize_detail(ticket)


@router.post(
    '/by-user-id/{user_id}/{ticket_id}/messages',
    response_model=UserTicketMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_account_ticket_message(
    user_id: int,
    ticket_id: int,
    payload: UserTicketMessageCreateRequest,
    _: Any = Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> UserTicketMessageResponse:
    """Append a person message after exact account and ticket ownership checks."""
    user = await _require_person_by_id(db, user_id)
    ticket = await _require_own_ticket(db, user, ticket_id, load_messages=False)

    if ticket.status == TicketStatus.CLOSED.value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Cannot add message to closed ticket')
    if ticket.is_user_reply_blocked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, 'Replies to this ticket are blocked')

    message = await TicketMessageCRUD.add_message(
        db,
        ticket_id=ticket.id,
        user_id=user.id,
        message_text=payload.message,
        is_from_admin=False,
        media_type=payload.media_type,
        media_file_id=payload.media_file_id,
        media_caption=payload.media_caption,
        **(
            {'media_items': [item.model_dump(exclude={'token'}) for item in payload.media_items]}
            if payload.media_items
            else {}
        ),
    )
    await _announce_ticket_reply(
        ticket,
        payload.message,
        db,
        media_file_id=payload.media_file_id,
        media_type=payload.media_type,
    )
    return _serialize_message(message)
