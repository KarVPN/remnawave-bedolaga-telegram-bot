"""Вложение тикета доходит до поддержки настоящим сообщением Telegram.

Машинный API поддержки принимает `media_file_id`, но анонс админам раньше умел
отправлять только фото: документ (лог приложения) молча терялся и оставался
текстом. Здесь проверяется, что вложение отправляется в тикет-топик тем же
`file_id`, с текстом в caption, и что фото и видео не сломаны.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.admin_notification_service import AdminNotificationService, NotificationCategory
from app.services.support_settings_service import SupportSettingsService


CHAT_ID = -1001234567890
TICKET_TOPIC_ID = 77
FILE_ID = 'BQACAgIAAxkBAAI'
TEXT = 'Лог приложения во вложении'


class _FakeBot:
    """Запоминает вызовы Bot API вместо сети."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def send_document(self, **kwargs):
        self.calls.append(('document', kwargs))
        return SimpleNamespace(message_id=1)

    async def send_photo(self, **kwargs):
        self.calls.append(('photo', kwargs))
        return SimpleNamespace(message_id=2)

    async def send_video(self, **kwargs):
        self.calls.append(('video', kwargs))
        return SimpleNamespace(message_id=3)

    async def send_message(self, **kwargs):
        self.calls.append(('message', kwargs))
        return SimpleNamespace(message_id=4)


@pytest.fixture(autouse=True)
def _ticket_notifications_on(monkeypatch):
    monkeypatch.setattr(
        SupportSettingsService,
        'get_admin_ticket_notifications_enabled',
        staticmethod(lambda: True),
    )


def _service(bot: _FakeBot) -> AdminNotificationService:
    service = AdminNotificationService(bot)
    service.chat_id = CHAT_ID
    service.enabled = True
    service.topic_id = None
    service.ticket_topic_id = TICKET_TOPIC_ID
    service.category_topics[NotificationCategory.TICKETS] = TICKET_TOPIC_ID
    return service


def _media_calls(bot: _FakeBot) -> list[tuple[str, dict]]:
    return [call for call in bot.calls if call[0] in ('document', 'photo', 'video')]


class TestTicketAttachment:
    async def test_document_is_sent_with_the_text_as_caption(self):
        bot = _FakeBot()

        sent = await _service(bot).send_ticket_event_notification(TEXT, media_file_id=FILE_ID, media_type='document')

        assert sent is True
        assert bot.calls == [
            (
                'document',
                {
                    'chat_id': CHAT_ID,
                    'message_thread_id': TICKET_TOPIC_ID,
                    'caption': TEXT,
                    'parse_mode': 'HTML',
                    'document': FILE_ID,
                },
            )
        ]

    async def test_long_text_goes_first_and_the_document_follows(self):
        bot = _FakeBot()

        sent = await _service(bot).send_ticket_event_notification(
            'a' * 2000, media_file_id=FILE_ID, media_type='document'
        )

        assert sent is True
        assert [call[0] for call in bot.calls] == ['message', 'document']
        assert bot.calls[1][1]['document'] == FILE_ID
        assert 'caption' not in bot.calls[1][1]

    async def test_photo_still_goes_as_photo(self):
        bot = _FakeBot()

        sent = await _service(bot).send_ticket_event_notification(TEXT, media_file_id=FILE_ID, media_type='photo')

        assert sent is True
        assert _media_calls(bot) == [
            (
                'photo',
                {
                    'chat_id': CHAT_ID,
                    'message_thread_id': TICKET_TOPIC_ID,
                    'caption': TEXT,
                    'parse_mode': 'HTML',
                    'photo': FILE_ID,
                },
            )
        ]

    async def test_video_goes_as_video(self):
        bot = _FakeBot()

        sent = await _service(bot).send_ticket_event_notification(TEXT, media_file_id=FILE_ID, media_type='video')

        assert sent is True
        assert _media_calls(bot)[0][0] == 'video'
        assert _media_calls(bot)[0][1]['video'] == FILE_ID

    async def test_an_unknown_media_type_does_not_invent_an_attachment(self):
        bot = _FakeBot()

        sent = await _service(bot).send_ticket_event_notification(TEXT, media_file_id=FILE_ID, media_type='audio')

        assert sent is True
        assert bot.calls[0][0] == 'message'
        assert _media_calls(bot) == []
