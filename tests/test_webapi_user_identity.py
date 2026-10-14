"""Exact machine identity lookup is separate from the legacy ambiguous route."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.database.models import User
from app.webapi.dependencies import get_db_session, require_api_token
from app.webapi.routes import users as users_route


@pytest.fixture
def identity_app(monkeypatch):
    app = FastAPI()
    app.include_router(users_route.router, prefix='/users')

    async def fake_db():
        yield object()

    app.dependency_overrides[get_db_session] = fake_db
    app.dependency_overrides[require_api_token] = lambda: SimpleNamespace(id=1, name='test', is_active=True)
    return app


async def _get(app: FastAPI, path: str, *, key: str | None = 'machine-key'):
    headers = {'X-API-Key': key} if key else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://bot') as client:
        return await client.get(path, headers=headers)


class TestExactMachineIdentity:
    async def test_by_id_uses_only_the_cabinet_primary_key_and_returns_minimal_fields(self, identity_app, monkeypatch):
        calls: list[tuple[str, int]] = []
        account = User(id=41, telegram_id=None, status='active', remnawave_id=701, remnawave_uuid='legacy-uuid')

        async def get_by_id(_db, user_id):
            calls.append(('id', user_id))
            return account if user_id == 41 else None

        async def get_by_telegram_id(_db, telegram_id):
            calls.append(('telegram', telegram_id))

        monkeypatch.setattr(users_route, 'get_user_by_id', get_by_id)
        monkeypatch.setattr(users_route, 'get_user_by_telegram_id', get_by_telegram_id)

        response = await _get(identity_app, '/users/by-id/41')

        assert response.status_code == 200
        assert response.json() == {
            'id': 41,
            'telegram_id': None,
            'status': 'active',
            'remnawave_id': 701,
            'remnawave_uuid': None,
        }
        assert calls == [('id', 41)]

    async def test_id_collision_resolves_exact_internal_user(self, identity_app, monkeypatch):
        account = User(
            id=895225, telegram_id=None, status='active', remnawave_id=702, remnawave_uuid='email-panel-uuid'
        )
        unrelated_telegram_user = User(id=52, telegram_id=895225, status='blocked', remnawave_uuid='other-uuid')
        calls: list[str] = []

        async def get_by_id(_db, user_id):
            calls.append('id')
            return account if user_id == account.id else None

        async def get_by_telegram_id(_db, telegram_id):
            calls.append('telegram')
            return unrelated_telegram_user if telegram_id == 895225 else None

        monkeypatch.setattr(users_route, 'get_user_by_id', get_by_id)
        monkeypatch.setattr(users_route, 'get_user_by_telegram_id', get_by_telegram_id)

        response = await _get(identity_app, '/users/by-id/895225')

        assert response.status_code == 200
        assert response.json()['id'] == account.id
        assert response.json()['remnawave_id'] == 702
        assert response.json()['remnawave_uuid'] is None
        assert calls == ['id']

    async def test_unknown_exact_id_is_404(self, identity_app, monkeypatch):
        async def get_by_id(_db, _user_id):
            return None

        monkeypatch.setattr(users_route, 'get_user_by_id', get_by_id)

        response = await _get(identity_app, '/users/by-id/999')

        assert response.status_code == 404
        assert response.json() == {'detail': 'User not found'}

    async def test_exact_id_route_requires_the_machine_key(self, identity_app):
        identity_app.dependency_overrides.pop(require_api_token)

        response = await _get(identity_app, '/users/by-id/41', key=None)

        assert response.status_code == 401
