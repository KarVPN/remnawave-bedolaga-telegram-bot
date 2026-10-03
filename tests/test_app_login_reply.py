"""Ответ на вход из приложения: локализованный текст и кнопка возврата (#78, #163).

Живого Telegram в тестах нет, поэтому проверяется то, что до него: ключи в
локалях (без них текст молча берётся из fallback, а каждый вход пишет
«Missing localization key» в лог), сам текст и клавиатура, которую получит чат.

Кнопка существует только вместе с адресом возврата: Telegram принимает в
inline-кнопке лишь http(s) и отвергает сообщение целиком на любой другой схеме
(#163), поэтому без адреса ответ уходит текстом, а не без ответа.
"""

import json
import re
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from structlog.testing import capture_logs

from app.config import settings
from app.handlers.start import (
    APP_LOGIN_MESSAGES,
    APP_LOGIN_RETURN_RESULTS,
    app_login_reply,
    send_app_login_answer,
)
from app.localization.loader import clear_locale_cache
from app.localization.texts import get_texts
from app.services.app_login_service import AppLoginResult


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = PROJECT_ROOT / 'app' / 'localization' / 'locales'
HANDLER = PROJECT_ROOT / 'app' / 'handlers' / 'start.py'
LANGUAGES = ('ru', 'en')
RETURN_URL = 'https://app.test.yolgins.ru/app/open'


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


def test_the_handler_uses_exactly_the_outcomes_the_service_knows():
    """Каждый исход входа получает свой текст: молча пропущенного быть не должно."""
    assert set(APP_LOGIN_MESSAGES) == set(AppLoginResult)


@pytest.mark.parametrize('language', LANGUAGES)
def test_every_key_the_handler_names_is_in_the_locales(language):
    """Ключи из кода, а не из теста: список обновляется вместе с обработчиком."""
    keys = set(re.findall(r"'(APP_LOGIN_[A-Z_]+)'", HANDLER.read_text(encoding='utf-8')))
    assert keys, 'обработчик не называет ни одного ключа APP_LOGIN_*'

    locale = _locale(language)
    assert sorted(keys - set(locale)) == []


@pytest.mark.parametrize('language', LANGUAGES)
def test_the_localized_text_is_used_and_nothing_warns(language):
    """Ни fallback, ни предупреждения: ради этого ключи и добавлены (#78).

    `texts.t(key)` вызывается без запасного текста: найдётся он только в
    локали, а пропавший ключ отсюда — исключение, а не тихий откат на ru.
    """
    locale = _locale(language)
    texts = get_texts(language)

    with capture_logs() as captured:
        for result, (key, _default) in APP_LOGIN_MESSAGES.items():
            assert texts.t(key) == locale[key], f'{language}: {key}'
            text, _ = app_login_reply(result, texts)
            assert text == locale[key], f'{language}: {key}'

        _, keyboard = app_login_reply(AppLoginResult.OK, texts)
        # Без адреса возврата кнопки нет — и это не повод писать в лог.
        assert keyboard is None

    warnings = [entry for entry in captured if entry.get('log_level') == 'warning']
    assert warnings == [], warnings


def test_the_confirmation_itself_carries_the_way_back():
    """Случай владельца: вошёл — и одним касанием вернулся (#78)."""
    assert AppLoginResult.OK in APP_LOGIN_RETURN_RESULTS


@pytest.mark.parametrize('language', LANGUAGES)
def test_the_button_is_a_single_tap_to_the_app(language, return_url):
    texts = get_texts(language)
    text, keyboard = app_login_reply(AppLoginResult.OK, texts)

    assert text == texts.t('APP_LOGIN_OK')
    rows = keyboard.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1, 'кнопка возврата — одна и в своём ряду'
    button = rows[0][0]
    assert button.url == return_url
    assert button.text == texts.t('APP_LOGIN_RETURN_BUTTON')
    assert button.callback_data is None, 'возврат открывает приложение, а не ещё один шаг в чате'


@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('result', APP_LOGIN_RETURN_RESULTS)
def test_no_return_url_means_no_button_and_a_text_without_it(language, result):
    """Пустая настройка — не негодная ссылка, а её отсутствие (#163).

    Telegram отвергает сообщение вместе с клавиатурой, поэтому негодная ссылка
    стоит всего ответа: без адреса возврата текст уходит один.
    """
    texts = get_texts(language)

    with capture_logs() as captured:
        text, keyboard = app_login_reply(result, texts)

    assert keyboard is None
    assert text == texts.t(APP_LOGIN_MESSAGES[result][0])
    assert captured == [], 'отсутствие настройки — не предупреждение'


@pytest.mark.parametrize(
    'scheme_url',
    ['karvpn://login', 'tg://resolve?domain=karvpn', 'ftp://app.test.yolgins.ru/app/open'],
)
def test_a_scheme_telegram_refuses_is_not_put_into_the_button(scheme_url, monkeypatch):
    """Схему приложения в переменной прописали — кнопки всё равно не будет.

    Ровно на `karvpn://login` Telegram ответил `Bad Request: inline keyboard
    button URL 'karvpn://login' is invalid: Unsupported URL protocol`; отдать
    такую ссылку снова значило бы потерять весь ответ.
    """
    monkeypatch.setattr(settings, 'KARVPN_APP_LOGIN_RETURN_URL', scheme_url, raising=False)

    with capture_logs() as captured:
        text, keyboard = app_login_reply(AppLoginResult.OK, get_texts('ru'))

    assert keyboard is None
    assert text == get_texts('ru').t('APP_LOGIN_OK')
    assert [entry['log_level'] for entry in captured] == ['warning'], 'о причине должно быть видно в логе'


@pytest.mark.asyncio
async def test_the_text_arrives_even_when_there_is_no_button_to_send():
    """Текст не едет на кнопке: без неё ответ всё равно уходит, и один раз."""
    texts = get_texts('ru')
    text, keyboard = app_login_reply(AppLoginResult.OK, texts)
    message = _FakeMessage()

    await send_app_login_answer(message, text, keyboard)

    assert [call['text'] for call in message.calls] == [text]
    assert message.calls[0]['reply_markup'] is None
    assert text == texts.t('APP_LOGIN_OK')


@pytest.mark.parametrize('result', set(AppLoginResult) - set(APP_LOGIN_RETURN_RESULTS))
def test_no_button_where_the_app_is_not_waiting(result):
    """Обещать возврат там, где вход не состоялся, — неправда в одну кнопку."""
    text, keyboard = app_login_reply(result, get_texts('ru'))

    assert keyboard is None
    assert text == get_texts('ru').t(APP_LOGIN_MESSAGES[result][0])


def test_a_stale_working_copy_of_the_locales_does_not_hide_the_keys(tmp_path, monkeypatch):
    """Рабочие локали контейнера — своя копия (`LOCALES_PATH`), засеянная из образа.

    Копия, сделанная до этих ключей, не должна их прятать: загрузчик сливает
    файл из образа с рабочим, и новый ключ приходит из образа — то есть после
    обновления образа вход перестаёт писать «Missing localization key» и без
    правки `./locales` на контуре (об этом же шаге развёртывания — в PR).
    """
    stale = tmp_path / 'ru.json'
    stale.write_text(json.dumps({'ACCESS_DENIED': 'из старой копии'}, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(settings, 'LOCALES_PATH', str(tmp_path), raising=False)
    clear_locale_cache()
    try:
        texts = get_texts('ru')
        assert texts.t('APP_LOGIN_OK') == _locale('ru')['APP_LOGIN_OK']
        # А то, что в рабочей копии есть, по-прежнему перекрывает образ.
        assert texts.t('ACCESS_DENIED') == 'из старой копии'
    finally:
        clear_locale_cache()


class _FakeMessage:
    """От `Message` нужен только `answer`: больше бот в этой ветке не делает.

    `refuses='keyboard'` повторяет разом и сервер, и клиент: Telegram принимает
    сообщение вместе с клавиатурой и на непонравившемся `url` отказывает целиком,
    а сообщение при этом не отправляется. `refuses='always'` — отказ без всякой
    клавиатуры: так падает, например, слишком длинный текст.
    """

    def __init__(self, refuses: str = 'nothing'):
        self.calls: list[dict] = []
        self.refuses = refuses

    async def answer(self, text, reply_markup=None):
        self.calls.append({'text': text, 'reply_markup': reply_markup})
        if self.refuses == 'always' or (self.refuses == 'keyboard' and reply_markup is not None):
            raise TelegramBadRequest(
                method=SendMessage(chat_id=1, text=text),
                message='Bad Request: BUTTON_URL_INVALID',
            )


@pytest.mark.asyncio
async def test_the_confirmation_arrives_even_if_the_button_is_refused(return_url):
    """Подтверждение не должно ехать на кнопке: тап ценен текстом, а не кнопкой.

    Адрес из настройки может оказаться негодным для Telegram и после нашей
    проверки — страховка остаётся на месте (#163).
    """
    texts = get_texts('ru')
    text, keyboard = app_login_reply(AppLoginResult.OK, texts)
    message = _FakeMessage(refuses='keyboard')

    with capture_logs() as captured:
        await send_app_login_answer(message, text, keyboard)

    assert [call['text'] for call in message.calls] == [text, text]
    assert message.calls[0]['reply_markup'] is keyboard
    assert message.calls[1]['reply_markup'] is None
    assert [entry['log_level'] for entry in captured] == ['warning']


@pytest.mark.asyncio
async def test_a_text_only_answer_is_sent_once_and_its_error_is_not_swallowed():
    message = _FakeMessage()
    await send_app_login_answer(message, 'текст', None)
    assert len(message.calls) == 1

    # Без клавиатуры повторять нечего: ошибка уходит наверх, а не превращается
    # в молчание — исходы входа и так честные, и падение не должно быть тихим.
    refusing = _FakeMessage(refuses='always')
    with pytest.raises(TelegramBadRequest):
        await send_app_login_answer(refusing, 'текст', None)
    assert len(refusing.calls) == 1
