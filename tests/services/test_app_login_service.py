"""Тесты входа из приложения: распознавание ссылки, исходы и выключенный режим.

Интеграция не должна менять поведение бота там, где она не настроена, поэтому
выключенный режим — такой же проверяемый исход, как и успешный вход.
"""

import httpx
import pytest

from app.config import settings
from app.services.app_login_service import (
    APP_LOGIN_RETURN_URL,
    AppLoginResult,
    app_login_nonce,
    confirm_app_login,
    is_app_login_configured,
    is_app_login_payload,
)


NONCE = 'K-Dph9mc4v8gGrn0abcd'


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    """Каждый тест начинает с пустых настроек: их выставляет только он сам."""
    monkeypatch.setattr(settings, 'KARVPN_BFF_URL', '', raising=False)
    monkeypatch.setattr(settings, 'KARVPN_BOT_SECRET', '', raising=False)


class _FakeAsyncClient:
    """Подменяет httpx.AsyncClient: запоминает вызов и отдаёт заданный ответ."""

    calls: list[dict] = []
    response: httpx.Response | None = None
    error: Exception | None = None

    def __init__(self, **kwargs):
        _FakeAsyncClient.calls = []
        _FakeAsyncClient.last_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.calls.append({'url': url, 'json': json, 'headers': headers})
        if _FakeAsyncClient.error is not None:
            raise _FakeAsyncClient.error
        return _FakeAsyncClient.response


@pytest.fixture
def fake_client(monkeypatch):
    _FakeAsyncClient.response = httpx.Response(200, json={'status': 'ok'})
    _FakeAsyncClient.error = None
    monkeypatch.setattr(httpx, 'AsyncClient', _FakeAsyncClient)
    return _FakeAsyncClient


def _configure(monkeypatch):
    monkeypatch.setattr(settings, 'KARVPN_BFF_URL', 'https://api.test.yolgins.ru/api/v1', raising=False)
    monkeypatch.setattr(settings, 'KARVPN_BOT_SECRET', 'bot-secret-value-long-enough', raising=False)


class TestPayload:
    def test_recognises_a_real_link(self):
        assert is_app_login_payload('login_' + NONCE) is True
        assert app_login_nonce('login_' + NONCE) == NONCE

    @pytest.mark.parametrize(
        'payload',
        [None, '', 'webauth_' + NONCE, 'login_short', 'login_', 'campaign_1', 'GIFT_abcdefgh'],
    )
    def test_ignores_everything_else(self, payload):
        assert is_app_login_payload(payload) is False


class TestConfirm:
    @pytest.mark.asyncio
    async def test_not_configured_means_disabled_and_no_request(self, fake_client):
        assert is_app_login_configured() is False
        assert await confirm_app_login(895225, NONCE) == AppLoginResult.DISABLED
        assert fake_client.calls == []

    @pytest.mark.asyncio
    async def test_success_sends_the_nonce_and_the_secret(self, monkeypatch, fake_client):
        _configure(monkeypatch)
        assert await confirm_app_login(895225, NONCE) == AppLoginResult.OK
        call = fake_client.calls[0]
        assert call['url'].endswith('/bot/login/confirm')
        assert call['json'] == {'telegram_id': 895225, 'nonce': NONCE}
        assert call['headers']['Authorization'].startswith('Bearer ')

    @pytest.mark.asyncio
    @pytest.mark.parametrize('status', [404, 410])
    async def test_expired_nonce(self, monkeypatch, fake_client, status):
        _configure(monkeypatch)
        fake_client.response = httpx.Response(status, json={'error': {'code': 'not_found'}})
        assert await confirm_app_login(895225, NONCE) == AppLoginResult.EXPIRED

    @pytest.mark.asyncio
    @pytest.mark.parametrize('status', [400, 401, 500, 502])
    async def test_unexpected_answers_are_unavailable(self, monkeypatch, fake_client, status):
        _configure(monkeypatch)
        fake_client.response = httpx.Response(status, json={})
        assert await confirm_app_login(895225, NONCE) == AppLoginResult.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_unreachable_bff_is_unavailable(self, monkeypatch, fake_client):
        _configure(monkeypatch)
        fake_client.error = httpx.ConnectError('no route to host')
        assert await confirm_app_login(895225, NONCE) == AppLoginResult.UNAVAILABLE


class TestReturnLink:
    """Ссылка возврата — половина контракта, вторая половина в приложении.

    Тот же `karvpn://login` зарегистрирован в манифесте приложения
    (`app/android/app/src/main/AndroidManifest.xml`), и его сторожит
    `app/test/android/app_link_manifest_test.dart`: строку меняют в обоих
    репозиториях сразу, иначе кнопка ведёт в никуда.
    """

    def test_the_link_is_the_app_scheme_and_host_the_manifest_registers(self):
        assert APP_LOGIN_RETURN_URL == 'karvpn://login'

    def test_the_link_names_one_scheme_and_one_host(self):
        parsed = httpx.URL(APP_LOGIN_RETURN_URL)
        assert parsed.scheme == 'karvpn'
        assert parsed.host == 'login'
        # Ни пути, ни запроса: intent-filter приложения совпадает по схеме и хосту,
        # а лишние части сделали бы ссылку хрупкой и неотличимой в тесте.
        assert parsed.path in ('', '/')
        assert not parsed.query
