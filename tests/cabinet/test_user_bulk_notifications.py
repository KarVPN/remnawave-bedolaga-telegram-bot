"""Receipts must follow committed success and must not replay Telegram sends."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.cabinet.schemas.user_bulk_operations import UserBulkPreview
from app.config import settings
from app.database.models import Subscription, Tariff, User, UserBulkNotification, UserBulkOperation, UserBulkTarget
from app.services import user_bulk_notifications as delivery, user_bulk_operations as service
from tests.cabinet.test_user_bulk_operations import seed
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    Tariff.__table__,
    UserBulkOperation.__table__,
    UserBulkTarget.__table__,
    UserBulkNotification.__table__,
)


def request(**kwargs):
    values = dict(request_key=uuid4(), action='extend_subscription', days=7, notify_users=True)
    values.update(kwargs)
    return UserBulkPreview(**values)


@pytest.fixture
def bot(monkeypatch):
    import app.cabinet.routes.admin_bulk_actions as handlers

    monkeypatch.setattr(settings, 'BOT_TOKEN', '123:test')
    monkeypatch.setattr(settings, 'USERS_BULK_ITEM_DELAY', 0)
    monkeypatch.setattr(settings, 'USERS_BULK_BATCH_SIZE', 25)
    executor = AsyncMock(return_value=SimpleNamespace(success=True, message='Applied'))
    monkeypatch.setattr(handlers, '_execute_for_subscription', executor)
    sender = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=321)), session=SimpleNamespace(close=AsyncMock())
    )
    monkeypatch.setattr(delivery, 'create_bot', lambda: sender)
    return sender, executor


async def test_one_receipt_after_all_subscriptions_and_no_replay(monkeypatch, bot):
    sender, executor = bot
    monkeypatch.setattr(settings, 'USERS_BULK_BATCH_SIZE', 1)
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        req = request(user_ids=[users[0].id], notification_message='Компенсация <за простой> & сервис')
        op = await service.preview(db, 7, req)
        assert await service.preview(db, 7, req) == op
        assert len((await db.scalars(select(UserBulkNotification))).all()) == 1
        sender.send_message.assert_not_awaited()
        await service.run_batch(db, op)
        sender.send_message.assert_not_awaited()  # second subscription still pending
        await service.run_batch(db, op)
        sender.send_message.assert_awaited_once()
        args, kwargs = sender.send_message.call_args
        assert args[0] == users[0].telegram_id
        assert 'подписки (2 шт.)' in args[1]
        assert '<code>продлена на 7 дней</code>' in args[1]
        assert 'Компенсация &lt;за простой&gt; &amp; сервис' in args[1]
        assert kwargs['parse_mode'] == 'HTML'
        assert kwargs['link_preview_options'].is_disabled
        result = await service.summary(db, op)
        assert result['notifications']['sent'] == 1
        assert result['state'] == 'completed'
        receipt = await db.scalar(select(UserBulkNotification))
        assert receipt.telegram_message_id == 321
        await service.run_batch(db, op)
        assert executor.await_count == 2
        assert sender.send_message.await_count == 1
        with pytest.raises(HTTPException) as exc:
            await service.preview(db, 7, req.model_copy(update={'notification_message': 'Changed'}))
        assert exc.value.status_code == 409


async def test_disabled_receipts_keep_existing_behavior(monkeypatch, bot):
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        op = await service.preview(db, 7, request(user_ids=[users[0].id], notify_users=False))
        await service.run_batch(db, op)
        bot[0].send_message.assert_not_awaited()
        assert (await service.summary(db, op))['notifications'] == {}


@pytest.mark.parametrize(
    'failure,expected', [('timeout', 'uncertain'), ('forbidden', 'failed'), ('rate_limit', 'limited')]
)
async def test_send_failure_never_repeats_changes_or_delivery(monkeypatch, bot, failure, expected):
    sender, executor = bot
    sender.send_message.side_effect = (
        TimeoutError()
        if failure == 'timeout'
        else TelegramForbiddenError(method=SendMessage(chat_id=1, text='test'), message='Forbidden')
    )
    if failure == 'rate_limit':
        sender.send_message.side_effect = TelegramRetryAfter(
            method=SendMessage(chat_id=1, text='test'), message='Retry', retry_after=30
        )
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        op = await service.preview(db, 7, request(user_ids=[users[1].id]))
        await service.run_batch(db, op)
        result = await service.summary(db, op)
        assert result['success'] == 1
        assert result['notifications'][expected] == 1
        assert result['state'] == 'needs_review'
        await service.run_batch(db, op)
        assert executor.await_count == sender.send_message.await_count == 1
        import app.cabinet.routes.user_bulk_operations as routes

        errors = await routes.notification_errors(operation_id=op, offset=0, admin=None, db=db)
        assert errors[0]['state'] == expected
        assert errors[0]['subscription_id'] is None


async def test_uncertain_subscription_does_not_claim_success(monkeypatch, bot):
    sender, executor = bot
    executor.side_effect = [
        SimpleNamespace(success=True, message='Applied'),
        SimpleNamespace(success=False, message='Unknown'),
    ]
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        op = await service.preview(db, 7, request(user_ids=[users[0].id]))
        await service.run_batch(db, op)
        assert sender.send_message.await_count == 1
        text = sender.send_message.call_args.args[1]
        assert 'Успешно изменено подписок: 1 из 2' in text
        assert 'Ваша подписка была <code>продлена на 7 дней</code>.' in text


async def test_no_success_no_telegram_and_blocked_are_skipped(monkeypatch, bot):
    sender, executor = bot
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        users[0].telegram_id = None
        users[1].status = 'blocked'
        await db.commit()
        executor.return_value = SimpleNamespace(success=False, message='Unknown')
        op = await service.preview(db, 7, request(user_ids=[users[2].id]))
        await service.run_batch(db, op)
        assert (await service.summary(db, op))['notifications']['skipped'] == 1
        executor.side_effect = None
        executor.return_value = SimpleNamespace(success=True, message='Applied')
        op = await service.preview(db, 7, request(user_ids=[users[0].id, users[1].id]))
        await service.run_batch(db, op)
        assert (await service.summary(db, op))['notifications']['skipped'] == 2
        sender.send_message.assert_not_awaited()


async def test_recovery_never_replays_interrupted_telegram_send(monkeypatch, bot):
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        op = await service.preview(db, 7, request(user_ids=[users[1].id]))
        target = await db.scalar(select(UserBulkTarget))
        target.state = 'success'
        receipt = await db.scalar(select(UserBulkNotification))
        receipt.state = 'sending'
        operation = await service.get_operation(db, op)
        operation.state = 'running'
        await db.commit()
        await service.run_batch(db, op)
        result = await service.summary(db, op)
        assert result['state'] == 'needs_review'
        assert result['notifications']['uncertain'] == 1
        bot[0].send_message.assert_not_awaited()
        bot[1].assert_not_awaited()


async def test_notification_only_backlog_can_resume_without_mutations(monkeypatch, bot):
    monkeypatch.setattr(settings, 'USERS_BULK_BATCH_SIZE', 1)
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        op = await service.preview(db, 7, request(user_ids=[users[1].id, users[2].id]))
        for target in (await db.scalars(select(UserBulkTarget))).all():
            target.state = 'success'
        await db.commit()
        await service.run_batch(db, op)
        assert (await service.summary(db, op))['state'] == 'paused'
        await service.run_batch(db, op)
        assert (await service.summary(db, op))['state'] == 'completed'
        assert bot[0].send_message.await_count == 2
        bot[1].assert_not_awaited()


async def test_cancel_discards_queued_notifications(monkeypatch, bot):
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        op = await service.preview(db, 7, request(user_ids=[users[0].id]))
        await service.cancel(db, op)
        await service.run_batch(db, op)
        result = await service.summary(db, op)
        assert result['notifications']['skipped'] == 1
        assert result['state'] == 'cancelled'
        bot[0].send_message.assert_not_awaited()
        bot[1].assert_not_awaited()


@pytest.mark.parametrize(
    'action,days,traffic,expected',
    [
        ('extend_subscription', 1, None, 'продлена на 1 день'),
        ('extend_subscription', 2, None, 'продлена на 2 дня'),
        ('extend_subscription', 11, None, 'продлена на 11 дней'),
        ('deduct_days', 21, None, 'сокращена на 21 день'),
        ('add_traffic', None, 50, 'добавлен трафик: 50 ГБ'),
    ],
)
def test_action_description(action, days, traffic, expected):
    assert delivery.action_text(action, days, traffic) == expected


def test_reject_custom_text_without_opt_in_and_telegram_oversize():
    for data in [
        dict(notify_users=False, notification_message='Text'),
        dict(notification_message='a' * 2001),
        dict(notification_message='😀' * 1001),
    ]:
        with pytest.raises(ValidationError):
            request(user_ids=[1], **data)


@pytest.mark.parametrize('endpoint', ['preview', 'run'])
async def test_notification_permission_checked_before_work(monkeypatch, endpoint):
    import app.cabinet.routes.user_bulk_operations as routes

    gate = AsyncMock(side_effect=HTTPException(403, 'Denied'))
    monkeypatch.setattr(routes, 'require_permission', lambda *args: gate)
    preview = AsyncMock()
    monkeypatch.setattr(service, 'preview', preview)
    monkeypatch.setattr(
        service, 'get_operation', AsyncMock(return_value=SimpleNamespace(params={'notify_users': True}))
    )
    with pytest.raises(HTTPException) as exc:
        if endpoint == 'preview':
            await routes.preview(request(user_ids=[1]), http_request=object(), admin=SimpleNamespace(id=7), db=object())
        else:
            await routes.run(uuid4(), request=object(), admin=SimpleNamespace(id=7), permission_db=object())
    assert exc.value.status_code == 403
    gate.assert_awaited_once()
    preview.assert_not_awaited()


async def test_blocked_bot_recipient_does_not_stop_other_receipts(monkeypatch, bot):
    sender, _ = bot
    sender.send_message.side_effect = [
        TelegramForbiddenError(method=SendMessage(chat_id=1, text='test'), message='Forbidden'),
        SimpleNamespace(message_id=322),
    ]
    async with memory_session(monkeypatch, TABLES) as db:
        users = await seed(db)
        op = await service.preview(db, 7, request(user_ids=[users[1].id, users[2].id]))
        await service.run_batch(db, op)
        result = await service.summary(db, op)
        assert result['notifications']['failed'] == result['notifications']['sent'] == 1
        assert sender.send_message.await_count == 2
