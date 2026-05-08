import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.utils import user_utils


async def test_ensure_user_referral_code_creates_and_persists_missing_code(monkeypatch):
    db = SimpleNamespace(flush=AsyncMock())
    user = SimpleNamespace(id=42, telegram_id=123456, referral_code=None, updated_at=None)

    generate_mock = AsyncMock(return_value='refABC12345')
    monkeypatch.setattr(user_utils, 'generate_unique_referral_code', generate_mock)

    referral_code = await user_utils.ensure_user_referral_code(db, user)

    assert referral_code == 'refABC12345'
    assert user.referral_code == 'refABC12345'
    assert user.updated_at is not None
    generate_mock.assert_awaited_once_with(db, 123456)
    db.flush.assert_awaited_once()


async def test_ensure_user_referral_code_keeps_existing_code(monkeypatch):
    db = SimpleNamespace(flush=AsyncMock())
    user = SimpleNamespace(id=42, telegram_id=123456, referral_code='refEXISTING', updated_at=None)

    generate_mock = AsyncMock()
    monkeypatch.setattr(user_utils, 'generate_unique_referral_code', generate_mock)

    referral_code = await user_utils.ensure_user_referral_code(db, user)

    assert referral_code == 'refEXISTING'
    generate_mock.assert_not_awaited()
    db.flush.assert_not_awaited()
