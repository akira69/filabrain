"""One intake plan reuses research, exact colors and explicit spool confirmation."""
import pytest
from app.models import Color, Filament, Spool
from sqlalchemy import func, select
from test_backend import BASE, installed
from test_color_review import publisher
from test_sku_discovery import SKU, URL, sku_sources
from test_spool_profiles import catalog_requests

__all__ = ['catalog_requests', 'installed', 'publisher', 'sku_sources']
pytest_plugins = ('tests.conftest',)
pytestmark = pytest.mark.asyncio


async def test_intake_sku_requires_product_choice_before_populating(installed, sku_sources):
    response = await installed.post(f'{BASE}/agent/intake', json={'query': 'PRL4007_7449C'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['requires_product_choice'] is True
    assert result['proposal']['suggestions'] == []
    assert result['filament_draft'] == {}
    assert result['color_previews'] == []
    assert result['spool_profiles'] == []
    assert any(i.get('product_url') == URL for i in result['proposal']['issues'])


async def test_intake_selected_product_keeps_name_exact_color_and_unknown_tare(installed, sku_sources, publisher, catalog_requests, db_session, monkeypatch):
    async def counts():
        return [await db_session.scalar(select(func.count()).select_from(model)) for model in (Filament, Color, Spool)]
    from filabrain import color_standards
    async def read(_url):
        return b'<a href="/en/colour/ral-classic/ral-4007/9000" style="background-color:#47243C"><span>RAL</span><span class="number">4007</span><div class="subtext">Purple violet</div></a>'
    monkeypatch.setattr(color_standards, 'read_asset', read)
    before = await counts()
    response = await installed.post(f'{BASE}/agent/intake', json={'sku_hint': SKU, 'product_url': URL})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['requires_product_choice'] is False
    assert result['filament_draft']['manufacturer_sku'] == SKU
    assert result['filament_draft']['manufacturer_color_name'] != 'RAL 4007'
    assert result['color_previews'][0]['code'] == 'RAL 4007'
    assert result['color_previews'][0]['hex_code'] == '#47243C'
    assert 'RAL 4007' in result['color_previews'][0]['canonical_name']
    assert result['spool_draft']['requires_design_confirmation'] is True
    assert result['spool_draft']['empty_weight_g'] is None
    assert result['requires_review'] is True
    assert await counts() == before


@pytest.mark.parametrize('payload', [{'apply': True}, {'filament_id': 1}, {'query': 'x' * 401}])
async def test_intake_rejects_writes_and_unbounded_clues(installed, payload):
    assert (await installed.post(f'{BASE}/agent/intake', json=payload)).status_code == 422


@pytest.mark.parametrize('code,hex_code', [('Pantone 2118 C', '#353579'), ('Pantone 9999 C', None)])
async def test_intake_exact_pantone_preview_or_explicit_unknown(installed, publisher, monkeypatch, code, hex_code):
    from filabrain import color_standards, research
    async def proposal(_db, _data, **_):
        return {'target': {'mode': 'create', 'identity': {}}, 'suggestions': [],
                'issues': [{'code': 'manufacturer_color_reference', 'name': code, 'url': 'https://paramount-3d.com/blue'}]}
    import io

    from PIL import Image
    image = io.BytesIO()
    Image.new('RGB', (16, 16), (53, 53, 121)).save(image, 'WEBP', lossless=True)
    asset = color_standards.PANTONE_ASSETS + 'pantone-color-chip-2118-c.webp'
    async def read(url):
        if not hex_code:
            return b''
        return image.getvalue() if url == asset else f'<img src="{asset}" alt="PANTONE 2118 C Chip">'.encode()
    monkeypatch.setattr(research, 'research', proposal)
    monkeypatch.setattr(color_standards, 'read_asset', read)
    response = await installed.post(f'{BASE}/agent/intake', json={'query': 'Autobot Blue'})
    assert response.status_code == 200, response.text
    preview = response.json()['color_previews'][0]
    assert preview['code'] == code
    assert preview['hex_code'] == hex_code


async def test_intake_ambiguous_identity_keeps_draft_empty(installed, monkeypatch):
    from filabrain import research
    async def proposal(_db, _data, **_):
        return {'target': {'mode': 'create', 'identity': {}},
                'suggestions': [{'field': 'manufacturer_color_name', 'value': 'Wrong purple', 'sources': []}],
                'issues': [{'code': 'ambiguous_match', 'requires_product_choice': True}]}
    monkeypatch.setattr(research, 'research', proposal)
    response = await installed.post(f'{BASE}/agent/intake', json={'query': 'purple'})
    assert response.status_code == 200, response.text
    assert response.json()['requires_product_choice'] is True
    assert response.json()['filament_draft'] == {}


async def test_intake_unavailable_spool_sources_report_partial_coverage(installed, monkeypatch):
    from filabrain import agent_tools, research
    async def proposal(_db, _data, **_):
        return {'target': {'mode': 'create', 'identity': {'manufacturer': 'Paramount 3D', 'designation': 'PLA'}}, 'suggestions': [], 'issues': []}
    async def unavailable(*_):
        return {'spool_profiles': [], 'source_status': 'unavailable'}
    monkeypatch.setattr(research, 'research', proposal)
    monkeypatch.setattr(agent_tools, 'spool_candidates', unavailable)
    response = await installed.post(f'{BASE}/agent/intake', json={'query': 'PLA'})
    assert response.status_code == 200, response.text
    assert response.json()['complete'] is False
    assert 'spool_lookup_incomplete' in response.json()['notices']


async def test_intake_warns_about_saved_format_equivalent_sku(installed, sku_sources, publisher, db_session):
    from app.models import Manufacturer
    maker = await db_session.scalar(select(Manufacturer).where(Manufacturer.name == 'Paramount 3D'))
    saved = Filament(manufacturer_id=maker.id, designation='PLA Purple', material_type='PLA', diameter_mm=1.75,
                     manufacturer_sku='PRL4007_7449C', manufacturer_color_name='Decepticon Purple', raw_material_weight_g=1000)
    db_session.add(saved)
    await db_session.commit()
    identifier = saved.id
    response = await installed.post(f'{BASE}/agent/intake', json={'sku_hint': SKU, 'product_url': URL})
    assert response.status_code == 200, response.text
    assert any(issue.get('filament_id') == identifier and issue['code'] == 'duplicate' for issue in response.json()['proposal']['issues']), response.json()['filament_draft']


@pytest.mark.parametrize('weight', [-1, 0, 50001])
async def test_intake_rejects_invalid_identity_package_at_boundary(installed, weight):
    response = await installed.post(f'{BASE}/agent/intake', json={'identity': {'designation': 'PLA', 'weight_g': weight}})
    assert response.status_code == 422, response.text


async def test_real_catalog_outage_and_unusable_source_package_keep_partial_draft(installed, monkeypatch):
    from filabrain import research, spool_profiles
    async def proposal(*_, **__):
        return {'target': {'mode': 'create', 'identity': {}}, 'suggestions': [
            {'field': 'designation', 'value': 'PLA', 'sources': []},
            {'field': 'raw_material_weight_g', 'value': -1, 'sources': []}], 'issues': [{'code': 'unavailable_source'}]}
    async def empty(*_, **__):
        return []
    monkeypatch.setattr(research, 'research', proposal)
    monkeypatch.setattr(spool_profiles, '_catalog', empty)
    monkeypatch.setattr(spool_profiles, 'search_spool_cards', empty)
    response = await installed.post(f'{BASE}/agent/intake', json={'query': 'PLA'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['complete'] is False
    assert 'product_lookup_incomplete' in result['notices']
    assert 'package_weight_unverified' in result['notices']
    assert 'spool_lookup_incomplete' in result['notices']
    assert result['proposal']['suggestions']


async def test_intake_keeps_partial_fields_when_research_spends_its_network_budget(installed, monkeypatch):
    import asyncio

    from filabrain import agent_tools, research
    monkeypatch.setattr(agent_tools, 'INTAKE_RESEARCH_SECONDS', 0.01, raising=False)
    async def budgeted(_db, _data, *, deadline):
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.sleep(60)
        except TimeoutError:
            return {'target': {'mode': 'create', 'identity': {}}, 'suggestions': [
                {'field': 'manufacturer_color_name', 'value': 'Verified red', 'sources': []}],
                'issues': [{'code': 'unavailable_source', 'source': 'Configured model'}]}
    monkeypatch.setattr(research, 'research', budgeted)
    response = await installed.post(f'{BASE}/agent/intake', json={'query': 'Paramount red'})
    assert response.status_code == 200, response.text
    assert response.json()['filament_draft']['manufacturer_color_name'] == 'Verified red'
    assert response.json()['complete'] is False
