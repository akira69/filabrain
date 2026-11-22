from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from app.models import Manufacturer

from test_backend import BASE, installed

pytest_plugins = ('tests.conftest',)
pytestmark = pytest.mark.asyncio
URL = 'https://www.paramount-3d.com/product-page/pla-decepticon-purple-1-75mm-1kg-filament-prl40077449c'
SKU = 'PRL40077449C'


@pytest_asyncio.fixture
async def sku_sources(installed, db_session, monkeypatch):
    from filabrain import proposals
    db_session.add(Manufacturer(name='Paramount 3D', url='https://www.paramount-3d.com'))
    await db_session.commit()
    queries = []
    async def search(_origin, query):
        queries.append(query)
        return [URL] if query == SKU else []
    async def supplier(url, **_):
        if url != URL:
            raise ValueError('No fixture page')
        title = f'PLA (Decepticon Purple) 1.75mm 1kg Filament [{SKU}]'
        return proposals.Evidence(url, title, f'{title}\nSKU: {SKU}\nMaterial: PLA\nColor: Purple\nRAL Color: 4007 Purple / Decepticon',
            product={'manufacturer': 'Paramount 3D', 'designation': title, 'material': 'PLA', 'sku': SKU},
            scopes=(title, f'SKU: {SKU}', 'Material: PLA', 'RAL Color: 4007 Purple / Decepticon'))
    monkeypatch.setattr(proposals, 'search_web', search)
    monkeypatch.setattr(proposals, 'fetch_supplier', supplier)
    monkeypatch.setattr(proposals, 'fetch_archived_supplier', AsyncMock(return_value=None))
    monkeypatch.setattr(proposals, 'paramount_product_urls', AsyncMock(return_value=(URL,)))
    monkeypatch.setattr(proposals, 'manufacturer_website', AsyncMock(return_value=None))
    monkeypatch.setattr(proposals, 'catalog_candidates', AsyncMock(return_value=([], '', True)))
    monkeypatch.setattr(proposals, 'ofd_candidates', AsyncMock(return_value=([], '')))
    monkeypatch.delenv('FILABRAIN_MODEL_URL', raising=False)
    monkeypatch.delenv('FILABRAIN_MODEL_ID', raising=False)
    assert (await installed.put(f'{BASE}/settings', json={'search_url': 'http://search.test'})).is_success
    return queries


@pytest.mark.parametrize('brand', ['', 'Paramount 3D'])
@pytest.mark.parametrize('hint', ['PRL4007_7449C', 'PRL4007-7449C', SKU])
async def test_sku_only_resolves_manufacturer_product_without_material(installed, sku_sources, brand, hint):
    response = await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'identity': {'manufacturer': brand}, 'sku_hint': hint})
    assert response.is_success, response.text
    result = response.json()
    assert result['suggestions'] == [], 'discovery must not populate an unselected match'
    match = next(issue for issue in result['issues'] if issue['code'] == 'possible_match')
    assert match['product_url'] == URL
    assert match['identity']['sku'] == SKU
    selected = await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'identity': {'manufacturer': brand}, 'sku_hint': hint, 'product_url': URL,
    })
    assert selected.is_success, selected.text
    result = selected.json()
    fields = {item['field']: item['value'] for item in result['suggestions']}
    assert fields.get('manufacturer_sku') == SKU, result
    assert fields.get('material_type') == 'PLA', result
    assert fields.get('designation')
    assert fields.get('shop_url') == URL
    assert not any(issue['code'] == 'possible_match' for issue in result['issues']), 'verified choice should show its draft, not other products'
    assert any(issue.get('name', '').startswith('RAL 4007') for issue in result['issues'])


async def test_sku_discovery_does_not_override_conflicting_material(installed, sku_sources):
    result = (await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'identity': {'manufacturer': 'Paramount 3D', 'material': 'PETG'}, 'sku_hint': 'PRL4007_7449C',
    })).json()
    assert not any(item['field'] == 'material_type' and item['value'] == 'PLA' for item in result['suggestions'])


async def test_search_outage_is_not_reported_as_no_match(installed, sku_sources, monkeypatch):
    from filabrain import proposals
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(side_effect=httpx.ConnectError('offline')))
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'sku_hint': 'UNKNOWN-123'})).json()
    assert any(issue['code'] == 'search_incomplete' for issue in result['issues']), result
    assert not any(issue['code'] == 'no_match' for issue in result['issues']), result


async def test_paramount_sitemap_sku_search_needs_no_material(monkeypatch):
    from filabrain import proposals
    monkeypatch.setattr(proposals, 'paramount_product_urls', AsyncMock(return_value=(URL,)))
    assert await proposals._paramount_candidates(proposals.Identity(manufacturer='Paramount 3D'), 'PRL4007_7449C') == [URL]


async def test_general_text_discovery_finds_candidates_without_inferring_values(installed, sku_sources):
    result = (await installed.post(f"{BASE}/proposals", json={"mode": "create", "query": "Paramount 3D PLA purple"})).json()
    assert result["suggestions"] == []
    candidate = next(issue for issue in result["issues"] if issue["code"] == "possible_match")
    assert candidate["identity"]["color"] == "Decepticon Purple"
    assert candidate["product_url"] == URL
    assert candidate["match_kind"] == "search"


async def test_nearby_sku_is_a_candidate_never_an_automatic_correction(installed, sku_sources):
    result = (await installed.post(f"{BASE}/proposals", json={
        "mode": "create", "identity": {"manufacturer": "Paramount 3D"}, "sku_hint": "PRL40077448C",
    })).json()
    assert result["suggestions"] == []
    candidate = next(issue for issue in result["issues"] if issue["code"] == "possible_match")
    assert candidate["match_kind"] == "similar_sku"
    assert candidate["identity"]["sku"] == SKU


async def test_conflicting_sku_is_not_replaced_when_product_url_is_selected(installed, sku_sources):
    result = (await installed.post(f"{BASE}/proposals", json={
        "mode": "create", "identity": {"manufacturer": "Paramount 3D"}, "sku_hint": "PRL40077448C", "product_url": URL,
    })).json()
    assert not any(item["field"] == "manufacturer_sku" for item in result["suggestions"]), result


async def test_interactive_lookup_returns_when_a_supplier_exceeds_its_budget(installed, sku_sources, monkeypatch):
    import asyncio
    from filabrain import proposals, research
    monkeypatch.setattr(research, 'INTERACTIVE_RESEARCH_SECONDS', .01, raising=False)
    async def slow(*args, **kwargs):
        await asyncio.sleep(1)
        raise AssertionError('supplier should have been canceled')
    monkeypatch.setattr(proposals, 'fetch_supplier', slow)
    started = asyncio.get_running_loop().time()
    response = await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'product_url': URL, 'sku_hint': SKU,
    })
    assert response.is_success, response.text
    assert asyncio.get_running_loop().time() - started < .5
    assert any(issue['code'] in {'unavailable_source', 'search_incomplete'} for issue in response.json()['issues'])


async def test_discovery_and_selected_alias_work_for_other_manufacturers(installed, sku_sources, db_session, monkeypatch):
    from filabrain import proposals
    site = 'https://example-company.test'
    url = site + '/products/petg-ocean-blue-ex1234'
    db_session.add(Manufacturer(name='Example Company', url=site))
    await db_session.commit()
    async def search(_origin, _query):
        return [url]
    async def fetch(link, **kwargs):
        assert link == url
        return proposals.Evidence(url, 'PETG Ocean Blue', 'PETG Ocean Blue. SKU: EX1234. Diameter: 1.75 mm.',
            product={'manufacturer': 'Example Company', 'designation': 'PETG Ocean Blue', 'material': 'PETG',
                     'sku': 'EX1234', 'color': 'Ocean Blue', 'diameter_mm': 1.75})
    monkeypatch.setattr(proposals, 'search_web', search)
    monkeypatch.setattr(proposals, 'fetch_supplier', fetch)
    result = (await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'query': 'Example Company PETG blue',
    })).json()
    assert result['suggestions'] == []
    match = next(issue for issue in result['issues'] if issue['code'] == 'possible_match')
    assert match['identity']['manufacturer'] == 'Example Company'
    result = (await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'sku_hint': 'EX_1234', 'product_url': url,
    })).json()
    fields = {item['field']: item['value'] for item in result['suggestions']}
    assert fields.get('manufacturer_sku') == 'EX1234', result
    assert fields.get('material_type') == 'PETG'


async def test_discovery_rejects_standardization_before_network(installed, sku_sources):
    response = await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'action': 'standardize', 'query': 'purple'})
    assert response.status_code == 422
    assert sku_sources == []


async def test_discovery_uses_final_source_url_and_deduplicates_redirects(installed, sku_sources, monkeypatch):
    from dataclasses import replace
    from filabrain import proposals
    original = proposals.fetch_supplier
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(return_value=['https://retailer.test/redirect-a', 'https://retailer.test/redirect-b']))
    async def redirected(url, **kwargs):
        return replace(await original(URL, **kwargs), url=URL)
    monkeypatch.setattr(proposals, 'fetch_supplier', redirected)
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': 'purple'})).json()
    cards = [issue for issue in result['issues'] if issue['code'] == 'possible_match']
    assert len(cards) == 1
    assert cards[0]['product_url'] == URL
    assert cards[0]['source'] == 'paramount-3d.com'


async def test_normalized_sku_search_continues_after_nearby_match(installed, sku_sources, monkeypatch):
    from dataclasses import replace
    from filabrain import discovery, proposals
    monkeypatch.setattr(discovery, 'publisher_indexes', lambda *_args, **_kwargs: [])
    original = proposals.fetch_supplier
    near = URL.replace(SKU.lower(), 'prl40077448c')
    async def search(_origin, query):
        sku_sources.append(query)
        return [URL] if query == SKU else [near]
    async def fetch(url, **kwargs):
        page = await original(URL, **kwargs)
        return replace(page, url=near, title=page.title.replace(SKU, 'PRL40077448C'),
                       text=page.text.replace(SKU, 'PRL40077448C'),
                       scopes=tuple(scope.replace(SKU, 'PRL40077448C') for scope in page.scopes),
                       product={**page.product, 'sku': 'PRL40077448C',
                                'designation': page.product['designation'].replace(SKU, 'PRL40077448C')}) if url == near else page
    monkeypatch.setattr(proposals, 'search_web', search)
    monkeypatch.setattr(proposals, 'fetch_supplier', fetch)
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': 'PRL4007_7449C'})).json()
    cards = [issue for issue in result['issues'] if issue['code'] == 'possible_match']
    assert cards[0]['match_kind'] == 'sku_format'
    assert result['discovery_sku'] == 'PRL4007_7449C'
    assert sku_sources == ['PRL4007_7449C', SKU]


async def test_selected_sku_checks_duplicates_with_resolved_manufacturer(installed, sku_sources, db_session):
    from app.models import Filament
    other = Manufacturer(name='Unrelated Manufacturer')
    db_session.add(other)
    await db_session.flush()
    db_session.add(Filament(manufacturer_id=other.id, designation='Unrelated product', material_type='PLA', diameter_mm=1.75, manufacturer_sku=SKU))
    await db_session.commit()
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'sku_hint': SKU, 'product_url': URL})).json()
    assert not any(issue['code'] == 'duplicate' for issue in result['issues'])
    assert any(item['field'] == 'manufacturer_sku' and item['value'] == SKU for item in result['suggestions'])


async def test_manufacturer_index_outage_is_incomplete_not_no_match(installed, sku_sources, monkeypatch):
    from filabrain import proposals
    monkeypatch.setattr(proposals, 'paramount_product_urls', AsyncMock(side_effect=ValueError('Manufacturer product index unavailable')))
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(return_value=[]))
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'identity': {'manufacturer': 'Paramount 3D'}, 'sku_hint': SKU})).json()
    assert any(issue['code'] == 'search_incomplete' for issue in result['issues'])
    assert not any(issue['code'] == 'no_match' for issue in result['issues'])


@pytest.mark.parametrize('sku', ['A12', 'P/123'])
async def test_literal_skus_still_resolve_selected_structured_page(installed, sku_sources, monkeypatch, sku):
    from filabrain import proposals
    async def fetch(url, **kwargs):
        return proposals.Evidence(url, 'PLA Purple', f'SKU: {sku}', product={
            'manufacturer': 'Paramount 3D', 'designation': 'PLA Purple', 'material': 'PLA', 'sku': sku, 'mpn': sku,
        })
    monkeypatch.setattr(proposals, 'fetch_supplier', fetch)
    result = (await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'sku_hint': sku, 'product_url': 'https://shop.test/products/purple',
    })).json()
    assert any(item['field'] == 'manufacturer_sku' and item['value'] == sku for item in result['suggestions']), result


async def test_short_sku_keeps_exact_sitemap_match(monkeypatch):
    from filabrain import proposals
    url = 'https://maker.test/products/a12'
    monkeypatch.setattr(proposals, 'supplier_sitemap_urls', AsyncMock(return_value=(url,)))
    found = await proposals._manufacturer_sitemap_candidates('https://maker.test', 'maker.test', proposals.Identity(), 'A12')
    assert found == [url]


async def test_discovery_dropped_pages_are_incomplete_not_no_match(installed, sku_sources, monkeypatch):
    from filabrain import proposals
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(return_value=[f'https://shop.test/product-{i}' for i in range(5)]))
    monkeypatch.setattr(proposals, 'fetch_supplier', AsyncMock(return_value=proposals.Evidence('https://shop.test/product', 'Store', 'No structured product')))
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': 'purple'})).json()
    assert any(issue['code'] == 'search_incomplete' for issue in result['issues'])
    assert not any(issue['code'] == 'no_match' for issue in result['issues'])


@pytest.mark.parametrize('clue', ['PA12', 'PA6-CF20', 'eSUN3D', 'RAL4007', 'Pantone7449C'])
async def test_material_clue_is_not_forced_to_be_an_sku(installed, sku_sources, monkeypatch, clue):
    from filabrain import proposals
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(return_value=['https://maker.test/pa12']))
    monkeypatch.setattr(proposals, 'fetch_supplier', AsyncMock(return_value=proposals.Evidence(
        'https://maker.test/pa12', 'Natural PA12', 'PA12 filament', product={
            'manufacturer': 'Maker', 'designation': 'Natural PA12', 'sku': 'NAT-PA12-1KG', 'material': 'PA12',
        })))
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': clue})).json()
    assert any(issue['code'] == 'possible_match' for issue in result['issues'])
    assert result['discovery_sku'] == ''


@pytest.mark.parametrize('brand', ['', 'Polymaker'])
@pytest.mark.parametrize('query', ['PA02001', 'PA02-001', 'PA0200'])
async def test_known_catalog_sku_is_discoverable_without_web(installed, sku_sources, monkeypatch, brand, query):
    from filabrain import proposals
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(return_value=[]))
    result = (await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'identity': {'manufacturer': brand}, 'query': query,
    })).json()
    assert result['suggestions'] == []
    candidates = [issue for issue in result['issues'] if issue['code'] == 'possible_match']
    candidate = next((issue for issue in candidates if issue['identity']['sku'] == 'PA02001'), None)
    assert candidate, result
    assert candidate['identity']['manufacturer'] == 'Polymaker'
    assert 'polymaker.com' in candidate['product_url']
    assert candidate['identity']['material'].upper() == 'PLA'
    assert candidate['url'].startswith('https://')


async def test_named_catalog_clues_work_when_pages_are_blocked(installed, sku_sources, monkeypatch):
    from filabrain import proposals
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(side_effect=httpx.ConnectError('offline')))
    result = (await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'query': 'Bambu Lab PLA Basic Bambu Green 10501',
    })).json()
    assert result['suggestions'] == []
    candidates = [issue for issue in result['issues'] if issue['code'] == 'possible_match']
    assert any(issue['identity']['sku'] == '10501' and issue['identity']['manufacturer'] == 'Bambu Lab' for issue in candidates), result


async def test_fresh_live_catalog_product_is_a_selectable_candidate(installed, sku_sources, monkeypatch):
    from filabrain import proposals
    url = 'https://new-maker.test/products/fresh-pla'
    monkeypatch.setattr(proposals, 'catalog_candidates', AsyncMock(return_value=([
        {'name': 'Fresh PLA', 'manufacturer': {'name': 'New Maker'}, 'material': {'key': 'PLA'},
         'sku': 'NEW12345', 'shop_url': url, 'diameter_mm': 1.75, 'nominal_weight_g': 1000},
    ], 'https://db.filaman.app/api/v1/filaments?search=NEW12345', True)))
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(return_value=[]))
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': 'NEW12345'})).json()
    assert result['suggestions'] == []
    candidate = next((issue for issue in result['issues'] if issue['code'] == 'possible_match'), None)
    assert candidate and candidate['identity']['sku'] == 'NEW12345', result
    assert candidate['product_url'] == url
    assert candidate['source'] == 'FilaManDB'


async def test_retailer_code_can_find_product_without_becoming_manufacturer_sku(installed, sku_sources, monkeypatch):
    from filabrain import proposals
    url = 'https://retailer.test/products/black-pla'
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(return_value=[url]))
    monkeypatch.setattr(proposals, 'fetch_supplier', AsyncMock(return_value=proposals.Evidence(
        url, 'Black PLA', 'Retailer SKU M3RRH23H. Manufacturer part number PA02001.',
        product={'manufacturer': 'Polymaker', 'designation': 'Black PLA', 'sku': 'M3RRH23H', 'mpn': 'PA02001'},
    )))
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': 'M3RRH23H'})).json()
    card = next((issue for issue in result['issues'] if issue['code'] == 'possible_match'), None)
    assert card, result
    assert card['identity']['sku'] == 'PA02001'
    assert card['source_product_code'] == 'M3RRH23H'
    assert result['suggestions'] == []


@pytest.mark.parametrize('query,maker,code', [
    ('10501', 'Bambu Lab', '10501'),
    ('Bambu 10501', 'Bambu Lab', '10501'),
    ('Polymaker PA02001', 'Polymaker', 'PA02001'),
    ('Polymaker PA02-001', 'Polymaker', 'PA02001'),
])
async def test_short_codes_and_brand_code_clues_use_catalogs(installed, sku_sources, monkeypatch, query, maker, code):
    from filabrain import proposals
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(return_value=[]))
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': query})).json()
    assert result['suggestions'] == []
    assert any(issue['code'] == 'possible_match' and issue['identity']['manufacturer'] == maker
               and issue['identity']['sku'] == code for issue in result['issues']), result


async def test_selected_catalog_sku_keeps_proposals_when_product_page_is_blocked(installed, sku_sources, monkeypatch):
    from filabrain import proposals
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(return_value=[]))
    monkeypatch.setattr(proposals, 'fetch_supplier', AsyncMock(side_effect=ValueError('HTTP 503')))
    result = (await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'sku_hint': 'PA02001',
        'product_url': 'https://us.polymaker.com/products/polylite-pla?variant=43818025812025',
    })).json()
    fields = {item['field']: item['value'] for item in result['suggestions']}
    assert fields.get('manufacturer_sku') == 'PA02001', result
    assert fields.get('material_type') == 'PLA'
    assert fields.get('diameter_mm') == 1.75
    assert fields.get('raw_material_weight_g') == 1000
    assert fields.get('manufacturer_color_name') == 'Black'
    assert any('bundled' in source['title'].lower() for item in result['suggestions'] for source in item['sources'])


@pytest.mark.parametrize('query', ['Paramount 3D PLA purple', 'PRL40077449C'])
async def test_discovery_follow_up_guidance_changes_retrieval(installed, sku_sources, query):
    guidance = 'Check a supplier datasheet too'
    response = await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': query, 'guidance': guidance})
    assert response.is_success
    assert any(guidance in term and query in term for term in sku_sources), 'follow-up instructions must reach discovery search'


async def test_existing_filament_uses_shared_discovery_without_changing_target(installed, sku_sources, db_session):
    from sqlalchemy import select
    from app.models import Filament
    maker = await db_session.scalar(select(Manufacturer).where(Manufacturer.name == 'Paramount 3D'))
    filament = Filament(manufacturer_id=maker.id, designation='PLA (Decepticon Purple)', material_type='PLA',
                        diameter_mm=1.75, manufacturer_sku=SKU, manufacturer_color_name='Decepticon Purple')
    db_session.add(filament)
    await db_session.commit()
    filament_id = filament.id
    response = await installed.post(f'{BASE}/proposals', json={
        'mode': 'edit', 'filament_id': filament_id, 'query': 'PRL4007_7449C',
    })
    assert response.is_success, response.text
    result = response.json()
    assert result['target']['mode'] == 'edit'
    assert result['target']['filament_id'] == filament_id
    assert result['suggestions'] == []
    assert any(issue.get('product_url') == URL and issue['code'] == 'possible_match' for issue in result['issues'])
    await db_session.refresh(filament)
    assert filament.designation == 'PLA (Decepticon Purple)'
    assert filament.manufacturer_sku == SKU


async def test_editor_discovery_keeps_identity_and_record_checks(installed, sku_sources, db_session):
    from sqlalchemy import select
    from app.models import Filament
    maker = await db_session.scalar(select(Manufacturer).where(Manufacturer.name == 'Paramount 3D'))
    filament = Filament(manufacturer_id=maker.id, designation='PLA Purple', material_type='PLA', diameter_mm=1.75)
    db_session.add(filament)
    await db_session.commit()
    for changed in [{'record_token': '0' * 32}, {'identity': {'manufacturer': 'Another manufacturer'}}]:
        response = await installed.post(f'{BASE}/proposals', json={
            'mode': 'edit', 'filament_id': filament.id, 'query': 'Decepticon Purple', **changed,
        })
        assert response.status_code == 409
    assert sku_sources == []
