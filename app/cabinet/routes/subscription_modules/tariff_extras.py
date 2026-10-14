"""KarVPN tariff extra-server selection and renewal preview."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import Subscription, User
from app.services.pricing_engine import PricingEngine, pricing_engine
from app.services.tariff_extra_squads import get_subscription_extra_squad_records
from app.utils.pricing_utils import calculate_months_from_days

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import ExtraSquadPreviewItem, TariffRenewalPreviewRequest, TariffRenewalPreviewResponse
from .helpers import _apply_addon_discount, resolve_subscription


router = APIRouter()


async def _get_tariff_included_squads(
    db: AsyncSession,
    subscription: Subscription | None,
) -> list[str]:
    """Return immutable server squads included in the current tariff."""
    if not subscription or not subscription.tariff_id:
        return []

    tariff = getattr(subscription, 'tariff', None)
    if tariff is None:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)

    return list(getattr(tariff, 'allowed_squads', None) or [])


async def _build_tariff_extra_squad_preview(
    db: AsyncSession,
    user: User,
    subscription: Subscription | None,
    period_days: int,
    selected_extra_squads: list[str] | None = None,
) -> tuple[list[ExtraSquadPreviewItem], int]:
    if not subscription or not subscription.tariff_id:
        return [], 0

    extra_records = await get_subscription_extra_squad_records(db, subscription)
    if not extra_records:
        return [], 0

    selected_set = (
        set(selected_extra_squads)
        if selected_extra_squads is not None
        else {record.squad_uuid for record in extra_records}
    )
    discount = _apply_addon_discount(user, 'servers', 0, period_days)
    servers_discount_pct = discount.get('percent', 0)
    months = calculate_months_from_days(period_days)
    multiplier = 1 if getattr(subscription.tariff, 'is_daily', False) and period_days <= 1 else months

    items: list[ExtraSquadPreviewItem] = []
    total_kopeks = 0
    for record in extra_records:
        per_period = PricingEngine.apply_discount(int(record.price_kopeks or 0), servers_discount_pct) * multiplier
        enabled = record.squad_uuid in selected_set
        if enabled:
            total_kopeks += per_period
        items.append(
            ExtraSquadPreviewItem(
                uuid=record.squad_uuid,
                name=record.display_name,
                price_kopeks=per_period,
                price_label=settings.format_price(per_period),
                enabled=enabled,
            )
        )

    return items, total_kopeks


@router.post('/tariff-renewal-preview', response_model=TariffRenewalPreviewResponse)
async def tariff_renewal_preview(
    request: TariffRenewalPreviewRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    subscription = await resolve_subscription(db, user, request.subscription_id)
    if not subscription or not subscription.tariff_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No tariff subscription found')

    tariff = await get_tariff_by_id(db, request.tariff_id)
    if not tariff or not tariff.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Tariff not found or inactive')

    existing_subscription = subscription if subscription.tariff_id == tariff.id else None
    device_limit = existing_subscription.device_limit if existing_subscription else None

    result = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        request.period_days,
        device_limit=device_limit,
        custom_traffic_gb=request.traffic_gb
        if request.traffic_gb is not None and tariff.can_purchase_custom_traffic()
        else None,
        user=user,
        extra_squad_uuids=request.selected_extra_squads if existing_subscription else None,
        db=db,
    )
    extra_items, extra_total = await _build_tariff_extra_squad_preview(
        db,
        user,
        existing_subscription,
        request.period_days,
        request.selected_extra_squads,
    )

    original_total = result.original_total if result.original_total > result.final_total else None
    return TariffRenewalPreviewResponse(
        final_total_kopeks=result.final_total,
        final_total_label=settings.format_price(result.final_total),
        original_total_kopeks=original_total,
        original_total_label=settings.format_price(original_total) if original_total is not None else None,
        base_price_kopeks=result.base_price,
        devices_price_kopeks=result.devices_price,
        traffic_price_kopeks=result.traffic_price,
        servers_price_kopeks=result.servers_price,
        offer_discount_percent=int(result.breakdown.get('offer_discount_pct', 0) or 0),
        extra_squads_total_kopeks=extra_total,
        extra_squads_total_label=settings.format_price(extra_total),
        extra_squads=extra_items,
    )
