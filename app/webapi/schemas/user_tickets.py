"""Machine API schemas for person-scoped support tickets (ticket #192, variant A).

The surface speaks for one concrete person identified by `telegram_id`, so it
carries no admin fields (no status/priority/reply-block controls). The field
bounds are the same as in the cabinet user routes
(`app/cabinet/schemas/tickets.py`) — the app must keep one contract when the BFF
stops using the cabinet: `title` 3..255, `message` 10..4000 characters,
`media_caption` up to 1000. A parity test guards those numbers.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UserTicketMessageResponse(BaseModel):
    id: int
    message_text: str
    is_from_admin: bool
    has_media: bool = False
    media_type: str | None = None
    media_file_id: str | None = None
    media_caption: str | None = None
    created_at: datetime


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


class UserTicketCreateRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=255, description='Ticket title')
    message: str = Field(..., min_length=10, max_length=4000, description='Initial message from the person')
    media_type: str | None = Field(default=None, description='Media type: photo, video, document')
    media_file_id: str | None = Field(default=None, description='Telegram file_id of uploaded media')
    media_caption: str | None = Field(default=None, max_length=1000, description='Media caption')


class UserTicketMessageCreateRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000, description='Message from the person')
    media_type: str | None = Field(default=None, description='Media type: photo, video, document')
    media_file_id: str | None = Field(default=None, description='Telegram file_id of uploaded media')
    media_caption: str | None = Field(default=None, max_length=1000, description='Media caption')
