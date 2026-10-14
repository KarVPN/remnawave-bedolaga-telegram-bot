"""Загрузка вложения тикета: один раз в Telegram и без копии в поддержке.

Здесь проверяется сама загрузка (её в маршрутных тестах подменяют): файл уходит
в личный чат человека ради `file_id`, временное сообщение удаляется, а поддержка
получает вложение позже — один раз, через анонс обращения.
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest

from app import bot_factory
from app.cabinet.routes import media as cabinet_media
from app.services.ticket_media_service import (
    MAX_TICKET_MEDIA_BYTES,
    media_kind,
    upload_ticket_media,
    upload_ticket_media_to_admin_chat,
)


TELEGRAM_ID = 895225


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeBot:
    """Мини-бот: пишет, куда и что отправил, и что удалил."""

    def __init__(self, *, delete_error: Exception | None = None) -> None:
        self.session = _FakeSession()
        self.sent: list[tuple[str, dict]] = []
        self.deleted: list[dict] = []
        self.delete_error = delete_error

    async def send_document(self, **kwargs):
        self.sent.append(('document', kwargs))
        return SimpleNamespace(document=SimpleNamespace(file_id='DOC_FILE_ID'), message_id=11)

    async def send_photo(self, **kwargs):
        self.sent.append(('photo', kwargs))
        return SimpleNamespace(
            photo=[SimpleNamespace(file_id='SMALL'), SimpleNamespace(file_id='PHOTO_FILE_ID')], message_id=12
        )

    async def send_video(self, **kwargs):
        self.sent.append(('video', kwargs))
        return SimpleNamespace(video=SimpleNamespace(file_id='VIDEO_FILE_ID'), message_id=13)

    async def delete_message(self, **kwargs):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(kwargs)
        return True


@pytest.fixture
def fake_bot(monkeypatch):
    bot = _FakeBot()
    monkeypatch.setattr(bot_factory, 'create_bot', lambda: bot)
    return bot


class TestMediaKind:
    """Правила типа: логи — документ, картинка — фото, мусор — отказ."""

    @pytest.mark.parametrize(
        ('content_type', 'filename', 'expected'),
        [
            ('text/plain', 'app.log', 'document'),
            ('application/octet-stream', 'app.log', 'document'),
            ('application/octet-stream', 'logs.zip', 'document'),
            ('application/json', 'diagnostics.json', 'document'),
            ('application/zip', 'archive.zip', 'document'),
            ('image/png', 'screenshot.png', 'photo'),
            ('video/mp4', 'clip.mp4', 'video'),
            ('audio/mpeg', 'voice.mp3', None),
            ('application/x-msdownload', 'setup.exe', None),
        ],
    )
    def test_kind(self, content_type, filename, expected):
        assert media_kind(content_type, filename) == expected

    def test_unknown_binary_without_a_hint_is_a_document(self):
        assert media_kind('', 'dump.bin') == 'document'

    def test_the_limit_is_the_documented_ten_mebibytes(self):
        assert MAX_TICKET_MEDIA_BYTES == 10 * 1024 * 1024


class TestUploading:
    async def test_document_is_sent_to_the_person_and_the_copy_is_deleted(self, fake_bot):
        file_id = await upload_ticket_media(
            telegram_id=TELEGRAM_ID,
            filename='app.log',
            data=b'log line\n',
            media_type='document',
            caption='Лог приложения',
        )

        assert file_id == 'DOC_FILE_ID'
        assert len(fake_bot.sent) == 1
        kind, kwargs = fake_bot.sent[0]
        assert kind == 'document'
        assert kwargs['chat_id'] == TELEGRAM_ID
        assert kwargs['caption'] == 'Лог приложения'
        assert kwargs['disable_notification'] is True
        # Временное сообщение уходит: в поддержке файл появится один раз, из анонса.
        assert fake_bot.deleted == [{'chat_id': TELEGRAM_ID, 'message_id': 11}]
        assert fake_bot.session.closed is True

    async def test_photo_and_video_file_ids_are_read_from_the_right_field(self, monkeypatch):
        photo_bot = _FakeBot()
        monkeypatch.setattr(bot_factory, 'create_bot', lambda: photo_bot)
        photo_id = await upload_ticket_media(
            telegram_id=TELEGRAM_ID, filename='shot.png', data=b'png', media_type='photo', caption=None
        )
        assert photo_id == 'PHOTO_FILE_ID'

        video_bot = _FakeBot()
        monkeypatch.setattr(bot_factory, 'create_bot', lambda: video_bot)
        video_id = await upload_ticket_media(
            telegram_id=TELEGRAM_ID, filename='clip.mp4', data=b'mp4', media_type='video', caption=None
        )
        assert video_id == 'VIDEO_FILE_ID'

    async def test_a_failed_deletion_does_not_lose_the_file_id(self, monkeypatch):
        """Сбой удаления не должен превращать успешную загрузку в ошибку.

        Копия остаётся в личном чате человека (его же лог), а поддержке файл
        всё равно уйдёт один раз — по file_id из анонса.
        """
        bot = _FakeBot(delete_error=RuntimeError('message is too old'))
        monkeypatch.setattr(bot_factory, 'create_bot', lambda: bot)

        file_id = await upload_ticket_media(
            telegram_id=TELEGRAM_ID, filename='app.log', data=b'x', media_type='document', caption=None
        )

        assert file_id == 'DOC_FILE_ID'
        assert bot.deleted == []
        assert bot.session.closed is True

    async def test_email_only_ticket_upload_uses_existing_admin_chat_storage(self, fake_bot, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(type(settings), 'get_admin_notifications_chat_id', lambda _settings: -100123)
        monkeypatch.setattr(type(settings), 'get_admin_ids', lambda _settings: [123])

        file_id = await upload_ticket_media_to_admin_chat(
            filename='app.log',
            data=b'log line\n',
            media_type='document',
        )

        assert file_id == 'DOC_FILE_ID'
        kind, sent = fake_bot.sent[0]
        assert kind == 'document'
        assert sent['chat_id'] == -100123
        assert 'caption' not in sent
        assert fake_bot.deleted == []
        assert fake_bot.session.closed is True

    async def test_email_only_upload_falls_back_to_existing_admin_destination(self, fake_bot, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(type(settings), 'get_admin_notifications_chat_id', lambda _settings: None)
        monkeypatch.setattr(type(settings), 'get_admin_ids', lambda _settings: [123])

        await upload_ticket_media_to_admin_chat(filename='app.log', data=b'log line\n', media_type='document')

        assert fake_bot.sent[0][1]['chat_id'] == 123


class TestCabinetMediaDownload:
    async def test_download_route_returns_the_telegram_file_and_closes_the_bot(self, monkeypatch):
        class _DownloadBot:
            def __init__(self):
                self.session = _FakeSession()
                self.requested_file_id = None
                self.requested_path = None

            async def get_file(self, file_id):
                self.requested_file_id = file_id
                return SimpleNamespace(file_path='photos/screenshot.jpg')

            async def download_file(self, file_path):
                self.requested_path = file_path
                return BytesIO(b'jpeg bytes')

        bot = _DownloadBot()
        monkeypatch.setattr(cabinet_media, 'create_bot', lambda: bot)

        response = await cabinet_media.download_media('photo-file-id')

        assert bot.requested_file_id == 'photo-file-id'
        assert bot.requested_path == 'photos/screenshot.jpg'
        assert response.body == b'jpeg bytes'
        assert response.media_type == 'image/jpeg'
        assert response.headers['content-disposition'] == 'inline; filename=screenshot.jpg'
        assert bot.session.closed is True
