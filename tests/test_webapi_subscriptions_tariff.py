"""Tests for tariff serialization in the machine API subscriptions response."""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.database.models import Tariff
from app.webapi.routes.subscriptions import _serialize_subscription
from app.webapi.schemas.subscriptions import SubscriptionResponse


def _build_tariff(tariff_id: int = 5, name: str = 'Pro', period_prices: dict | None = None) -> Tariff:
    return Tariff(
        id=tariff_id,
        name=name,
        period_prices={'30': 50000, '90': 120000} if period_prices is None else period_prices,
    )


def _build_subscription(tariff: Tariff | None = None, tariff_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        user_id=2,
        status='active',
        actual_status='active',
        is_trial=False,
        start_date=datetime(2024, 1, 1, tzinfo=UTC),
        end_date=datetime(2024, 2, 1, tzinfo=UTC),
        traffic_limit_gb=100,
        traffic_used_gb=12.5,
        device_limit=3,
        autopay_enabled=False,
        autopay_days_before=3,
        subscription_url='https://example.com/sub',
        subscription_crypto_link=None,
        connected_squads=['squad-uuid'],
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 2, tzinfo=UTC),
        tariff_id=tariff_id,
        tariff=tariff,
    )


def test_serialize_subscription_includes_tariff_name_and_periods():
    tariff = _build_tariff()
    subscription = _build_subscription(tariff=tariff, tariff_id=tariff.id)

    response = _serialize_subscription(subscription)

    assert response.tariff is not None
    assert response.tariff.id == 5
    assert response.tariff.name == 'Pro'
    assert response.tariff.available_periods == [30, 90]


def test_serialize_subscription_without_tariff_returns_none():
    subscription = _build_subscription(tariff=None, tariff_id=None)

    response = _serialize_subscription(subscription)

    assert response.tariff is None
    # Existing fields must stay intact.
    assert response.id == 1
    assert response.device_limit == 3
    assert response.traffic_limit_gb == 100


def test_serialize_subscription_with_deleted_tariff_returns_none():
    # tariff_id is set, but the related tariff row is gone (FK SET NULL race / dangling reference).
    subscription = _build_subscription(tariff=None, tariff_id=5)

    response = _serialize_subscription(subscription)

    assert response.tariff is None


def test_serialize_subscription_with_empty_period_prices_returns_empty_periods():
    tariff = _build_tariff(period_prices={})
    subscription = _build_subscription(tariff=tariff, tariff_id=tariff.id)

    response = _serialize_subscription(subscription)

    assert response.tariff is not None
    assert response.tariff.available_periods == []


def test_tariff_field_is_optional_for_existing_consumers():
    # Consumers unaware of the new field keep working: it defaults to None.
    assert SubscriptionResponse.model_fields['tariff'].default is None

    legacy = SubscriptionResponse(
        id=1,
        user_id=2,
        status='active',
        actual_status='active',
        is_trial=False,
        start_date=datetime(2024, 1, 1, tzinfo=UTC),
        end_date=datetime(2024, 2, 1, tzinfo=UTC),
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        device_limit=3,
        autopay_enabled=False,
    )

    assert legacy.tariff is None
    assert legacy.model_dump()['tariff'] is None
