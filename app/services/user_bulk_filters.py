"""Shared SQL selection. Dates are estimates, never reconstructed history."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_

from app.config import settings
from app.database.models import Subscription


def day_bounds(day: date) -> tuple[datetime, datetime]:
    zone = ZoneInfo(settings.TIMEZONE)
    start = datetime.combine(day, time.min, zone)
    end = datetime.combine(day + timedelta(days=1), time.min, zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def subscription_day_condition(day: date):
    start, end = day_bounds(day)
    return and_(Subscription.start_date < end, Subscription.end_date > start)
