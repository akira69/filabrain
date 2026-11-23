"""Inventory decisions remain measured, bounded and read-only."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from app.api.deps import require_auth
from app.core.security import Principal
from app.main import app
from app.models import (
    Color,
    Filament,
    FilamentColor,
    Manufacturer,
    Printer,
    Spool,
    SpoolEvent,
    SpoolStatus,
)
from sqlalchemy import select
from test_backend import BASE, installed

__all__ = ['installed']
pytest_plugins = ('tests.conftest',)
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def inventory(installed, db_session):
    statuses = {s.key: s.id for s in await db_session.scalars(select(SpoolStatus))}
    maker = Manufacturer(name='Inventory fixture')
    db_session.add(maker)
    await db_session.flush()
    products = [Filament(manufacturer_id=maker.id, designation=name, material_type='PETG', diameter_mm=1.75,
                         manufacturer_sku=sku, manufacturer_color_name='Red', raw_material_weight_g=weight,
                         shop_url='https://maker.example/' + name.lower()) for name, sku, weight in [
                             ('Outdoor', 'P_001', 1000), ('Duplicate', 'P001', 1000), ('Unknown package', '', None)]]
    db_session.add_all(products)
    await db_session.flush()
    color = Color(name='Red', hex_code='#FF0000')
    db_session.add(color)
    await db_session.flush()
    db_session.add(FilamentColor(filament_id=products[0].id, color_id=color.id, position=1))
    spools = [Spool(filament_id=products[index].id, status_id=statuses[status], remaining_weight_g=weight)
              for index, status, weight in [(0, 'active', 400), (0, 'new', None), (0, 'opened', 100),
                                             (0, 'archived', 9999), (0, 'empty', 500), (1, 'new', 150),
                                             (2, 'new', 50)]]
    db_session.add_all(spools)
    printer = Printer(name='P1S', driver_key='fixture')
    db_session.add(printer)
    await db_session.commit()
    return {'products': [SimpleNamespace(id=p.id) for p in products],
            'spools': [SimpleNamespace(id=s.id) for s in spools], 'printer': SimpleNamespace(id=printer.id)}


async def test_job_fit_never_pools_rolls_or_promotes_unknown_weight(installed, inventory):
    response = await installed.post(f'{BASE}/agent/job-fit', json={'query': 'outdoor PETG bracket', 'required_g': 450})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['required_g'] == 450
    assert result['enough_material_spool_ids'] == []
    candidates = {p['id']: p for p in result['candidates']}
    assert inventory['spools'][3].id not in candidates
    assert inventory['spools'][4].id not in candidates
    assert candidates[inventory['spools'][1].id]['meets_required'] is None
    assert candidates[inventory['spools'][0].id]['meets_required'] is False
    assert result['compatibility_requires_review'] is True
    assert result['evidence']


async def test_job_fit_explicit_grams_override_unrelated_package_weight(installed, inventory):
    response = await installed.post(f'{BASE}/agent/job-fit', json={'query': 'I bought a 1 kg PETG roll', 'required_g': 280})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['required_g'] == 280
    assert result['enough_material_spool_ids'] == [inventory['spools'][0].id]


async def test_job_fit_checks_printer_scope_before_reading_it(installed, inventory, monkeypatch):
    async def principal():
        return Principal(auth_type='token', is_superadmin=True, scopes=['filaments:read', 'spools:read'])
    monkeypatch.setitem(app.dependency_overrides, require_auth, principal)
    response = await installed.post(f'{BASE}/agent/job-fit', json={'required_g': 100, 'printer_id': inventory['printer'].id})
    assert response.status_code == 403


async def test_library_health_finds_missing_swatches_and_duplicates_outside_page(installed, inventory):
    response = await installed.post(f'{BASE}/agent/library-health', json={'page_size': 1})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['coverage']['records_checked'] == 1
    assert result['coverage']['matching_records'] == 3
    assert result['coverage']['has_more'] is True
    assert result['complete'] is False
    assert set(result['duplicate_groups'][0]['filament_ids']) == {f.id for f in inventory['products'][:2]}
    selected = await installed.post(f'{BASE}/agent/library-health', json={'filament_ids': [inventory['products'][1].id]})
    item = selected.json()['items'][0]
    assert any(f['code'] == 'missing_swatch' for f in item['findings'])
    assert any(f['code'] == 'missing_field' and f['field'] == 'density_g_cm3' for f in item['findings'])


async def test_source_checks_distinguish_denial_from_a_missing_page(installed, inventory, monkeypatch):
    from filabrain import sources
    async def supplier(url, **_):
        raise ValueError('HTTP 403 (access denied)' if url.endswith('/outdoor') else 'HTTP 404 (page not found)')
    monkeypatch.setattr(sources, 'fetch_supplier', supplier)
    result = await installed.post(f'{BASE}/agent/library-health', json={
        'filament_ids': [f.id for f in inventory['products'][:2]], 'check_sources': True})
    assert result.status_code == 200, result.text
    assert result.json()['complete'] is False
    items = result.json()['items']
    assert items[0]['source_check']['status'] == 'blocked'
    assert items[1]['source_check']['status'] == 'missing'
    assert not any(f['code'] == 'broken_link' for f in items[0]['findings'])
    assert any(f['code'] == 'broken_link' for f in items[1]['findings'])


async def test_stock_does_not_order_when_remaining_weights_are_unknown(installed, inventory):
    response = await installed.post(f'{BASE}/agent/stock', json={'filament_ids': [inventory['products'][0].id], 'target_g': 1000})
    assert response.status_code == 200, response.text
    item = response.json()['items'][0]
    assert item['known_available_g'] == 500
    assert item['unknown_weight_spool_ids'] == [inventory['spools'][1].id]
    assert item['order_quantity'] is None
    assert item['needs_measurement'] is True


async def test_stock_requires_a_target_and_a_known_package_for_quantity(installed, inventory):
    response = await installed.post(f'{BASE}/agent/stock', json={'filament_ids': [inventory['products'][1].id]})
    assert response.status_code == 200
    assert response.json()['items'][0]['order_quantity'] is None
    assert response.json()['items'][0]['needs_target'] is True
    ready = await installed.post(f'{BASE}/agent/stock', json={'filament_ids': [inventory['products'][1].id], 'target_g': 2200})
    assert ready.json()['items'][0]['order_quantity'] == 3
    unknown = await installed.post(f'{BASE}/agent/stock', json={'filament_ids': [inventory['products'][2].id], 'target_g': 1000})
    assert unknown.json()['items'][0]['order_quantity'] is None
    assert unknown.json()['items'][0]['needs_package_weight'] is True


async def test_stock_counts_consumption_not_measurements_or_adjustments(installed, inventory, db_session):
    now = datetime.now(UTC)
    from app.services.spool_service import SpoolService
    spool = await db_session.get(Spool, inventory['spools'][5].id)
    await SpoolService(db_session).record_consumption(spool, 120, now)
    db_session.add_all([SpoolEvent(spool_id=inventory['spools'][5].id, event_at=when, event_type=kind, delta_weight_g=delta)
                        for kind, delta, when in [('manual_adjust', -500, now), ('measurement', -900, now),
                                                  ('print_consumption', -200, now - timedelta(days=90))]])
    await db_session.commit()
    response = await installed.post(f'{BASE}/agent/stock', json={'filament_ids': [inventory['products'][1].id], 'target_g': 1000, 'history_days': 30})
    assert response.status_code == 200, response.text
    assert response.json()['items'][0]['recorded_consumption_g'] == 120


@pytest.mark.parametrize('path,payload', [('job-fit', {'required_g': 0}), ('stock', {'target_g': -1}),
    ('stock', {'target_g': 'nan'}), ('library-health', {'page_size': 51}),
    ('library-health', {'filament_ids': [True]}), ('stock', {'apply': True})])
async def test_inventory_tools_reject_invalid_or_write_inputs(installed, path, payload):
    assert (await installed.post(f'{BASE}/agent/{path}', json=payload)).status_code == 422


@pytest.mark.parametrize('path', ['job-fit', 'stock'])
async def test_inventory_tools_require_spool_read(installed, monkeypatch, path):
    async def principal():
        return Principal(auth_type='token', is_superadmin=True, scopes=['filaments:read'])
    monkeypatch.setitem(app.dependency_overrides, require_auth, principal)
    assert (await installed.post(f'{BASE}/agent/{path}', json={})).status_code == 403


async def test_loaded_spools_require_present_active_printer_slots(installed, inventory, db_session):
    from app.models import PrinterSlot, PrinterSlotAssignment
    for number, active, present, index in [(1, True, True, 0), (2, True, False, 2), (3, False, True, 5)]:
        slot = PrinterSlot(printer_id=inventory['printer'].id, slot_no=number, is_active=active)
        db_session.add(slot)
        await db_session.flush()
        db_session.add(PrinterSlotAssignment(slot_id=slot.id, spool_id=inventory['spools'][index].id, present=present))
    await db_session.commit()
    response = await installed.post(f'{BASE}/agent/job-fit', json={'required_g': 100, 'printer_id': inventory['printer'].id})
    assert response.status_code == 200, response.text
    assert [s['id'] for s in response.json()['candidates'] if s['loaded_in_selected_printer']] == [inventory['spools'][0].id]


async def test_health_exposes_duplicate_evidence_and_reports_unavailable_selection(installed, inventory):
    response = await installed.post(f'{BASE}/agent/library-health', json={'filament_ids': [inventory['products'][0].id, 999999]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['complete'] is False
    assert result['coverage']['unavailable_filament_ids'] == [999999]
    assert any('Duplicate' in e['title'] and len(e['links']) == 2 for e in result['evidence'])
    assert result['evidence'][0]['links'][0]['url'] == 'https://maker.example/outdoor'


async def test_inventory_checks_do_not_write_inventory(installed, inventory, db_session):
    from sqlalchemy import func
    async def counts():
        return [await db_session.scalar(select(func.count()).select_from(model)) for model in (Filament, Spool, Color, FilamentColor, SpoolEvent)]
    before = await counts()
    for path in ('job-fit', 'library-health', 'stock'):
        response = await installed.post(f'{BASE}/agent/{path}', json={})
        assert response.status_code == 200, response.text
    assert await counts() == before


async def test_health_preserves_report_when_one_source_has_bad_http(installed, inventory, monkeypatch):
    import http.client

    from filabrain import sources
    async def supplier(*_, **__):
        raise http.client.BadStatusLine('invalid fixture response')
    monkeypatch.setattr(sources, 'fetch_supplier', supplier)
    response = await installed.post(f'{BASE}/agent/library-health', json={'filament_ids': [inventory['products'][0].id], 'check_sources': True})
    assert response.status_code == 200, response.text
    assert response.json()['complete'] is False
    assert response.json()['items'][0]['source_check']['status'] == 'unverified'


async def test_job_fit_count_is_explicitly_limited_to_displayed_candidates(installed, inventory, db_session):
    status = await db_session.scalar(select(SpoolStatus.id).where(SpoolStatus.key == 'active'))
    db_session.add_all(Spool(filament_id=inventory['products'][0].id, status_id=status, remaining_weight_g=800) for _ in range(6))
    await db_session.commit()
    result = await installed.post(f'{BASE}/agent/job-fit', json={'required_g': 500})
    assert result.status_code == 200, result.text
    assert result.json()['reply'].startswith('Showing ')
    assert result.json()['coverage']['exhaustive'] is False
