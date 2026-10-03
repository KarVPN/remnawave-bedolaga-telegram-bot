"""Machine API support tickets for one concrete person (ticket #192, variant A).

Why this exists: the app used to reach support through the cabinet ticket routes,
which are authorised by the cabinet *user* JWT. The app keeps that token in
memory for 15 minutes only, and a person who signed in through the bot has no
such token at all — hence "I am already signed in and using the VPN, why should I
sign in to the cabinet again". Here our service talks to our service: the caller
presents the machine key (`X-API-Key`, the same `WEB_API_DEFAULT_TOKEN` used by
the rest of the machine API) and names the person by `telegram_id`, which the BFF
knows from its own session. The internal bot user id is never required.

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
from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ticket import TicketCRUD, TicketMessageCRUD
from app.database.crud.ticket_notification import TicketNotificationCRUD
from app.database.crud.user import get_user_by_telegram_id
from app.database.models import Ticket, TicketMessage, TicketStatus, User

from ..dependencies import get_db_session, require_api_token
from ..schemas.user_tickets import (
    UserTicketCreateRequest,
    UserTicketDetailResponse,
    UserTicketListResponse,
    UserTicketMessageCreateRequest,
    UserTicketMessageResponse,
    UserTicketSummaryResponse,
)


router = APIRouter()
logger = structlog.get_logger(__name__)


def _serialize_message(message: TicketMessage) -> UserTicketMessageResponse:
    return UserTicketMessageResponse(
        id=message.id,
        message_text=message.message_text or '',
        is_from_admin=bool(message.is_from_admin),
        has_media=bool(message.media_file_id),
        media_type=message.media_type,
        media_file_id=message.media_file_id,
        media_caption=message.media_caption,
        created_at=message.created_at,
    )


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
    )

    await _announce_ticket_reply(
        ticket,
        payload.message,
        db,
        media_file_id=payload.media_file_id,
        media_type=payload.media_type,
    )

    return _serialize_message(message)
