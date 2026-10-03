"""Вход кодом из чата: шесть цифр вместо ссылки (#178).

Живого Telegram в тестах нет, поэтому проверяется то, что до него: разбор
сообщения, вызов подтверждения, текст ответа и кнопка возврата. Смысл правки —
человек, у которого Telegram не открывается на телефоне (а приложение
показывает код именно там), подтверждает вход с компьютера, отправив боту шесть
цифр обычным сообщением.

Главный риск — реферальные и промокоды: они разбираются из тех же сообщений.
Приоритет задан явно и проверяется здесь же: код входа берётся только в
свободном чате (`StateFilter(None)`), регистрация и её шаг промокода остаются
своим обработчикам. Форма промокода (`[A-Za-z0-9_-]{3,50}`) допускает `123456`,
поэтому без такого разделения рукописный промокод из шести цифр перестал бы
работать, а `123-456` перестал бы доходить до регистрации как реферальный.
"""

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Dispatcher, types
from aiogram.filters import StateFilter
from structlog.testing import capture_logs

from app.config import settings
from app.handlers import common as common_handler, start as start_handler
from app.handlers.start import (
    APP_LOGIN_CODE_MESSAGES,
    APP_LOGIN_CODE_RETURN_RESULTS,
    APP_LOGIN_MESSAGES,
    APP_LOGIN_RETURN_RESULTS,
    app_login_code_reply,
    app_login_reply,
    handle_app_login_code,
    handle_potential_referral_code,
    process_referral_code_input,
)
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts
from app.services.app_login_service import AppLoginResult, app_login_code
from app.states import RegistrationStates


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = PROJECT_ROOT / 'app' / 'localization' / 'locales'
LANGUAGES = ('ru', 'en')
RETURN_URL = 'https://app.test.yolgins.ru/app/open'
TELEGRAM_ID = 895225
CODE = '123456'


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


class TestAnswer:
    """Ответ на код: те же слова, что у ссылки, и кнопка только на состоявшемся входе."""

    def test_every_outcome_the_service_knows_has_a_text(self):
        assert set(APP_LOGIN_CODE_MESSAGES) == set(AppLoginResult)

    def test_success_uses_the_words_of_the_link_path(self):
        """Один вход — один ответ: успех говорит тем же текстом, что `/start login_<nonce>`."""
        assert APP_LOGIN_CODE_MESSAGES[AppLoginResult.OK] is APP_LOGIN_MESSAGES[AppLoginResult.OK]

    @pytest.mark.parametrize('language', LANGUAGES)
    def test_the_code_refusal_is_localized(self, language):
        """У кода своя формулировка: «ссылка устарела» — неправда про шесть цифр."""
        text, keyboard = app_login_code_reply(AppLoginResult.EXPIRED, get_texts(language))

        assert text == _locale(language)['APP_LOGIN_CODE_EXPIRED']
        assert keyboard is None

    def test_the_code_refusal_is_not_the_link_refusal(self):
        assert APP_LOGIN_CODE_MESSAGES[AppLoginResult.EXPIRED] != APP_LOGIN_MESSAGES[AppLoginResult.EXPIRED]

    @pytest.mark.parametrize('language', LANGUAGES)
    def test_the_localized_text_is_used_and_nothing_warns(self, language):
        """`texts.t(key)` без запасного текста: найдётся он только в локали."""
        locale = _locale(language)
        texts = get_texts(language)

        with capture_logs() as captured:
            for result, (key, _default) in APP_LOGIN_CODE_MESSAGES.items():
                assert texts.t(key) == locale[key], f'{language}: {key}'
                text, _ = app_login_code_reply(result, texts)
                assert text == locale[key], f'{language}: {key}'

        assert [entry for entry in captured if entry.get('log_level') == 'warning'] == []

    def test_a_worked_code_offers_the_way_back(self, return_url):
        """Случай владельца: подтвердил вход с компьютера — и одним касанием вернулся."""
        assert APP_LOGIN_CODE_RETURN_RESULTS == (AppLoginResult.OK,)

        texts = get_texts('ru')
        text, keyboard = app_login_code_reply(AppLoginResult.OK, texts)

        assert text == texts.t('APP_LOGIN_OK')
        rows = keyboard.inline_keyboard
        assert len(rows) == 1 and len(rows[0]) == 1, 'кнопка возврата — одна и в своём ряду'
        assert rows[0][0].url == return_url
        assert rows[0][0].text == texts.t('APP_LOGIN_RETURN_BUTTON')

    @pytest.mark.parametrize('result', set(AppLoginResult) - set(APP_LOGIN_CODE_RETURN_RESULTS))
    def test_no_button_where_the_login_did_not_happen(self, result, return_url):
        """Обещать возврат в приложение, которое ещё ждёт вход, — неправда в одну кнопку."""
        text, keyboard = app_login_code_reply(result, get_texts('ru'))

        assert keyboard is None
        assert text == get_texts('ru').t(APP_LOGIN_CODE_MESSAGES[result][0])

    def test_an_expired_nonce_keeps_the_button_but_an_expired_code_does_not(self, return_url):
        """Разные пути — разные обещания, и это решение, а не случайность.

        По ссылке человек стоит в приложении, которое не смогло войти: ему нужно
        вернуться и начать заново. С кодом он уже смотрит в приложение с другого
        устройства — возвращать его одним касанием некуда.
        """
        texts = get_texts('ru')

        assert AppLoginResult.EXPIRED in APP_LOGIN_RETURN_RESULTS
        assert app_login_reply(AppLoginResult.EXPIRED, texts)[1] is not None
        assert app_login_code_reply(AppLoginResult.EXPIRED, texts)[1] is None


class TestHandler:
    """Обработчик кода: подтверждение, ответ и язык — без живого Telegram."""

    @pytest.mark.asyncio
    async def test_a_worked_code_confirms_the_login_and_offers_the_way_back(self, confirm, return_url):
        message, calls = _message(f' {CODE} ')

        await handle_app_login_code(message, None)

        assert confirm.calls == [{'telegram_id': TELEGRAM_ID, 'code': CODE}], (
            'подтверждается код отправителя, а не что-то ещё'
        )
        assert [call['text'] for call in calls] == [get_texts(DEFAULT_LANGUAGE).t('APP_LOGIN_OK')]
        assert calls[0]['reply_markup'].inline_keyboard[0][0].url == return_url

    @pytest.mark.asyncio
    async def test_the_code_is_never_written_to_the_log(self, confirm, return_url):
        """Пока вход не подтверждён, код — учётные данные: в логе ему не место."""
        message, _calls = _message(CODE)

        with capture_logs() as captured:
            await handle_app_login_code(message, None)

        assert CODE not in json.dumps(captured, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_a_code_that_did_not_work_is_refused_without_a_button(self, confirm, return_url):
        confirm.result = AppLoginResult.EXPIRED
        message, calls = _message(CODE)

        await handle_app_login_code(message, None)

        assert [call['text'] for call in calls] == [
            get_texts(DEFAULT_LANGUAGE).t('APP_LOGIN_CODE_EXPIRED'),
        ]
        assert calls[0]['reply_markup'] is None, 'возвращаться некуда'

    @pytest.mark.asyncio
    @pytest.mark.parametrize('language', LANGUAGES)
    async def test_the_answer_is_in_the_language_of_the_person(self, confirm, language):
        confirm.result = AppLoginResult.EXPIRED
        message, calls = _message(CODE)

        await handle_app_login_code(message, _User(language=language))

        assert [call['text'] for call in calls] == [get_texts(language).t('APP_LOGIN_CODE_EXPIRED')]

    @pytest.mark.asyncio
    async def test_a_person_the_bot_does_not_know_yet_is_not_gated_here(self, confirm, return_url):
        """Своей проверки регистрации обработчик не добавляет — и это решение (#178).

        Кто дойдёт до обработчика, решают другие: `AuthMiddleware` просит у
        незнакомого человека `/start`, а в самой регистрации шесть цифр остаются
        промокодом (проверено в `TestPriority`). Здесь подтверждение ровно такое
        же, как по ссылке `login_<nonce>`: решает BFF, он записывает telegram_id и
        про подписку не спрашивает.
        """
        message, calls = _message(CODE)

        await handle_app_login_code(message, None)

        assert confirm.calls == [{'telegram_id': TELEGRAM_ID, 'code': CODE}]
        assert [call['text'] for call in calls] == [get_texts(DEFAULT_LANGUAGE).t('APP_LOGIN_OK')]

    @pytest.mark.asyncio
    async def test_a_disabled_deployment_answers_honestly(self, confirm):
        confirm.result = AppLoginResult.DISABLED
        message, calls = _message(CODE)

        await handle_app_login_code(message, None)

        assert [call['text'] for call in calls] == [get_texts(DEFAULT_LANGUAGE).t('APP_LOGIN_DISABLED')]
        assert calls[0]['reply_markup'] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize('text', ['привет', '123-456', CODE + '1'])
    async def test_a_message_that_is_not_a_code_is_left_alone(self, confirm, text):
        """Страховка: без кода обработчик молчит, а не отвечает про несуществующий вход."""
        message, calls = _message(text)

        await handle_app_login_code(message, None)

        assert confirm.calls == []
        assert calls == []


class TestPriority:
    """Приоритет разбора: код входа — только свободный чат, регистрация — своя (#178).

    Фильтры берутся у настоящего `Dispatcher`, а не пересказываются в тесте:
    проверяется то, что действительно зарегистрировано (`register_handlers`), и
    то, в каком порядке это спросят у сообщения.
    """

    @pytest.mark.asyncio
    async def test_the_code_is_read_only_in_the_free_chat(self):
        handler = _registered(_dispatcher(), handle_app_login_code)
        message, _calls = _message(CODE)

        assert (await handler.check(message, raw_state=None))[0] is True

        for state in (
            RegistrationStates.waiting_for_language,
            RegistrationStates.waiting_for_rules_accept,
            RegistrationStates.waiting_for_privacy_policy_accept,
            RegistrationStates.waiting_for_referral_code,
        ):
            assert (await handler.check(message, raw_state=state.state))[0] is False, state.state

    @pytest.mark.asyncio
    @pytest.mark.parametrize('text', ['привет', '123-456', '12345', CODE + '7', 'refAB12cd34', None])
    async def test_only_a_code_passes_the_filter(self, text):
        """Свободный чат — не значит «любой текст»: фильтр пропускает только код."""
        handler = _registered(_dispatcher(), handle_app_login_code)
        message, _calls = _message(text)

        assert (await handler.check(message, raw_state=None))[0] is False

    @pytest.mark.asyncio
    async def test_the_filter_agrees_with_the_parser(self):
        """Фильтр и разбор — одна функция (`app_login_code`), а не две разные правды."""
        handler = _registered(_dispatcher(), handle_app_login_code)

        for text in (CODE, f' {CODE}\n', '\u00a0' + CODE, '123 456', '123-456', 'код ' + CODE):
            message, _calls = _message(text)
            expected = app_login_code(text) is not None

            assert (await handler.check(message, raw_state=None))[0] is expected, text

    def test_registration_keeps_its_own_handlers(self):
        """Шесть цифр в регистрации по-прежнему идут по её ветке, а не в BFF."""
        dp = _dispatcher()

        assert _states(_registered(dp, process_referral_code_input)) == (
            RegistrationStates.waiting_for_referral_code.state,
        )
        assert set(_states(_registered(dp, handle_potential_referral_code))) == {
            RegistrationStates.waiting_for_rules_accept.state,
            RegistrationStates.waiting_for_referral_code.state,
        }

    def test_the_code_is_asked_before_the_unknown_message(self):
        """Иначе шесть цифр получали бы «❓ Не понимаю эту команду» (#178).

        Порядок — тот же, что в `app/bot.py`: `start.register_handlers` вызывается
        раньше `common.register_handlers`, а ловящий «всё остальное» обработчик
        остаётся последним.
        """
        dp = Dispatcher()
        start_handler.register_handlers(dp)
        common_handler.register_handlers(dp)

        callbacks = [handler.callback for handler in dp.message.handlers]
        assert callbacks.index(handle_app_login_code) < callbacks.index(common_handler.handle_unknown_message)


class _FakeConfirm:
    """Подтверждение без BFF: запоминает вызов и отдаёт заданный исход."""

    def __init__(self, result: AppLoginResult = AppLoginResult.OK):
        self.calls: list[dict] = []
        self.result = result

    async def __call__(self, telegram_id: int, code: str) -> AppLoginResult:
        self.calls.append({'telegram_id': telegram_id, 'code': code})
        return self.result


@pytest.fixture
def confirm(monkeypatch):
    fake = _FakeConfirm()
    monkeypatch.setattr(start_handler, 'confirm_app_login_by_code', fake)
    return fake


class _User:
    """От пользователя этой ветке нужен только язык."""

    def __init__(self, language: str = 'ru'):
        self.language = language


def _message(text: str | None) -> tuple[types.Message, list[dict]]:
    """Сообщение с записанным ответом: бота у него нет, вызовы Telegram не уходят.

    Ответы складываются в список рядом с сообщением, а не в поле самого
    сообщения: `types.Message` — модель pydantic, и своих полей ей не добавить.
    """
    calls: list[dict] = []

    class _Incoming(types.Message):
        async def answer(self, answer_text, reply_markup=None):
            calls.append({'text': answer_text, 'reply_markup': reply_markup})
            return self

    message = _Incoming(
        message_id=1,
        date=datetime.now(UTC),
        chat=types.Chat(id=TELEGRAM_ID, type='private'),
        from_user=types.User(id=TELEGRAM_ID, is_bot=False, first_name='Owner'),
        text=text,
    )
    return message, calls


def _dispatcher() -> Dispatcher:
    dp = Dispatcher()
    start_handler.register_handlers(dp)
    return dp


def _registered(dp: Dispatcher, callback):
    """Зарегистрированный обработчик именно этой функции — вместе с его фильтрами."""
    for handler in dp.message.handlers:
        if inspect.unwrap(handler.callback) is callback:
            return handler

    raise AssertionError(f'{callback.__name__} не зарегистрирован')


def _states(handler) -> tuple[str | None, ...] | None:
    """Состояния из фильтра регистрации: чем обработчик ограничен на самом деле."""
    for event_filter in handler.filters:
        if isinstance(event_filter.callback, StateFilter):
            return event_filter.callback.states

    return None
