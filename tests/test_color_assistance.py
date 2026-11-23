"""Color-form assistance only proposes publisher-backed values; native Save owns writes."""
import pytest
from app.models import Color
from sqlalchemy import func, select
from test_backend import BASE, installed
from test_color_review import publisher
from test_sku_discovery import SKU, URL, sku_sources

__all__ = ['installed', 'publisher', 'sku_sources']
pytest_plugins = ('tests.conftest',)
pytestmark = pytest.mark.asyncio
PATH = f'{BASE}/agent/color-intake'


async def test_exact_color_uses_publisher_and_saved_matches_without_inventory_write(installed, publisher, db_session, monkeypatch):
    from filabrain import research
    async def forbidden(*_, **__):
        pytest.fail('An exact color reference does not need product/model research')
    monkeypatch.setattr(research, 'research', forbidden)
    saved = Color(name='Existing red', hex_code='#AB1519')
    db_session.add(saved)
    await db_session.commit()
    response = await installed.post(PATH, json={'query': 'Traffic red (RAL3020)'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['action']['page'] == 'color'
    preview = result['proposal']['color_previews'][0]
    assert preview['canonical_name'] == 'Traffic red (RAL 3020)'
    assert preview['hex_code'] == '#AB1519'
    assert preview['matches'][0]['id'] == saved.id
    assert result['complete'] is True
    assert await db_session.scalar(select(func.count(Color.id))) == 1


async def test_unknown_color_never_offers_guessed_hex(installed, publisher):
    response = await installed.post(PATH, json={'query': 'RAL 9999'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['proposal']['color_previews'][0]['hex_code'] is None
    assert result['complete'] is False
    assert 'color_lookup_incomplete' in result['notices']


async def test_product_color_keeps_explicit_choice_and_skips_spool_catalogs(installed, sku_sources, publisher, monkeypatch):
    from filabrain import agent_tools
    async def forbidden(*_, **__):
        pytest.fail('Color research must not call spool-profile research')
    monkeypatch.setattr(agent_tools, 'spool_candidates', forbidden)
    found = await installed.post(PATH, json={'query': 'PRL4007_7449C'})
    assert found.status_code == 200, found.text
    proposal = found.json()['proposal']
    assert proposal['color_previews'] == []
    assert any(issue['code'] == 'possible_match' for issue in proposal['issues'])
    chosen = await installed.post(PATH, json={'sku_hint': SKU, 'product_url': URL})
    assert chosen.status_code == 200, chosen.text
    proposal = chosen.json()['proposal']
    assert proposal['color_previews'][0]['code'] == 'RAL 4007'
    assert not proposal.get('spool_profiles')


@pytest.mark.parametrize('payload', [{'query': 'x'*401}, {'query': 'RAL ٣٠٢٠'}, {'query': 'RAL 3020', 'apply': True}])
async def test_color_assistance_validates_bounded_read_only_input(installed, payload):
    assert (await installed.post(PATH, json=payload)).status_code == 422


async def test_pantone_uses_publisher_chip_and_canonical_name(installed, publisher, monkeypatch):
    import io

    from PIL import Image

    from filabrain import color_standards
    image = io.BytesIO()
    Image.new('RGB', (16, 16), (53, 53, 121)).save(image, 'WEBP', lossless=True)
    asset = color_standards.PANTONE_ASSETS + 'pantone-color-chip-2118-c.webp'
    async def read(url):
        return image.getvalue() if url == asset else f'<img src="{asset}" alt="PANTONE 2118 C Chip">'.encode()
    monkeypatch.setattr(color_standards, 'read_asset', read)
    result = await installed.post(PATH, json={'query': 'Pantone 2118 C'})
    assert result.status_code == 200, result.text
    preview = result.json()['proposal']['color_previews'][0]
    assert preview['hex_code'] == '#353579'
    assert preview['canonical_name'] == 'Pantone 2118 C'


async def test_read_permission_required_before_and_after_lookup(installed, publisher, monkeypatch):
    from app.api.deps import require_auth
    from app.core.security import Principal
    from app.main import app

    from filabrain import color_standards
    app.dependency_overrides[require_auth] = lambda: Principal(auth_type='token', is_superadmin=True, scopes=['colors:create'])
    try:
        assert (await installed.post(PATH, json={'query': 'RAL 3020'})).status_code == 403
        principal = Principal(auth_type='token', is_superadmin=True, scopes=['filaments:read'])
        app.dependency_overrides[require_auth] = lambda: principal
        async def revoked(_url):
            principal.scopes.clear()
            return b''
        monkeypatch.setattr(color_standards, 'read_asset', revoked)
        assert (await installed.post(PATH, json={'query': 'RAL 3020'})).status_code == 403
    finally:
        app.dependency_overrides.pop(require_auth, None)


async def test_product_research_timeout_returns_partial_read_result(installed, monkeypatch):
    from filabrain import research
    async def timeout(*_, **__):
        raise TimeoutError
    monkeypatch.setattr(research, 'research', timeout)
    response = await installed.post(PATH, json={'query': 'purple filament'})
    assert response.status_code == 200, response.text
    assert response.json()['complete'] is False
    assert response.json()['proposal']['color_previews'] == []
    assert 'product_lookup_incomplete' in response.json()['notices']


@pytest.mark.parametrize(('name', 'code', 'path'), [
    ('Anthracite RAL 7016', 'RAL 7016', 'data/francofil/PLA/pla/anthracite_ral_7016/sizes.json'),
    ('Vert Pantone 3268C', 'Pantone 3268 C', 'data/arianeplast/PLA/pla/vert_pantone_3268c/sizes.json'),
])
async def test_embedded_catalog_color_keeps_original_evidence_separate_from_publisher(installed, monkeypatch, name, code, path):
    from filabrain import agent_tools, research
    source = {'title': 'Open Filament Database', 'url': f'https://github.com/OpenFilamentCollective/open-filament-database/blob/60be55d3448c615c4938b949435b1b1c904668b4/{path}', 'evidence': name}
    async def proposal(*_, **__):
        return {'target': {'mode': 'create', 'identity': {}}, 'issues': [],
                'suggestions': [{'field': 'manufacturer_color_name', 'value': name, 'sources': [source]}]}
    async def publisher_preview(value):
        assert value == code
        return {'name': code, 'hex_code': '#123456', 'source': 'https://publisher.example/swatch'}
    monkeypatch.setattr(research, 'research', proposal)
    monkeypatch.setattr(agent_tools, 'resolve_preview', publisher_preview)
    result = await installed.post(PATH, json={'query': 'chosen product'})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['proposal']['suggestions'][0] == {'field': 'manufacturer_color_name', 'value': name, 'sources': [source]}
    preview = data['proposal']['color_previews'][0]
    assert preview['code'] == code
    assert preview['manufacturer_source'] is None, 'Catalog evidence must not become a manufacturer assertion'
    assert preview['source'] == 'https://publisher.example/swatch'
    assert data['complete'] is True
