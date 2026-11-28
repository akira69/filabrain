"""Agent read tools share publisher/catalog logic without applying inventory."""
import json

import pytest
from app.api.deps import require_auth
from app.core.security import Principal
from app.main import app
from app.models import Color, Filament, FilamentColor, InstalledPlugin, Manufacturer
from sqlalchemy import func, select
from test_backend import BASE, installed
from test_color_review import publisher
from test_spool_profiles import catalog_requests

__all__ = ['catalog_requests', 'installed', 'publisher']

pytest_plugins = ('tests.conftest',)
pytestmark = pytest.mark.asyncio


async def test_agent_color_preview_reuses_exact_hex_without_creating(installed, db_session, publisher):
    saved = Color(name='Existing red', hex_code='#AB1519')
    db_session.add(saved)
    await db_session.commit()
    response = await installed.post(f'{BASE}/agent/color', json={'code': 'RAL 3020'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['code'] == 'RAL 3020'
    assert result['canonical_name'] == 'Traffic red (RAL 3020)'
    assert result['hex_code'] == '#AB1519'
    assert result['matches'] == [{'id': saved.id, 'name': 'Existing red', 'hex_code': '#AB1519'}]
    assert result['requires_review'] is True
    assert await db_session.scalar(select(func.count(Color.id))) == 1


@pytest.mark.parametrize('code', ['RAL 9999', 'Pantone 9999 C'])
async def test_unknown_agent_color_never_invents_hex(installed, publisher, monkeypatch, code):
    if code.startswith("Pantone"):
        from filabrain import color_standards
        async def unavailable(_url):
            return b""
        monkeypatch.setattr(color_standards, "read_asset", unavailable)
    response = await installed.post(f'{BASE}/agent/color', json={'code': code})
    assert response.status_code == 200
    assert response.json()['hex_code'] is None
    assert response.json()['matches'] == []


@pytest.mark.parametrize('payload', [
    {'code': 'RAL 3020 trailing text'}, {'code': 'Pantone 2118 C/../../secret'},
    {'code': 'https://example.com'}, {'code': 'RAL ٣٠٢٠'},
    {'code': 'RAL 3020', 'apply': True},
])
async def test_agent_color_rejects_unsafe_or_write_arguments(installed, payload):
    assert (await installed.post(f'{BASE}/agent/color', json=payload)).status_code == 422


async def test_agent_spool_lookup_keeps_distinct_designs_and_unknown_dimensions(installed, catalog_requests):
    response = await installed.post(f'{BASE}/agent/spools', json={'query': 'Paramount 3D', 'package_weight_g': 1000})
    assert response.status_code == 200, response.text
    profiles = response.json()['spool_profiles']
    assert {p['empty_weight_g'] for p in profiles} == {208, 232}
    assert all('outer_diameter_mm' not in p for p in profiles)
    assert len({p['id'] for p in profiles}) == 2


@pytest.mark.parametrize('conflicting', [False, True])
async def test_agent_color_audit_reports_shared_scope_without_renaming(installed, db_session, publisher, conflicting):
    maker = Manufacturer(name='Audit agent fixture')
    color = Color(name='RAL3020', hex_code='#AB1519')
    db_session.add_all([maker, color])
    await db_session.flush()
    products = [Filament(manufacturer_id=maker.id, designation=f'PLA {i}', material_type='PLA', diameter_mm=1.75,
                         manufacturer_color_name='Enzo Red', color_mode='single') for i in range(2)]
    if conflicting:
        products[1].manufacturer_color_name = 'RAL 4007'
    db_session.add_all(products)
    await db_session.flush()
    db_session.add_all(FilamentColor(filament_id=f.id, color_id=color.id, position=1) for f in products)
    await db_session.commit()
    result = await installed.post(f'{BASE}/agent/color-audit', json={'filament_ids': [products[0].id]})
    assert result.status_code == 200, result.text
    item = result.json()['items'][0]
    assert item['references'][0]['canonical_name'] == 'Traffic red (RAL 3020)'
    assert item['references'][0]['usage_count'] == 2
    if conflicting:
        assert item['references'][0]['conflicts'] == ['RAL 4007']
        assert 'proposed_name' not in item['references'][0]
    else:
        assert item['references'][0]['proposed_name'] == 'Traffic red (RAL 3020)'
    await db_session.refresh(color)
    assert color.name == 'RAL3020'
    await db_session.refresh(products[0])
    assert products[0].manufacturer_color_name == 'Enzo Red'


@pytest.mark.parametrize('path,payload', [
    ('color', {'code': 'RAL 3020'}), ('spools', {'query': 'Paramount 3D'}),
    ('color-audit', {'filament_ids': [1]}),
])
async def test_agent_tools_enforce_effective_permissions(installed, monkeypatch, path, payload):
    async def principal():
        return Principal(auth_type='token', is_superadmin=True, scopes=['printers:read'])
    monkeypatch.setitem(app.dependency_overrides, require_auth, principal)
    assert (await installed.post(f'{BASE}/agent/{path}', json=payload)).status_code == 403


async def test_disabled_plugin_cannot_run_agent_read_tools(installed, db_session):
    plugin = await db_session.scalar(select(InstalledPlugin).where(InstalledPlugin.plugin_key == 'filabrain'))
    plugin.is_active = False
    await db_session.commit()
    assert (await installed.post(f'{BASE}/agent/color', json={'code': 'RAL 3020'})).status_code == 404


async def test_new_tools_use_the_existing_native_mcp_endpoint(installed, db_session, monkeypatch, publisher):
    from app.core.config import settings
    from app.models import AppSettings, Spool, SpoolStatus, SystemExtraField
    from httpx import ASGITransport, AsyncClient
    tools = ['filabrain-research_filament', 'filabrain-resolve_color_reference',
             'filabrain-find_spool_profiles', 'filabrain-audit_filament_colors', 'filabrain-plan_filament_intake',
             'filabrain-choose_job_spools', 'filabrain-audit_library', 'filabrain-plan_stock',
             'filabrain-audit_extra_fields']
    monkeypatch.setattr(settings, 'mcp_plugin_read_tools', ','.join(tools))
    flags = await db_session.get(AppSettings, 1)
    if flags is None:
        flags = AppSettings(id=1)
        db_session.add(flags)
    flags.mcp_enabled, flags.mcp_write_enabled = True, False
    definitions = [
        SystemExtraField(target_type='filament', key='heated_bed', label='Bed Temperature (°C)'),
        SystemExtraField(target_type='filament', key='nozzle_temp', label='Print temperature'),
        SystemExtraField(target_type='filament', key='first_layer_bed_temp', label='First-layer bed temperature'),
        SystemExtraField(target_type='spool', key='bed_temp', label='Bed Temperature'),
    ]
    db_session.add_all(definitions)
    maker = Manufacturer(name='Example overlap')
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(manufacturer_id=maker.id, designation='Overlap PLA', material_type='PLA', diameter_mm=1.75,
                       bed_temp_range_c={'min': 50, 'max': 55},
                       custom_fields={'heated_bed': {'min': 60, 'max': 65}})
    db_session.add(filament)
    await db_session.flush()
    filament_id = filament.id
    status_id = await db_session.scalar(select(SpoolStatus.id).where(SpoolStatus.key == 'new'))
    db_session.add(Spool(filament_id=filament_id, status_id=status_id, custom_fields={'bed_temp': {'min': 50, 'max': 55}}))
    await db_session.commit()
    key = (await installed.post('/api/v1/me/api-keys', json={'name': 'Agent tool fixture'})).json()['token']
    async with AsyncClient(transport=ASGITransport(app=app), base_url='https://filaman.test',
                           headers={'Authorization': 'ApiKey ' + key}) as client:
        listed = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        names = {t['name'] for t in listed.json()['result']['tools']}
        assert set(tools) <= names
        assert 'create_color' not in names
        result = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
            'params': {'name': tools[1], 'arguments': {'code': 'RAL3020'}}})
        payload = result.json()['result']
        assert payload['isError'] is False, payload
        assert json.loads(payload['content'][0]['text'])['hex_code'] == '#AB1519'
        audited = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'filabrain-audit_extra_fields', 'arguments': {}}})
        payload = audited.json()['result']
        assert payload['isError'] is False, payload
        audit = json.loads(payload['content'][0]['text'])
        assert audit['requires_review'] is True
        assert [(row['key'], row['native_fields']) for row in audit['items']] == [
            ('heated_bed', ['bed_temp_range_c']), ('nozzle_temp', ['extruder_temp_range_c']),
        ]
        assert audit['items'][0]['values']['bed_temp_range_c'] == {
            'populated': 1, 'same': 0, 'different': 1, 'native_blank': 0, 'different_record_ids': [filament_id],
        }
        spools = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
            'params': {'name': 'filabrain-audit_extra_fields', 'arguments': {'target_type': 'spool'}}})
        payload = spools.json()['result']
        assert payload['isError'] is False, payload
        spool_audit = json.loads(payload['content'][0]['text'])
        assert spool_audit['target_type'] == 'spool'
        assert spool_audit['native_target_type'] == 'filament'
        assert [(row['key'], row['native_fields']) for row in spool_audit['items']] == [
            ('bed_temp', ['bed_temp_range_c']),
        ]
        assert spool_audit['items'][0]['values']['bed_temp_range_c']['same'] == 1
    assert await db_session.scalar(select(func.count(SystemExtraField.id))) == len(definitions)


async def test_agent_palette_read_uses_the_hosts_filament_read_permission(installed, publisher, monkeypatch):
    async def principal():
        return Principal(auth_type='token', is_superadmin=True, scopes=['filaments:read'])
    monkeypatch.setitem(app.dependency_overrides, require_auth, principal)
    response = await installed.post(f'{BASE}/agent/color', json={'code': 'RAL 3020'})
    assert response.status_code == 200, response.text
    assert response.json()['hex_code'] == '#AB1519'


async def test_agent_spool_timeout_is_an_unavailable_source_not_an_empty_match(installed, monkeypatch):
    from unittest.mock import AsyncMock

    from filabrain import agent_tools
    monkeypatch.setattr(agent_tools, 'find_spool_profiles', AsyncMock(side_effect=TimeoutError))
    response = await installed.post(f'{BASE}/agent/spools', json={'query': 'Paramount 3D'})
    assert response.status_code == 200, response.text
    assert response.json()['source_status'] == 'unavailable'
    assert response.json()['spool_profiles'] == []


async def test_agent_audit_deadline_returns_unprocessed_ids(installed, db_session, monkeypatch):
    from unittest.mock import AsyncMock

    from filabrain import agent_tools
    maker = Manufacturer(name='Incomplete audit')
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(manufacturer_id=maker.id, designation='Unknown PLA', material_type='PLA', diameter_mm=1.75)
    db_session.add(filament)
    await db_session.commit()
    identifier = filament.id
    monkeypatch.setattr(agent_tools, 'batch_proposal', AsyncMock(side_effect=TimeoutError))
    response = await installed.post(f'{BASE}/agent/color-audit', json={'filament_ids': [filament.id]})
    assert response.status_code == 200, response.text
    assert response.json()['complete'] is False
    assert response.json()['remaining_filament_ids'] == [identifier]
    assert response.json()['items'] == []


async def test_spool_tool_retains_the_existing_linked_design(installed, db_session, monkeypatch, catalog_requests):
    from app.api.v1 import filamentdb_proxy
    from fastapi import HTTPException
    maker = Manufacturer(name='Paramount 3D')
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(manufacturer_id=maker.id, designation='PLA', material_type='PLA', diameter_mm=1.75,
                        raw_material_weight_g=1000, filamentdb_spool_profile_id=735)
    db_session.add(filament)
    await db_session.commit()
    async def lookup(path, _params=None):
        if path == '/spool-profiles/735':
            return {'id': 735, 'name': 'Paramount 3D PLA Spool', 'image_file': '735.png', 'empty_weight_g': 170, 'net_weight_g': 1000}
        raise HTTPException(503)
    monkeypatch.setattr(filamentdb_proxy, '_proxy_get', lookup)
    response = await installed.post(f'{BASE}/agent/spools', json={'filament_id': filament.id})
    assert response.status_code == 200, response.text
    assert any(p['id'] == 'filamandb-735' for p in response.json()['spool_profiles'])


async def test_color_audit_missing_and_omitted_references_are_not_complete(installed, db_session, publisher, monkeypatch):
    from filabrain import agent_tools
    maker = Manufacturer(name='Five references')
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(manufacturer_id=maker.id, designation='PLA', material_type='PLA', diameter_mm=1.75)
    db_session.add(filament)
    await db_session.commit()
    async def codes(*_):
        return ['RAL 3020', 'RAL 4007', 'RAL 5002', 'RAL 9004', 'RAL 9010']
    monkeypatch.setattr(agent_tools, 'saved_references', codes)
    response = await installed.post(f'{BASE}/agent/color-audit', json={'filament_ids': [filament.id, 999999]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['complete'] is False
    assert result['unavailable_filament_ids'] == [999999]
    assert result['items'][0]['omitted_reference_codes'] == ['RAL 9010']


async def test_color_audit_rechecks_access_after_publisher_timeout(installed, db_session, monkeypatch):
    from fastapi import HTTPException

    from filabrain import agent_tools
    maker = Manufacturer(name='Timed out')
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(manufacturer_id=maker.id, designation='PLA', material_type='PLA', diameter_mm=1.75, manufacturer_color_name='RAL 3020')
    db_session.add(filament)
    await db_session.commit()
    expired = False
    original = agent_tools.read_access
    async def access(*args):
        if expired:
            raise HTTPException(403, 'Access revoked')
        return await original(*args)
    async def timeout(_):
        nonlocal expired
        expired = True
        raise TimeoutError
    monkeypatch.setattr(agent_tools, 'read_access', access)
    monkeypatch.setattr(agent_tools, 'resolve_preview', timeout)
    assert (await installed.post(f'{BASE}/agent/color-audit', json={'filament_ids': [filament.id]})).status_code == 403
