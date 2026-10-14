"""Attachments for support tickets: turn a file of ours into a Telegram `file_id`.

Why this exists. The machine API of the support tickets accepts a Telegram
`media_file_id` — an identifier only the bot can produce. Our side (the BFF and
the app) has the bytes of an application log, not a Telegram message, so the bot
has to receive the file and upload it once.

How the upload avoids duplicates. Telegram addresses a file a bot has already
uploaded by `file_id`, and there is no upload without a message. So the file is
sent to the person's own chat — not to the support chat — and that temporary
message is removed right away. The support chat then carries the attachment
exactly once: the ticket announcement forwards it by the returned `file_id`.
Sending to the person's own chat (their own log) and not to support means a
failed deletion can never leave a second copy where the moderators look.
"""

from __future__ import annotations

import mimetypes

import structlog
from aiogram.types import BufferedInputFile


logger = structlog.get_logger(__name__)

# The application log of a phone is small; 10 MiB is a generous ceiling that
# still protects the bot from an accidental stream.
MAX_TICKET_MEDIA_BYTES = 10 * 1024 * 1024

# Kinds Telegram can carry that we forward. Anything else is refused with 415
# instead of being silently turned into an unknown document.
PHOTO_PREFIX = 'image/'
VIDEO_PREFIX = 'video/'
TEXT_PREFIX = 'text/'

# Content types that stay documents even though they are not text. Diagnostics
# and screenshots the app may attach: logs, archives, dumps, reports.
ALLOWED_DOCUMENT_CONTENT_TYPES = frozenset(
    {
        'application/json',
        'application/pdf',
        'application/sql',
        'application/xml',
        'application/yaml',
        'application/x-yaml',
        'application/x-ndjson',
        'application/zip',
        'application/gzip',
        'application/x-gzip',
        'application/x-tar',
        'application/x-7z-compressed',
        # Unknown binary: the filename carries no usable type either, but a log
        # the app labelled octet-stream must still reach support.
        'application/octet-stream',
    }
)


def media_kind(content_type: str | None, filename: str | None) -> str | None:
    """The Telegram kind of a file, or None when we refuse to forward it.

    The declared content type wins; when it is missing or generic, the filename
    is asked. Photos, videos and anything text-like or explicitly allowed stay;
    everything else (audio, executables, unknown binaries) is refused.
    """
    normalized = (content_type or '').split(';')[0].strip().lower()

    if not normalized or normalized == 'application/octet-stream':
        guessed = mimetypes.guess_type(filename or '')[0] or ''
        if guessed:
            normalized = guessed.lower()

    if not normalized:
        normalized = 'application/octet-stream'

    if normalized.startswith(PHOTO_PREFIX):
        return 'photo'
    if normalized.startswith(VIDEO_PREFIX):
        return 'video'
    if normalized.startswith(TEXT_PREFIX):
        return 'document'
    if normalized in ALLOWED_DOCUMENT_CONTENT_TYPES:
        return 'document'
    return None


async def upload_ticket_media(
    *,
    telegram_id: int,
    filename: str,
    data: bytes,
    media_type: str,
    caption: str | None,
) -> str:
    """Upload the file once and return the Telegram `file_id` of the attachment.

    The temporary message goes to the person's own chat and is deleted at once;
    the caller keeps only the `file_id` and passes it into ticket creation or a
    message, where the support announcement delivers the attachment.
    """
    file_id, _ = await upload_media_to_chat(
        chat_id=telegram_id,
        filename=filename,
        data=data,
        media_type=media_type,
        caption=caption,
        disable_notification=True,
        delete_after_upload=True,
    )
    return file_id


def resolve_ticket_upload_chat_id() -> int:
    """Return the existing cabinet ticket upload destination."""
    from app.config import settings

    chat_id = settings.get_admin_notifications_chat_id()
    if chat_id is not None:
        return chat_id

    admin_ids = settings.get_admin_ids()
    if admin_ids:
        return admin_ids[0]

    raise ValueError('No chat configured for file uploads')


async def upload_ticket_media_to_admin_chat(
    *,
    filename: str,
    data: bytes,
    media_type: str,
) -> str:
    """Upload a ticket attachment through the cabinet's existing Telegram storage."""
    file_id, _ = await upload_media_to_chat(
        chat_id=resolve_ticket_upload_chat_id(),
        filename=filename,
        data=data,
        media_type=media_type,
    )
    return file_id


async def upload_media_to_chat(
    *,
    chat_id: int,
    filename: str,
    data: bytes,
    media_type: str,
    caption: str | None = None,
    disable_notification: bool = False,
    delete_after_upload: bool = False,
) -> tuple[str, str | None]:
    """Upload one Telegram media object and return its reusable file ID."""
    from app.bot_factory import create_bot

    bot = create_bot()
    try:
        upload = BufferedInputFile(data, filename=filename or 'upload')
        send_kwargs: dict = {'chat_id': chat_id}
        if caption is not None:
            send_kwargs['caption'] = caption
        if disable_notification:
            send_kwargs['disable_notification'] = True

        if media_type == 'photo':
            message = await bot.send_photo(photo=upload, **send_kwargs)
            media = message.photo[-1]
        elif media_type == 'video':
            message = await bot.send_video(video=upload, **send_kwargs)
            media = message.video
        else:
            message = await bot.send_document(document=upload, **send_kwargs)
            media = message.document

        if delete_after_upload:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
            except Exception as delete_error:
                logger.warning(
                    'Failed to delete the temporary ticket media message',
                    telegram_id=chat_id,
                    error=delete_error,
                )

        return media.file_id, getattr(media, 'file_unique_id', None)
    finally:
        await bot.session.close()
