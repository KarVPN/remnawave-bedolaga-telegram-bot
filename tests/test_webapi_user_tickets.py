"""Машинный доступ к поддержке (#192): обращения от имени человека по telegram_id.

Живого приложения, BFF и кабинета в окружении тестов нет, поэтому проверяется
контракт, по которому дальше сделают BFF и приложение: пути, коды ответов,
границы полей (как у кабинетных роутов), отказ в доступе к чужому обращению и
то, что сообщения в этих роутах всегда от человека, а не от админа.

База данных не поднимается: маршруты работают через CRUD, и здесь он заменён
на память. Проверяется именно то, что видит вызывающий — ключ, путь, тело,
код и форма ответа.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.cabinet.routes import websocket as cabinet_websocket
from app.cabinet.schemas.tickets import (
    TicketCreateRequest as CabinetTicketCreateRequest,
    TicketDetailResponse as CabinetTicketDetailResponse,
    TicketMessageCreateRequest as CabinetTicketMessageCreateRequest,
    TicketMessageResponse as CabinetTicketMessageResponse,
    TicketResponse as CabinetTicketResponse,
)
from app.config import settings
from app.database.crud.ticket import TicketCRUD, TicketMessageCRUD
from app.database.crud.ticket_notification import TicketNotificationCRUD
from app.database.models import Ticket, TicketMessage, User
from app.handlers import tickets as handlers_tickets
from app.services.web_api_token_service import web_api_token_service
from app.webapi.dependencies import get_db_session, require_api_token
from app.webapi.routes import user_tickets as user_tickets_route
from app.webapi.schemas.user_tickets import (
    UserTicketCreateRequest,
    UserTicketDetailResponse,
    UserTicketMessageCreateRequest,
    UserTicketMessageResponse,
    UserTicketSummaryResponse,
)


MACHINE_KEY = 'machine-key-for-tests'
TELEGRAM_ID = 895225
OTHER_TELEGRAM_ID = 111222
INTERNAL_USER_ID = 41
OTHER_INTERNAL_USER_ID = 42
NOW = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)

BASE = f'/tickets/by-telegram-id/{TELEGRAM_ID}'
FIRST_MESSAGE = 'Подключение не устанавливается уже час'


class _FakeSession:
    """Сессия-заглушка: маршрутам нужны только commit и rollback."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, *_args, **_kwargs) -> None:
        return None

    async def flush(self) -> None:
        return None

    def add(self, _instance) -> None:
        return None


def _authorised_token() -> SimpleNamespace:
    """Токен, который вернула бы проверка машинного ключа."""
    return SimpleNamespace(id=1, name='test token', is_active=True)


class _Store:
    """Тикеты и пользователи в памяти вместо живой БД."""

    def __init__(self) -> None:
        self.users: dict[int, User] = {}
        self.tickets: dict[int, Ticket] = {}
        # Сообщения, созданные через машинные роуты.
        self.created_messages: list[TicketMessage] = []
        self.query_calls: list[tuple[str, dict]] = []
        self.telegram_notifications: list[str] = []
        self.cabinet_notifications: list[str] = []
        self.cabinet_websocket_events: list[str] = []
        self._ticket_seq = 0
        self._message_seq = 0

    def add_user(self, telegram_id: int, user_id: int) -> User:
        user = User(id=user_id, telegram_id=telegram_id, username=f'user{user_id}', first_name='Test')
        self.users[telegram_id] = user
        return user

    def new_message(
        self,
        *,
        user: User,
        ticket_id: int,
        text: str = 'Не приходит подключение',
        is_from_admin: bool = False,
        media_file_id: str | None = None,
    ) -> TicketMessage:
        self._message_seq += 1
        return TicketMessage(
            id=self._message_seq,
            ticket_id=ticket_id,
            user_id=user.id,
            message_text=text,
            is_from_admin=is_from_admin,
            has_media=bool(media_file_id),
            media_type='photo' if media_file_id else None,
            media_file_id=media_file_id,
            media_caption=None,
            created_at=NOW + timedelta(seconds=self._message_seq),
        )

    def add_ticket(
        self,
        user: User,
        *,
        ticket_id: int,
        title: str = 'Проблема с подпиской',
        status_value: str = 'open',
        reply_block_permanent: bool = False,
        messages: list[TicketMessage] | None = None,
    ) -> Ticket:
        ticket = Ticket(
            id=ticket_id,
            user_id=user.id,
            title=title,
            status=status_value,
            priority='normal',
            user_reply_block_permanent=reply_block_permanent,
            user_reply_block_until=None,
            created_at=NOW,
            updated_at=NOW,
            closed_at=NOW if status_value == 'closed' else None,
        )
        ticket.messages = messages if messages is not None else [self.new_message(user=user, ticket_id=ticket_id)]
        self.tickets[ticket_id] = ticket
        self._ticket_seq = max(self._ticket_seq, ticket_id)
        return ticket


@pytest.fixture(autouse=True)
def _support_enabled(monkeypatch):
    """Обращения включены: иначе проверялся бы отказ, а не контракт."""
    monkeypatch.setattr(type(settings), 'is_support_tickets_enabled', lambda _settings: True)


@pytest.fixture
def store(monkeypatch) -> _Store:
    """Подменяет CRUD и уведомления: маршруты проверяются без БД и без Telegram."""
    store = _Store()
    module = user_tickets_route

    def person(user_id: int) -> User:
        return next(user for user in store.users.values() if user.id == user_id)

    async def get_user_by_telegram_id(_db, telegram_id):
        return store.users.get(telegram_id)

    async def get_user_tickets(_db, user_id, status=None, limit=20, offset=0, *, load_messages=False):
        store.query_calls.append(
            (
                'get_user_tickets',
                {
                    'user_id': user_id,
                    'status': status,
                    'limit': limit,
                    'offset': offset,
                    'load_messages': load_messages,
                },
            )
        )
        items = [ticket for ticket in store.tickets.values() if ticket.user_id == user_id]
        if status:
            items = [ticket for ticket in items if ticket.status == status]
        items.sort(key=lambda ticket: ticket.updated_at, reverse=True)
        return items[offset : offset + limit]

    async def count_user_tickets_by_statuses(_db, user_id, statuses):
        items = [ticket for ticket in store.tickets.values() if ticket.user_id == user_id]
        if statuses:
            items = [ticket for ticket in items if ticket.status in statuses]
        return len(items)

    async def get_ticket_by_id(_db, ticket_id, load_messages=True, load_user=False):
        return store.tickets.get(ticket_id)

    async def create_ticket(
        _db,
        user_id,
        title,
        message_text,
        priority='normal',
        *,
        media_type=None,
        media_file_id=None,
        media_caption=None,
    ):
        store._ticket_seq += 1
        author = person(user_id)
        ticket = store.add_ticket(author, ticket_id=store._ticket_seq, title=title, messages=[])
        message = store.new_message(user=author, ticket_id=ticket.id, text=message_text, media_file_id=media_file_id)
        message.media_type = media_type
        message.media_caption = media_caption
        ticket.messages = [message]
        store.created_messages.append(message)
        return ticket

    async def add_message(
        _db,
        ticket_id,
        user_id,
        message_text,
        is_from_admin=False,
        media_type=None,
        media_file_id=None,
        media_caption=None,
    ):
        author = person(user_id)
        message = store.new_message(user=author, ticket_id=ticket_id, text=message_text, media_file_id=media_file_id)
        message.media_type = media_type
        message.media_caption = media_caption
        store.created_messages.append(message)

        ticket = store.tickets[ticket_id]
        ticket.messages.append(message)
        if not is_from_admin and ticket.status != 'closed':
            ticket.status = 'open'
        return message

    async def notify_admins_about_new_ticket(ticket, _db):
        store.telegram_notifications.append(f'new:{ticket.id}')

    async def notify_admins_about_ticket_reply(ticket, reply_text, _db, *, media_file_id=None, media_type=None):
        store.telegram_notifications.append(f'reply:{ticket.id}:{reply_text}')

    async def create_admin_notification_for_new_ticket(_db, ticket):
        store.cabinet_notifications.append(f'new:{ticket.id}')
        return SimpleNamespace(id=1)

    async def create_admin_notification_for_user_reply(_db, ticket, _preview):
        store.cabinet_notifications.append(f'reply:{ticket.id}')
        return SimpleNamespace(id=2)

    async def notify_admins_new_ticket(ticket_id, _title, _user_id):
        store.cabinet_websocket_events.append(f'new:{ticket_id}')

    async def notify_admins_ticket_reply(ticket_id, _message, _user_id):
        store.cabinet_websocket_events.append(f'reply:{ticket_id}')

    monkeypatch.setattr(module, 'get_user_by_telegram_id', get_user_by_telegram_id)
    monkeypatch.setattr(TicketCRUD, 'get_user_tickets', staticmethod(get_user_tickets))
    monkeypatch.setattr(TicketCRUD, 'count_user_tickets_by_statuses', staticmethod(count_user_tickets_by_statuses))
    monkeypatch.setattr(TicketCRUD, 'get_ticket_by_id', staticmethod(get_ticket_by_id))
    monkeypatch.setattr(TicketCRUD, 'create_ticket', staticmethod(create_ticket))
    monkeypatch.setattr(TicketMessageCRUD, 'add_message', staticmethod(add_message))
    monkeypatch.setattr(handlers_tickets, 'notify_admins_about_new_ticket', notify_admins_about_new_ticket)
    monkeypatch.setattr(handlers_tickets, 'notify_admins_about_ticket_reply', notify_admins_about_ticket_reply)
    monkeypatch.setattr(
        TicketNotificationCRUD,
        'create_admin_notification_for_new_ticket',
        staticmethod(create_admin_notification_for_new_ticket),
    )
    monkeypatch.setattr(
        TicketNotificationCRUD,
        'create_admin_notification_for_user_reply',
        staticmethod(create_admin_notification_for_user_reply),
    )
    monkeypatch.setattr(cabinet_websocket, 'notify_admins_new_ticket', notify_admins_new_ticket)
    monkeypatch.setattr(cabinet_websocket, 'notify_admins_ticket_reply', notify_admins_ticket_reply)

    return store


def _build_app(*, with_token: bool) -> FastAPI:
    app = FastAPI()
    app.include_router(user_tickets_route.router, prefix='/tickets')
    app.dependency_overrides[get_db_session] = _FakeSession
    if with_token:
        app.dependency_overrides[require_api_token] = _authorised_token
    return app


@pytest.fixture
def machine_app(store) -> FastAPI:
    """Приложение, где машинный ключ уже принят: проверяется сам контракт."""
    return _build_app(with_token=True)


@pytest.fixture
def unauthorised_app() -> FastAPI:
    """Приложение с настоящей проверкой ключа: проверяется отказ."""
    return _build_app(with_token=False)


async def _call(app: FastAPI, method: str, path: str, *, body=None, key: str | None = MACHINE_KEY, headers=None):
    request_headers = dict(headers or {})
    if key is not None:
        request_headers.setdefault('X-API-Key', key)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://bot') as client:
        return await client.request(method, path, json=body, headers=request_headers)


def _dependency_calls(dependant) -> set:
    """Все зависимости роута, включая вложенные: так видно, чем проверяется ключ."""
    calls = set()
    for sub_dependant in dependant.dependencies:
        calls.add(sub_dependant.call)
        calls |= _dependency_calls(sub_dependant)
    return calls


def _person_with_tickets(store: _Store) -> User:
    user = store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)
    store.add_ticket(user, ticket_id=7, title='Не работает VPN')
    store.add_ticket(user, ticket_id=8, title='Вопрос по оплате', status_value='closed')
    return user


class TestAuthorisation:
    """Машинный ключ — тот же, что у остального машинного API, и только он."""

    async def test_missing_machine_key_is_unauthorised(self, unauthorised_app):
        response = await _call(unauthorised_app, 'GET', BASE, key=None)

        assert response.status_code == 401
        assert response.json() == {'detail': 'Missing API key'}

    async def test_unknown_machine_key_is_unauthorised(self, unauthorised_app, monkeypatch):
        async def authenticate(_db, _api_key, remote_ip=None):
            return

        monkeypatch.setattr(web_api_token_service, 'authenticate', authenticate)

        response = await _call(unauthorised_app, 'GET', BASE, key='not-a-machine-key')

        assert response.status_code == 401
        assert response.json() == {'detail': 'Invalid or expired API key'}

    async def test_cabinet_user_token_does_not_open_machine_routes(self, unauthorised_app, monkeypatch):
        """Кабинетный JWT в Authorization — не машинный ключ и не подходит."""
        seen_keys: list[str] = []

        async def authenticate(_db, api_key, remote_ip=None):
            seen_keys.append(api_key)

        monkeypatch.setattr(web_api_token_service, 'authenticate', authenticate)

        response = await _call(
            unauthorised_app,
            'GET',
            BASE,
            key=None,
            headers={'Authorization': 'Bearer cabinet-user-jwt'},
        )

        assert response.status_code == 401
        assert seen_keys == ['cabinet-user-jwt']

    def test_every_route_uses_the_shared_machine_key_dependency(self):
        """Новая зависимость авторизации не заводится: у всех роутов та же проверка ключа."""
        for route in user_tickets_route.router.routes:
            assert require_api_token in _dependency_calls(route.dependant)


class TestListing:
    """Список обращений человека: только его собственные."""

    async def test_lists_only_the_named_persons_tickets(self, machine_app, store):
        _person_with_tickets(store)
        other = store.add_user(OTHER_TELEGRAM_ID, OTHER_INTERNAL_USER_ID)
        store.add_ticket(other, ticket_id=9, title='Чужое обращение')

        response = await _call(machine_app, 'GET', BASE)

        assert response.status_code == 200
        payload = response.json()
        assert {item['id'] for item in payload['items']} == {7, 8}
        assert payload['total'] == 2
        assert payload['limit'] == 50
        assert payload['offset'] == 0

    async def test_list_item_carries_the_last_message(self, machine_app, store):
        user = store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)
        store.add_ticket(
            user,
            ticket_id=7,
            messages=[
                store.new_message(user=user, ticket_id=7, text='Первое сообщение'),
                store.new_message(user=user, ticket_id=7, text='Последнее сообщение'),
            ],
        )

        response = await _call(machine_app, 'GET', BASE)

        item = response.json()['items'][0]
        assert item['messages_count'] == 2
        assert item['last_message']['message_text'] == 'Последнее сообщение'

    async def test_status_filter_and_pagination_reach_the_crud(self, machine_app, store):
        _person_with_tickets(store)

        response = await _call(machine_app, 'GET', f'{BASE}?status=open&limit=10&offset=5')

        assert response.status_code == 200
        assert response.json()['total'] == 1
        assert store.query_calls[-1][1] == {
            'user_id': INTERNAL_USER_ID,
            'status': 'open',
            'limit': 10,
            'offset': 5,
            'load_messages': True,
        }

    async def test_invalid_status_is_rejected(self, machine_app, store):
        _person_with_tickets(store)

        response = await _call(machine_app, 'GET', f'{BASE}?status=спам')

        assert response.status_code == 422

    async def test_unknown_person_is_not_found(self, machine_app, store):
        response = await _call(machine_app, 'GET', BASE)

        assert response.status_code == 404
        assert response.json() == {'detail': 'User not found'}

    async def test_support_disabled_answers_forbidden(self, machine_app, store, monkeypatch):
        _person_with_tickets(store)
        monkeypatch.setattr(type(settings), 'is_support_tickets_enabled', lambda _settings: False)

        response = await _call(machine_app, 'GET', BASE)

        assert response.status_code == 403
        assert response.json() == {'detail': 'Support tickets are disabled'}


class TestCreating:
    """Создание обращения от имени человека."""

    async def test_creates_ticket_with_the_persons_first_message(self, machine_app, store):
        store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)

        response = await _call(
            machine_app,
            'POST',
            BASE,
            body={'title': 'Не работает VPN', 'message': FIRST_MESSAGE},
        )

        assert response.status_code == 201
        payload = response.json()
        assert payload['title'] == 'Не работает VPN'
        assert payload['status'] == 'open'
        assert payload['priority'] == 'normal'
        assert payload['messages_count'] == 1
        assert payload['messages'][0]['message_text'] == FIRST_MESSAGE
        assert payload['messages'][0]['is_from_admin'] is False

        stored = store.tickets[payload['id']]
        assert stored.user_id == INTERNAL_USER_ID
        assert store.created_messages[0].is_from_admin is False
        assert store.created_messages[0].user_id == INTERNAL_USER_ID

    async def test_creation_notifies_moderators_in_both_channels(self, machine_app, store):
        store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)

        response = await _call(machine_app, 'POST', BASE, body={'title': 'Не работает VPN', 'message': FIRST_MESSAGE})

        ticket_id = response.json()['id']
        assert store.telegram_notifications == [f'new:{ticket_id}']
        assert store.cabinet_notifications == [f'new:{ticket_id}']
        assert store.cabinet_websocket_events == [f'new:{ticket_id}']

    async def test_media_is_passed_through_like_the_cabinet(self, machine_app, store):
        store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)

        response = await _call(
            machine_app,
            'POST',
            BASE,
            body={
                'title': 'Скриншот ошибки',
                'message': 'Прикладываю скриншот с ошибкой подключения',
                'media_type': 'photo',
                'media_file_id': 'AgACAgIAAxkBAAI',
                'media_caption': 'Ошибка',
            },
        )

        assert response.status_code == 201
        message = response.json()['messages'][0]
        assert message['has_media'] is True
        assert message['media_file_id'] == 'AgACAgIAAxkBAAI'
        assert message['media_caption'] == 'Ошибка'

    async def test_unknown_person_creates_nothing(self, machine_app, store):
        response = await _call(machine_app, 'POST', BASE, body={'title': 'Не работает VPN', 'message': FIRST_MESSAGE})

        assert response.status_code == 404
        assert response.json() == {'detail': 'User not found'}
        assert store.tickets == {}

    async def test_support_disabled_answers_forbidden(self, machine_app, store, monkeypatch):
        store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)
        monkeypatch.setattr(type(settings), 'is_support_tickets_enabled', lambda _settings: False)

        response = await _call(machine_app, 'POST', BASE, body={'title': 'Не работает VPN', 'message': FIRST_MESSAGE})

        assert response.status_code == 403
        assert store.tickets == {}


class TestReading:
    """Чтение обращения: своё видно, чужое — «не найдено»."""

    async def test_reads_own_ticket_with_messages(self, machine_app, store):
        user = store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)
        store.add_ticket(
            user,
            ticket_id=7,
            messages=[
                store.new_message(user=user, ticket_id=7, text='Первое сообщение'),
                store.new_message(user=user, ticket_id=7, text='Ответ поддержки', is_from_admin=True),
            ],
        )

        response = await _call(machine_app, 'GET', f'{BASE}/7')

        assert response.status_code == 200
        payload = response.json()
        assert payload['id'] == 7
        assert [message['message_text'] for message in payload['messages']] == [
            'Первое сообщение',
            'Ответ поддержки',
        ]
        assert payload['messages'][1]['is_from_admin'] is True

    async def test_someone_elses_ticket_is_not_found_not_forbidden(self, machine_app, store):
        store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)
        other = store.add_user(OTHER_TELEGRAM_ID, OTHER_INTERNAL_USER_ID)
        store.add_ticket(other, ticket_id=9, title='Чужое обращение')

        response = await _call(machine_app, 'GET', f'{BASE}/9')

        assert response.status_code == 404
        assert response.json() == {'detail': 'Ticket not found'}

    async def test_missing_ticket_answers_exactly_like_a_foreign_one(self, machine_app, store):
        store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)

        response = await _call(machine_app, 'GET', f'{BASE}/999')

        assert response.status_code == 404
        assert response.json() == {'detail': 'Ticket not found'}


class TestAddingMessages:
    """Продолжение обращения — сообщением от человека."""

    async def test_person_message_is_stored_as_a_person_message(self, machine_app, store):
        user = store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)
        store.add_ticket(user, ticket_id=7, status_value='answered')

        response = await _call(
            machine_app,
            'POST',
            f'{BASE}/7/messages',
            body={'message': 'Проблема осталась, подключение не работает'},
        )

        assert response.status_code == 201
        assert response.json()['is_from_admin'] is False
        assert response.json()['message_text'] == 'Проблема осталась, подключение не работает'
        assert store.created_messages[-1].is_from_admin is False
        assert store.tickets[7].status == 'open'
        assert store.telegram_notifications == ['reply:7:Проблема осталась, подключение не работает']
        assert store.cabinet_notifications == ['reply:7']

    async def test_closed_ticket_answers_like_the_cabinet(self, machine_app, store):
        user = store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)
        store.add_ticket(user, ticket_id=8, status_value='closed')

        response = await _call(machine_app, 'POST', f'{BASE}/8/messages', body={'message': 'И всё-таки не работает'})

        assert response.status_code == 400
        assert response.json() == {'detail': 'Cannot add message to closed ticket'}
        assert store.created_messages == []

    async def test_blocked_ticket_refuses_the_message(self, machine_app, store):
        user = store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)
        store.add_ticket(user, ticket_id=7, reply_block_permanent=True)

        response = await _call(machine_app, 'POST', f'{BASE}/7/messages', body={'message': 'Ответьте пожалуйста'})

        assert response.status_code == 403
        assert response.json() == {'detail': 'Replies to this ticket are blocked'}
        assert store.created_messages == []

    async def test_message_to_someone_elses_ticket_is_not_found(self, machine_app, store):
        store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)
        other = store.add_user(OTHER_TELEGRAM_ID, OTHER_INTERNAL_USER_ID)
        store.add_ticket(other, ticket_id=9)

        response = await _call(machine_app, 'POST', f'{BASE}/9/messages', body={'message': 'Влезу в чужое обращение'})

        assert response.status_code == 404
        assert response.json() == {'detail': 'Ticket not found'}
        assert store.created_messages == []


class TestFieldBounds:
    """Границы полей — как у кабинетных роутов: title 3..255, message 10..4000."""

    def _bounds(self, model, field: str) -> tuple[int | None, int | None]:
        minimum = maximum = None
        for meta in model.model_fields[field].metadata:
            minimum = getattr(meta, 'min_length', minimum)
            maximum = getattr(meta, 'max_length', maximum)
        return minimum, maximum

    @pytest.mark.parametrize('field', ['title', 'message', 'media_caption'])
    def test_creation_bounds_match_the_cabinet(self, field):
        assert self._bounds(UserTicketCreateRequest, field) == self._bounds(CabinetTicketCreateRequest, field)

    @pytest.mark.parametrize('field', ['message', 'media_caption'])
    def test_message_bounds_match_the_cabinet(self, field):
        assert self._bounds(UserTicketMessageCreateRequest, field) == self._bounds(
            CabinetTicketMessageCreateRequest, field
        )

    @pytest.mark.parametrize(
        ('body', 'reason'),
        [
            ({'title': 'ab', 'message': 'Подключение не устанавливается'}, 'title is too short'),
            ({'title': 'a' * 256, 'message': 'Подключение не устанавливается'}, 'title is too long'),
            ({'title': 'Не работает VPN', 'message': 'a' * 9}, 'message is too short'),
            ({'title': 'Не работает VPN', 'message': 'a' * 4001}, 'message is too long'),
        ],
    )
    async def test_creation_rejects_out_of_bounds(self, machine_app, store, body, reason):
        store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)

        response = await _call(machine_app, 'POST', BASE, body=body)

        assert response.status_code == 422, reason
        assert store.tickets == {}

    @pytest.mark.parametrize(
        ('title', 'message'),
        [('abc', 'a' * 10), ('a' * 255, 'a' * 4000)],
    )
    async def test_creation_accepts_the_exact_bounds(self, machine_app, store, title, message):
        store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)

        response = await _call(machine_app, 'POST', BASE, body={'title': title, 'message': message})

        assert response.status_code == 201
        assert response.json()['title'] == title

    @pytest.mark.parametrize('message', ['', 'a' * 4001])
    async def test_message_rejects_out_of_bounds(self, machine_app, store, message):
        user = store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)
        store.add_ticket(user, ticket_id=7)

        response = await _call(machine_app, 'POST', f'{BASE}/7/messages', body={'message': message})

        assert response.status_code == 422
        assert store.created_messages == []

    async def test_message_accepts_the_exact_bound(self, machine_app, store):
        user = store.add_user(TELEGRAM_ID, INTERNAL_USER_ID)
        store.add_ticket(user, ticket_id=7)

        response = await _call(machine_app, 'POST', f'{BASE}/7/messages', body={'message': 'a' * 4000})

        assert response.status_code == 201


class TestContract:
    """Контракт для BFF и приложения: ровно эти пути и форма, как у кабинета."""

    def test_router_exposes_only_person_scoped_routes(self):
        routes = {route.path for route in user_tickets_route.router.routes}

        assert routes == {
            '/by-telegram-id/{telegram_id}',
            '/by-telegram-id/{telegram_id}/{ticket_id}',
            '/by-telegram-id/{telegram_id}/{ticket_id}/messages',
        }

    def test_router_has_no_admin_capabilities(self):
        """Ответы, статусы, приоритеты и блокировки остаются у админского API."""
        methods = {method for route in user_tickets_route.router.routes for method in getattr(route, 'methods', set())}
        paths = ' '.join(route.path for route in user_tickets_route.router.routes)

        assert methods == {'GET', 'POST'}
        for admin_ability in ('reply', 'status', 'priority', 'reply-block'):
            assert admin_ability not in paths

    def test_response_shape_matches_the_cabinet_user_schemas(self):
        assert set(UserTicketSummaryResponse.model_fields) == set(CabinetTicketResponse.model_fields)
        assert set(UserTicketMessageResponse.model_fields) == set(CabinetTicketMessageResponse.model_fields)
        assert set(CabinetTicketDetailResponse.model_fields) <= set(UserTicketDetailResponse.model_fields)
