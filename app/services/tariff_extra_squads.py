from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.server_squad import get_server_squads_by_uuids
from app.database.crud.tariff import get_tariff_by_id
from app.database.models import ServerSquad, Subscription, Tariff


def _dedupe(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(values or []))


async def get_subscription_tariff(subscription: Subscription, db: AsyncSession) -> Tariff | None:
    if not getattr(subscription, 'tariff_id', None):
        return None

    tariff = getattr(subscription, 'tariff', None)
    if tariff is not None:
        return tariff

    return await get_tariff_by_id(db, subscription.tariff_id)


def tariff_includes_all_servers(tariff: Tariff | None) -> bool:
    if tariff is None:
        return False
    return not bool(getattr(tariff, 'allowed_squads', None))


async def get_subscription_extra_squad_uuids(
    db: AsyncSession,
    subscription: Subscription | None,
) -> list[str]:
    if not subscription or not getattr(subscription, 'tariff_id', None):
        return []

    tariff = await get_subscription_tariff(subscription, db)
    if tariff is None or tariff_includes_all_servers(tariff):
        return []

    connected = _dedupe(list(getattr(subscription, 'connected_squads', None) or []))
    included = set(_dedupe(list(getattr(tariff, 'allowed_squads', None) or [])))
    return [uuid for uuid in connected if uuid not in included]


async def build_connected_squads_for_tariff_renewal(
    db: AsyncSession,
    subscription: Subscription,
    selected_extra_squad_uuids: list[str] | None = None,
) -> list[str]:
    tariff = await get_subscription_tariff(subscription, db)
    current_connected = _dedupe(list(getattr(subscription, 'connected_squads', None) or []))
    if tariff is None:
        return current_connected

    if tariff_includes_all_servers(tariff):
        # Empty allowed_squads means all servers are included in the tariff, so
        # there is no separable paid extra-squad layer to toggle on renew.
        return current_connected

    base_squads = _dedupe(list(getattr(tariff, 'allowed_squads', None) or []))
    current_extra = await get_subscription_extra_squad_uuids(db, subscription)
    extra_set = set(current_extra)

    if selected_extra_squad_uuids is None:
        selected = current_extra
    else:
        selected = [uuid for uuid in _dedupe(selected_extra_squad_uuids) if uuid in extra_set]

    return _dedupe(base_squads + selected)


async def get_subscription_extra_squad_records(
    db: AsyncSession,
    subscription: Subscription | None,
) -> list[ServerSquad]:
    extra_uuids = await get_subscription_extra_squad_uuids(db, subscription)
    if not extra_uuids:
        return []

    records = await get_server_squads_by_uuids(db, extra_uuids)
    record_map = {record.squad_uuid: record for record in records}
    return [record_map[uuid] for uuid in extra_uuids if uuid in record_map]
