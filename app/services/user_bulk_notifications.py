"""Durable, single-attempt Telegram receipts for confirmed subscription changes.

Claim is committed before Telegram I/O. An interrupted claim is uncertain and is
never automatically sent again: Telegram has no idempotency key for sendMessage.
"""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from html import escape

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions
from sqlalchemy import case, func, insert, literal, select, update

from app.bot_factory import create_bot
from app.config import settings
from app.database.models import User, UserBulkNotification, UserBulkOperation, UserBulkTarget, UserStatus


STATES = ('pending', 'sending', 'sent', 'failed', 'limited', 'uncertain', 'skipped')


def action_text(action, days=None, traffic_gb=None):
    if action == 'add_traffic':
        return f'добавлен трафик: {traffic_gb} ГБ'
    remainder = days % 100
    unit = (
        'дней' if 11 <= remainder <= 14 else 'день' if days % 10 == 1 else 'дня' if days % 10 in (2, 3, 4) else 'дней'
    )
    return f'{"продлена" if action == "extend_subscription" else "сокращена"} на {days} {unit}'


def receipt_text(operation, successful, total):
    description = action_text(operation.action, operation.params['days'], operation.params['traffic_gb'])
    if successful == 1:
        prefix = 'Для вашей подписки' if operation.action == 'add_traffic' else 'Ваша подписка была'
        text = f'{prefix} <code>{escape(description)}</code>.'
    else:
        text = f'Изменены ваши подписки ({successful} шт.). Для каждой: <code>{escape(description)}</code>.'
    if successful < total:
        text += (
            f'\nУспешно изменено подписок: {successful} из {total} выбранных. Сообщение касается только этих изменений.'
        )
    custom = operation.params.get('notification_message')
    if custom:
        text += f'\n\n{escape(custom)}'
    return text


async def create_receipts(db, operation_id):
    source = (
        select(literal(operation_id), UserBulkTarget.user_id, literal('pending'), literal(datetime.now(UTC)))
        .where(UserBulkTarget.operation_id == operation_id)
        .distinct()
    )
    await db.execute(
        insert(UserBulkNotification).from_select(['operation_id', 'user_id', 'state', 'updated_at'], source)
    )


async def recover(db, operation_id):
    await db.execute(
        update(UserBulkNotification)
        .where(UserBulkNotification.operation_id == operation_id, UserBulkNotification.state == 'sending')
        .values(
            state='uncertain',
            message='Доставка не подтверждена после прерывания; автоматического повтора нет',
            updated_at=datetime.now(UTC),
        )
    )


async def counts(db, operation_id):
    rows = dict(
        (
            await db.execute(
                select(UserBulkNotification.state, func.count())
                .where(UserBulkNotification.operation_id == operation_id)
                .group_by(UserBulkNotification.state)
            )
        ).all()
    )
    return {state: rows.get(state, 0) for state in STATES}


async def deliver_batch(db, operation_id):
    """Called only under the operation executor's global advisory lock."""
    pending_targets = select(UserBulkTarget.user_id).where(
        UserBulkTarget.operation_id == operation_id, UserBulkTarget.state.in_(['pending', 'processing'])
    )
    bot = None
    try:
        for _ in range(settings.USERS_BULK_BATCH_SIZE):
            operation = await db.scalar(
                select(UserBulkOperation)
                .where(UserBulkOperation.id == operation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if operation.cancel_requested:
                await db.commit()
                break
            receipt = await db.scalar(
                select(UserBulkNotification)
                .where(
                    UserBulkNotification.operation_id == operation_id,
                    UserBulkNotification.state == 'pending',
                    UserBulkNotification.user_id.not_in(pending_targets),
                )
                .order_by(UserBulkNotification.id)
                .limit(1)
            )
            if receipt is None:
                await db.commit()
                break
            successful, total = (
                await db.execute(
                    select(
                        func.sum(case((UserBulkTarget.state == 'success', 1), else_=0)),
                        func.count(),
                    ).where(UserBulkTarget.operation_id == operation_id, UserBulkTarget.user_id == receipt.user_id)
                )
            ).one()
            user = await db.get(User, receipt.user_id, populate_existing=True)
            if not successful:
                receipt.state, receipt.message = 'skipped', 'Нет подтверждённых успешных изменений'
            elif user is None or user.status in (UserStatus.DELETED.value, UserStatus.BLOCKED.value):
                receipt.state, receipt.message = 'skipped', 'Пользователь удалён или заблокирован'
            elif not user.telegram_id:
                receipt.state, receipt.message = 'skipped', 'У пользователя нет Telegram'
            elif not settings.BOT_TOKEN:
                receipt.state, receipt.message = 'failed', 'Бот для отправки уведомлений не настроен'
            else:
                receipt.text = receipt_text(operation, int(successful), total)
                receipt.state = 'sending'
            receipt.updated_at = datetime.now(UTC)
            receipt_id, telegram_id, text = receipt.id, user.telegram_id if user else None, receipt.text
            state = receipt.state
            await db.commit()  # durable BEFORE the non-idempotent sendMessage
            if state != 'sending':
                continue
            try:
                if bot is None:
                    bot = create_bot()
                result = await bot.send_message(
                    telegram_id,
                    text,
                    parse_mode='HTML',
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    request_timeout=15,
                )
                state, message, telegram_message_id = 'sent', None, result.message_id
            except TelegramRetryAfter:
                state, message, telegram_message_id = (
                    'limited',
                    'Telegram ограничил частоту отправки; повтор не выполнялся',
                    None,
                )
            except (TelegramForbiddenError, TelegramBadRequest):
                state, message, telegram_message_id = (
                    'failed',
                    'Telegram отклонил сообщение или пользователь заблокировал бота',
                    None,
                )
            except Exception:
                state, message, telegram_message_id = (
                    'uncertain',
                    'Доставка не подтверждена; автоматического повтора нет',
                    None,
                )
            await db.execute(
                update(UserBulkNotification)
                .where(UserBulkNotification.id == receipt_id)
                .values(
                    state=state,
                    message=message,
                    telegram_message_id=telegram_message_id,
                    updated_at=datetime.now(UTC),
                )
            )
            await db.execute(
                update(UserBulkOperation)
                .where(UserBulkOperation.id == operation_id)
                .values(updated_at=datetime.now(UTC))
            )
            await db.commit()
            if state in ('limited', 'uncertain'):
                break
            await asyncio.sleep(max(settings.USERS_BULK_ITEM_DELAY, 0.05))
    finally:
        if bot is not None:
            with suppress(Exception):
                await bot.session.close()
