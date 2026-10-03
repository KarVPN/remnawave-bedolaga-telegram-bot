"""Тесты входа из приложения: распознавание ссылки, исходы и выключенный режим.

Интеграция не должна менять поведение бота там, где она не настроена, поэтому
выключенный режим — такой же проверяемый исход, как и успешный вход.
"""

import httpx
import pytest
from structlog.testing import capture_logs

from app.config import Settings, settings
from app.services.app_login_service import (
    AppLoginResult,
    app_login_nonce,
    app_login_return_url,
    confirm_app_login,
    is_app_login_configured,
    is_app_login_payload,
    is_supported_return_url,
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
    """Адрес кнопки возврата: настройка, а не константа (#163).

    Telegram принимает в inline-кнопке только http(s) и на схеме приложения
    отвечает `Bad Request: inline keyboard button URL 'karvpn://login' is
    invalid: Unsupported URL protocol`, отвергая всё сообщение вместе с
    клавиатурой. Поэтому здесь больше нет `karvpn://login`: бот отдаёт
    https-адрес страницы, которая открывает приложение сама, а про схему не
    знает вовсе. Пусто = кнопки нет (значение по умолчанию), потому что
    заведомо негодная ссылка стоит целого сообщения.
    """

    SAMPLE = 'https://app.test.yolgins.ru/app/open'

    @pytest.fixture(autouse=True)
    def _empty_return_url(self, monkeypatch):
        monkeypatch.setattr(settings, 'KARVPN_APP_LOGIN_RETURN_URL', '', raising=False)

    def test_the_shipped_default_is_empty(self):
        """Домен появляется позже кода: без настройки поведение прежнее — кнопки нет."""
        assert Settings.model_fields['KARVPN_APP_LOGIN_RETURN_URL'].default == ''

    def test_empty_setting_means_no_button_and_no_noise(self):
        """Пустая настройка — не ошибка развёртывания, а её обычное начало."""
        with capture_logs() as captured:
            assert app_login_return_url() is None

        assert captured == []

    def test_the_configured_address_is_handed_over_as_it_is(self, monkeypatch):
        monkeypatch.setattr(settings, 'KARVPN_APP_LOGIN_RETURN_URL', self.SAMPLE, raising=False)

        assert app_login_return_url() == self.SAMPLE

    def test_surrounding_whitespace_does_not_leak_into_the_button(self, monkeypatch):
        """Значение приходит из .env, и лишний перевод строки — обычное дело."""
        monkeypatch.setattr(settings, 'KARVPN_APP_LOGIN_RETURN_URL', f'  {self.SAMPLE}\n', raising=False)

        assert app_login_return_url() == self.SAMPLE

    def test_the_value_comes_from_the_environment(self, monkeypatch):
        """Читается то же имя, что записано в .env.example, — иначе настройку не найти."""
        monkeypatch.setenv('KARVPN_APP_LOGIN_RETURN_URL', self.SAMPLE)

        assert Settings(_env_file=None).KARVPN_APP_LOGIN_RETURN_URL == self.SAMPLE

    @pytest.mark.parametrize('url', ['http://app.test.yolgins.ru/app/open', 'https://app.test.yolgins.ru/app/open'])
    def test_http_addresses_are_accepted(self, url):
        assert is_supported_return_url(url) is True
        # Простая проверка из тикета: всё, что попадает в кнопку, начинается с http.
        assert url.startswith('http')

    @pytest.mark.parametrize(
        'url',
        [
            'karvpn://login',  # именно то, на чём Telegram сказал Unsupported URL protocol
            'KARVPN://login',
            'tg://resolve?domain=karvpn',
            'ftp://app.test.yolgins.ru/app/open',
            'app.test.yolgins.ru/app/open',  # без схемы Telegram тоже не примет
        ],
    )
    def test_anything_but_http_is_refused(self, url):
        assert is_supported_return_url(url) is False

    @pytest.mark.parametrize('url', ['karvpn://login', 'ftp://app.test/open'])
    def test_a_refused_scheme_in_the_setting_never_reaches_the_button(self, monkeypatch, url):
        """Прописали схему приложения в переменной — кнопки всё равно не будет."""
        monkeypatch.setattr(settings, 'KARVPN_APP_LOGIN_RETURN_URL', url, raising=False)

        with capture_logs() as captured:
            assert app_login_return_url() is None

        assert [entry['log_level'] for entry in captured] == ['warning']
