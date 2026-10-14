"""Machine API schemas for person-scoped support tickets (ticket #192, variant A).

The surface speaks for one concrete person identified by `telegram_id`, so it
carries no admin fields (no status/priority/reply-block controls). The field
bounds are the same as in the cabinet user routes
(`app/cabinet/schemas/tickets.py`) — the app must keep one contract when the BFF
stops using the cabinet: `title` 3..255, `message` up to 4000 characters (or a media attachment),
`media_caption` up to 1000. A parity test guards those numbers.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.cabinet.schemas.tickets import TicketCreateRequest, TicketMessageCreateRequest, TicketMessageResponse


class UserTicketMessageResponse(TicketMessageResponse):
    """Machine API uses the same validated contract as the v5 cabinet."""


class UserTicketSummaryResponse(BaseModel):
    id: int
    title: str
    status: str
    priority: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    messages_count: int = 0
    last_message: UserTicketMessageResponse | None = None


class UserTicketDetailResponse(UserTicketSummaryResponse):
    is_reply_blocked: bool = False
    messages: list[UserTicketMessageResponse] = Field(default_factory=list)


class UserTicketListResponse(BaseModel):
    items: list[UserTicketSummaryResponse] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class UserTicketCreateRequest(TicketCreateRequest):
    """Machine API uses the same validated contract as the v5 cabinet."""


class UserTicketMediaUploadResponse(BaseModel):
    """Result of uploading one attachment (`POST /tickets/by-telegram-id/{id}/media`).

    This is exactly the triple the caller passes into ticket creation or a
    message: `media_type`, `media_file_id`, `media_caption`. Telegram addresses
    an uploaded file by `file_id`, so the file itself is never echoed back.
    """

    media_type: str = Field(..., description='Media type: photo, video, document')
    media_file_id: str = Field(..., description='Telegram file_id of the uploaded media')
    media_caption: str | None = Field(default=None, description='Caption stored with the media')


class UserTicketMessageCreateRequest(TicketMessageCreateRequest):
    """Machine API uses the same validated contract as the v5 cabinet."""
