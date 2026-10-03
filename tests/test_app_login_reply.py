"""Ответ на вход из приложения: локализованный текст и кнопка возврата (#78).

Живого Telegram в тестах нет, поэтому проверяется то, что до него: ключи в
локалях (без них текст молча берётся из fallback, а каждый вход пишет
«Missing localization key» в лог), сам текст и клавиатура, которую получит чат.
"""

import json
import re
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from app.config import settings
from app.handlers.start import APP_LOGIN_MESSAGES, APP_LOGIN_RETURN_RESULTS, app_login_reply
from app.localization.loader import clear_locale_cache
from app.localization.texts import get_texts
from app.services.app_login_service import APP_LOGIN_RETURN_URL, AppLoginResult


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = PROJECT_ROOT / 'app' / 'localization' / 'locales'
HANDLER = PROJECT_ROOT / 'app' / 'handlers' / 'start.py'
LANGUAGES = ('ru', 'en')


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
        assert keyboard.inline_keyboard[0][0].text == locale['APP_LOGIN_RETURN_BUTTON']

    warnings = [entry for entry in captured if entry.get('log_level') == 'warning']
    assert warnings == [], warnings


def test_the_confirmation_itself_carries_the_way_back():
    """Случай владельца: вошёл — и одним касанием вернулся (#78)."""
    assert AppLoginResult.OK in APP_LOGIN_RETURN_RESULTS


@pytest.mark.parametrize('language', LANGUAGES)
def test_the_button_is_a_single_tap_to_the_app(language):
    texts = get_texts(language)
    text, keyboard = app_login_reply(AppLoginResult.OK, texts)

    assert text == texts.t('APP_LOGIN_OK')
    rows = keyboard.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1, 'кнопка возврата — одна и в своём ряду'
    button = rows[0][0]
    assert button.url == APP_LOGIN_RETURN_URL
    assert button.text == texts.t('APP_LOGIN_RETURN_BUTTON')
    assert button.callback_data is None, 'возврат открывает приложение, а не ещё один шаг в чате'


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
