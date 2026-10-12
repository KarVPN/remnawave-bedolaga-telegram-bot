from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.cabinet.routes.subscription_modules import servers, tariff_extras
from app.cabinet.schemas.subscription import TariffRenewalPreviewRequest
from app.handlers.subscription import tariff_purchase


async def test_country_update_keeps_servers_included_in_selected_tariff(monkeypatch):
    tariff = SimpleNamespace(allowed_squads=['included'])
    subscription = SimpleNamespace(
        id=321, tariff_id=7, tariff=tariff, is_trial=False, connected_squads=['included', 'extra']
    )
    resolver = AsyncMock(return_value=subscription)
    monkeypatch.setattr(servers, 'resolve_subscription', resolver)
    from app.database.crud import server_squad

    monkeypatch.setattr(
        server_squad,
        'get_available_server_squads',
        AsyncMock(return_value=[SimpleNamespace(squad_uuid='extra')]),
    )
    user = SimpleNamespace(id=27, promo_group_id=None)
    db = SimpleNamespace()

    result = await servers.update_countries({'countries': ['extra']}, user, db, subscription_id=321)

    resolver.assert_awaited_once_with(db, user, 321)
    assert result['message'] == 'No changes detected'
    assert result['connected_squads'] == ['included', 'extra']


async def test_renewal_preview_quotes_only_selected_extras_for_explicit_subscription(monkeypatch):
    tariff = SimpleNamespace(id=7, is_active=True, device_limit=2, is_daily=False, allowed_squads=['included'])
    subscription = SimpleNamespace(id=321, tariff_id=7, tariff=tariff, device_limit=2)
    user = SimpleNamespace(id=27, get_primary_promo_group=lambda: None, promo_offer_discount_percent=None)
    db = SimpleNamespace()
    resolver = AsyncMock(return_value=subscription)
    monkeypatch.setattr(tariff_extras, 'resolve_subscription', resolver)
    monkeypatch.setattr(tariff_extras, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(
        tariff_extras,
        'get_subscription_extra_squad_records',
        AsyncMock(
            return_value=[
                SimpleNamespace(squad_uuid='extra-a', display_name='A', price_kopeks=2000),
                SimpleNamespace(squad_uuid='extra-b', display_name='B', price_kopeks=3000),
            ]
        ),
    )
    monkeypatch.setattr(tariff_extras, '_apply_addon_discount', lambda *args: {'percent': 0})
    quote = SimpleNamespace(
        final_total=12000,
        original_total=12000,
        base_price=10000,
        devices_price=0,
        traffic_price=0,
        servers_price=2000,
        breakdown={},
    )
    calculate = AsyncMock(return_value=quote)
    monkeypatch.setattr(tariff_extras.pricing_engine, 'calculate_tariff_purchase_price', calculate)
    request = TariffRenewalPreviewRequest(
        tariff_id=7, period_days=30, subscription_id=321, selected_extra_squads=['extra-a']
    )

    response = await tariff_extras.tariff_renewal_preview(request, user, db)

    resolver.assert_awaited_once_with(db, user, 321)
    assert calculate.await_args.kwargs['extra_squad_uuids'] == ['extra-a']
    assert calculate.await_args.kwargs['db'] is db
    assert response.extra_squads_total_kopeks == 2000
    assert [(item.uuid, item.enabled) for item in response.extra_squads] == [('extra-a', True), ('extra-b', False)]


async def test_bot_extra_selection_cannot_use_another_persons_subscription(monkeypatch):
    find_subscription = AsyncMock(return_value=None)
    monkeypatch.setattr(tariff_purchase, 'get_subscription_by_id_for_user', find_subscription)
    monkeypatch.setattr(tariff_purchase, 'get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(id=7)))
    callback = SimpleNamespace(data='tariff_ext_extra:7:30:extra-a', answer=AsyncMock())
    state = SimpleNamespace(get_data=AsyncMock(return_value={'active_subscription_id': 321}))
    user = SimpleNamespace(id=27)
    db = SimpleNamespace()

    await tariff_purchase.toggle_tariff_extend_extra_squad(callback, user, db, state)

    find_subscription.assert_awaited_once_with(db, 321, 27)
    callback.answer.assert_awaited_once_with('Подписка не найдена', show_alert=True)
