"""Кабинетный вход: кнопка возврата в приложение в ответе на подтверждение (#163).

Живого Telegram в тестах нет, поэтому проверяется то, что до него: клавиатура,
которую получит сообщение, ключи в локалях (без них каждый вход пишет «Missing
localization key» в лог) и то, что подтверждение по-прежнему связывает токен.

Приглашение подтвердить вход и ответ на него — одно и то же сообщение, поэтому
ответ приходит правкой: кнопки «Да, войти / Нет» уходят вместе с ним, а на
подтверждённом входе их место занимает возврат в приложение. Адрес кнопки — та же
настройка `KARVPN_APP_LOGIN_RETURN_URL` и те же правила, что у входа по
`/start login_<nonce>`: только http(s), пусто — кнопки нет, отказ Telegram —
тот же текст без клавиатуры (#163).
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from structlog.testing import capture_logs

from app.config import settings
from app.database.models import UserStatus
from app.handlers import start as start_handler
from app.handlers.start import (
    EMPTY_INLINE_KEYBOARD,
    app_login_reply,
    app_login_return_keyboard,
    edit_app_login_answer,
    process_webauth_confirm,
)
from app.localization.texts import get_texts
from app.services.app_login_service import AppLoginResult


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = PROJECT_ROOT / 'app' / 'localization' / 'locales'
HANDLER = PROJECT_ROOT / 'app' / 'handlers' / 'start.py'
LANGUAGES = ('ru', 'en')
RETURN_URL = 'https://api.test.yolgins.ru/app/open'
TOKEN = 'K-Dph9mc4v8gGrn0abcd'
TELEGRAM_ID = 895225
USER_ID = 42


@pytest.fixture(autouse=True)
def _no_return_url(monkeypatch):
    """По умолчанию адреса возврата нет: так выглядит контур без настройки."""
    monkeypatch.setattr(settings, 'KARVPN_APP_LOGIN_RETURN_URL', '', raising=False)


@pytest.fixture
def return_url(monkeypatch, _no_return_url):
    """Адрес возврата задан — и только тогда кнопка вообще собирается."""
    monkeypatch.setattr(settings, 'KARVPN_APP_LOGIN_RETURN_URL', RETURN_URL, raising=False)
    return RETURN_URL


def _locale(language: str) -> dict:
    return json.loads((LOCALES_DIR / f'{language}.json').read_text(encoding='utf-8'))


def _named_keys() -> set[str]:
    """Ключи из кода, а не из теста: список обновляется вместе с обработчиком."""
    return set(re.findall(r"'(WEB_AUTH_[A-Z_]+)'", HANDLER.read_text(encoding='utf-8')))


@pytest.mark.parametrize('language', LANGUAGES)
def test_every_key_the_handler_names_is_in_the_locales(language):
    """Пропавший ключ не ломает вход — он пишет «Missing localization key» в лог."""
    keys = _named_keys()
    assert keys, 'обработчик не называет ни одного ключа WEB_AUTH_*'

    assert sorted(keys - set(_locale(language))) == []


@pytest.mark.parametrize('language', LANGUAGES)
def test_the_localized_text_is_used_and_nothing_warns(language):
    """`texts.t(key)` без запасного текста: найдётся он только в локали."""
    locale = _locale(language)
    texts = get_texts(language)

    with capture_logs() as captured:
        for key in sorted(_named_keys()):
            assert texts.t(key) == locale[key], f'{language}: {key}'

    assert [entry for entry in captured if entry.get('log_level') == 'warning'] == []


def test_the_way_back_is_built_once_for_both_ways_in(return_url):
    """Кнопка кабинетного входа — та же, что у ссылки входа из приложения (#78)."""
    texts = get_texts('ru')

    _, from_the_login_link = app_login_reply(AppLoginResult.OK, texts)
    keyboard = app_login_return_keyboard(texts)

    assert keyboard == from_the_login_link, 'кнопка собирается в одном месте, а не в двух'
    button = keyboard.inline_keyboard[0][0]
    assert button.url == return_url
    assert button.text == texts.t('APP_LOGIN_RETURN_BUTTON')


@pytest.mark.parametrize(
    'scheme_url',
    ['karvpn://login', 'tg://resolve?domain=karvpn', 'ftp://api.test.yolgins.ru/app/open'],
)
def test_a_scheme_telegram_refuses_never_becomes_a_button(scheme_url, monkeypatch):
    """Схему приложения в переменной прописали — кнопки всё равно не будет."""
    monkeypatch.setattr(settings, 'KARVPN_APP_LOGIN_RETURN_URL', scheme_url, raising=False)

    with capture_logs() as captured:
        assert app_login_return_keyboard(get_texts('ru')) is None

    assert [entry['log_level'] for entry in captured] == ['warning'], 'о причине должно быть видно в логе'


@pytest.mark.asyncio
async def test_a_confirmed_sign_in_hands_the_way_back(return_url, user, link):
    """Случай владельца: подтвердил вход — и вернулся одним касанием (#163)."""
    calls: list[dict] = []
    message = _prompt(calls)

    with capture_logs() as captured:
        await process_webauth_confirm(_tap(f'webauth_confirm:{TOKEN}', message), object())

    assert link.calls == [{'token': TOKEN, 'telegram_id': TELEGRAM_ID, 'user_id': USER_ID}], (
        'подтверждение по-прежнему связывает токен'
    )
    assert [call['text'] for call in calls] == [get_texts('ru').t('WEB_AUTH_SUCCESS')]
    rows = calls[0]['reply_markup'].inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1, 'кнопка возврата — одна и в своём ряду'
    assert rows[0][0].url == return_url
    assert rows[0][0].text == get_texts('ru').t('APP_LOGIN_RETURN_BUTTON')
    assert captured == []


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_the_answer_is_in_the_language_of_the_person(language, monkeypatch, return_url, link):
    """Ключи добавлены ради этого: ответ и подпись кнопки — на языке человека."""

    async def _get_user(_db, _telegram_id):
        return _User(language=language)

    monkeypatch.setattr(start_handler, 'get_user_by_telegram_id', _get_user)
    calls: list[dict] = []
    message = _prompt(calls)

    await process_webauth_confirm(_tap(f'webauth_confirm:{TOKEN}', message), object())

    texts = get_texts(language)
    assert [call['text'] for call in calls] == [texts.t('WEB_AUTH_SUCCESS')]
    assert calls[0]['reply_markup'].inline_keyboard[0][0].text == texts.t('APP_LOGIN_RETURN_BUTTON')


@pytest.mark.asyncio
async def test_without_a_return_address_the_prompt_buttons_still_go(user, link):
    """Пустая настройка — не негодная ссылка, а её отсутствие (#163).

    Кнопок «Да, войти / Нет» под ответом тоже быть не должно: правка, которой
    клавиатуру не передали вовсе, оставила бы их там.
    """
    calls: list[dict] = []
    message = _prompt(calls)

    with capture_logs() as captured:
        await process_webauth_confirm(_tap(f'webauth_confirm:{TOKEN}', message), object())

    assert [call['text'] for call in calls] == [get_texts('ru').t('WEB_AUTH_SUCCESS')]
    assert calls[0]['reply_markup'].inline_keyboard == [], 'без адреса кнопки нет, а старая — уходит'
    assert captured == [], 'отсутствие настройки — не предупреждение'


@pytest.mark.asyncio
async def test_a_scheme_in_the_setting_leaves_no_button_in_the_answer(monkeypatch, user, link):
    """Негодный адрес не подставляется: кнопки нет, текст и подтверждение — на месте."""
    monkeypatch.setattr(settings, 'KARVPN_APP_LOGIN_RETURN_URL', 'karvpn://login', raising=False)
    calls: list[dict] = []
    message = _prompt(calls)

    with capture_logs() as captured:
        await process_webauth_confirm(_tap(f'webauth_confirm:{TOKEN}', message), object())

    assert link.calls == [{'token': TOKEN, 'telegram_id': TELEGRAM_ID, 'user_id': USER_ID}]
    assert [call['text'] for call in calls] == [get_texts('ru').t('WEB_AUTH_SUCCESS')]
    assert calls[0]['reply_markup'].inline_keyboard == []
    assert [entry['log_level'] for entry in captured] == ['warning']


@pytest.mark.asyncio
async def test_the_confirmation_arrives_even_if_the_button_is_refused(return_url, user, link):
    """Подтверждение не должно ехать на кнопке: тап ценен текстом, а не кнопкой.

    Адрес из настройки может оказаться негодным для Telegram и после нашей
    проверки — страховка остаётся на месте (#163).
    """
    calls: list[dict] = []
    message = _prompt(calls, refuses='keyboard')

    with capture_logs() as captured:
        await process_webauth_confirm(_tap(f'webauth_confirm:{TOKEN}', message), object())

    expected = get_texts('ru').t('WEB_AUTH_SUCCESS')
    assert [call['text'] for call in calls] == [expected, expected]
    assert len(calls[0]['reply_markup'].inline_keyboard) == 1
    assert calls[1]['reply_markup'].inline_keyboard == []
    assert [entry['log_level'] for entry in captured] == ['warning']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('data', 'key'),
    [
        ('webauth_deny', 'WEB_AUTH_DENIED'),
        ('webauth_confirm:too-short', 'WEB_AUTH_INVALID_TOKEN'),
    ],
)
async def test_a_sign_in_that_did_not_happen_offers_nothing_to_tap(data, key, return_url, user, link):
    """Обещать возврат там, где вход не состоялся, — неправда в одну кнопку."""
    calls: list[dict] = []
    message = _prompt(calls)

    with capture_logs() as captured:
        await process_webauth_confirm(_tap(data, message), object())

    assert [call['text'] for call in calls] == [get_texts('ru').t(key)]
    assert calls[0]['reply_markup'].inline_keyboard == [], 'возвращаться некуда'
    assert link.calls == [], 'неподтверждённый вход токен не связывает'
    assert captured == []


@pytest.mark.asyncio
async def test_an_inactive_account_is_told_so_and_gets_no_button(monkeypatch, return_url, link):
    async def _get_user(_db, _telegram_id):
        return _User(status=UserStatus.BLOCKED.value)

    monkeypatch.setattr(start_handler, 'get_user_by_telegram_id', _get_user)
    calls: list[dict] = []
    message = _prompt(calls)

    with capture_logs() as captured:
        await process_webauth_confirm(_tap(f'webauth_confirm:{TOKEN}', message), object())

    assert [call['text'] for call in calls] == [get_texts('ru').t('WEB_AUTH_INACTIVE_ACCOUNT')]
    assert calls[0]['reply_markup'].inline_keyboard == []
    assert link.calls == []
    assert captured == []


@pytest.mark.asyncio
async def test_an_expired_token_is_reported_without_a_button(return_url, user, link):
    """Ссылка истекла — возвращаться некуда, даже если адрес возврата задан."""
    link.linked = False
    calls: list[dict] = []
    message = _prompt(calls)

    with capture_logs() as captured:
        await process_webauth_confirm(_tap(f'webauth_confirm:{TOKEN}', message), object())

    assert [call['text'] for call in calls] == [get_texts('ru').t('WEB_AUTH_EXPIRED')]
    assert calls[0]['reply_markup'].inline_keyboard == []
    assert captured == []


@pytest.mark.asyncio
async def test_an_edit_without_a_keyboard_clears_the_one_already_there():
    """Ради этого ответ и правится: иначе «Да, войти / Нет» остались бы под ним."""
    calls: list[dict] = []
    message = _prompt(calls)

    await edit_app_login_answer(message, 'текст', None)

    assert [call['text'] for call in calls] == ['текст']
    assert calls[0]['reply_markup'] is EMPTY_INLINE_KEYBOARD


@pytest.mark.asyncio
async def test_an_edit_whose_keyboard_is_refused_is_repeated_without_one(return_url):
    """Страховка общая с отправкой ответа: отказ на клавиатуре — не отказ тексту."""
    calls: list[dict] = []
    message = _prompt(calls, refuses='keyboard')
    keyboard = app_login_return_keyboard(get_texts('ru'))

    with capture_logs() as captured:
        await edit_app_login_answer(message, 'текст', keyboard)

    assert [call['text'] for call in calls] == ['текст', 'текст']
    assert calls[0]['reply_markup'] is keyboard
    assert calls[1]['reply_markup'].inline_keyboard == []
    assert [entry['log_level'] for entry in captured] == ['warning']


@pytest.mark.asyncio
async def test_a_plain_edit_is_sent_once_and_its_error_is_not_swallowed():
    calls: list[dict] = []
    message = _prompt(calls)
    await edit_app_login_answer(message, 'текст', None)
    assert len(calls) == 1

    # Без клавиатуры повторять нечего: ошибка уходит наверх, а не превращается
    # в молчание — исходы входа и так честные, и падение не должно быть тихим.
    refusing = _prompt(calls, refuses='always')
    with pytest.raises(TelegramBadRequest):
        await edit_app_login_answer(refusing, 'текст', None)


class _User:
    """От пользователя кабинетной ветке нужны язык, статус и id."""

    def __init__(self, language: str = 'ru', status: str = UserStatus.ACTIVE.value):
        self.id = USER_ID
        self.language = language
        self.status = status


class _FakeLink:
    """Связывание токена без Redis: запоминает вызов и отдаёт заданный исход."""

    def __init__(self):
        self.calls: list[dict] = []
        self.linked = True

    async def __call__(self, token: str, telegram_id: int, user_id: int) -> bool:
        self.calls.append({'token': token, 'telegram_id': telegram_id, 'user_id': user_id})
        return self.linked


@pytest.fixture
def user(monkeypatch):
    """Пользователь находится без БД: под запросом здесь нечего проверять."""
    found = _User()

    async def _get_user(_db, _telegram_id):
        return found

    monkeypatch.setattr(start_handler, 'get_user_by_telegram_id', _get_user)
    return found


@pytest.fixture
def link(monkeypatch):
    """Токен связывается без Redis: важно, что вызов есть и с чем он пришёл."""
    fake = _FakeLink()
    monkeypatch.setattr(start_handler, 'link_web_auth_token', fake)
    return fake


class _Tap(types.CallbackQuery):
    """Касание кнопки: `answer()` в тесте ничего не делает — бота у него нет."""

    async def answer(self, *args, **kwargs):
        return True


def _tap(data: str, message: types.Message) -> types.CallbackQuery:
    return _Tap(
        id='1',
        from_user=types.User(id=TELEGRAM_ID, is_bot=False, first_name='Owner'),
        chat_instance='1',
        data=data,
        message=message,
    )


def _prompt(calls: list[dict], refuses: str = 'nothing') -> types.Message:
    """Сообщение-приглашение подтвердить вход; правки складываются в `calls`.

    Наследник `types.Message`, а не заглушка: обработчик проверяет
    `isinstance(callback.message, types.Message)`, и подделка его не прошла бы.
    Бота у сообщения нет — вместо вызова Telegram правка записывается в список.

    `refuses='keyboard'` повторяет отказ Telegram на непонравившейся клавиатуре
    (так было с `karvpn://login` до #163): правка отвергается целиком, текста
    человек не видит. `refuses='always'` — отказ без всякой клавиатуры.
    """

    class _Prompt(types.Message):
        async def edit_text(self, text, reply_markup=None):
            calls.append({'text': text, 'reply_markup': reply_markup})
            if refuses == 'always' or (
                refuses == 'keyboard' and reply_markup is not None and reply_markup.inline_keyboard
            ):
                raise TelegramBadRequest(
                    method=EditMessageText(chat_id=TELEGRAM_ID, message_id=1, text=text),
                    message='Bad Request: BUTTON_URL_INVALID',
                )
            return self

    return _Prompt(
        message_id=1,
        date=datetime.now(UTC),
        chat=types.Chat(id=TELEGRAM_ID, type='private'),
        text='🔐 Подтвердите вход в личный кабинет...',
    )
