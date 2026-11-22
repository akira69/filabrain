"""Official URL snapshots seed discovery, never supply product identities."""
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from app.core.cache import TTLCache
from test_backend import BASE
from test_backend import installed as installed_fixture
from test_sku_discovery import SKU, URL
from test_sku_discovery import sku_sources as sku_fixture

installed = installed_fixture
sku_sources = sku_fixture
pytest_plugins = ('tests.conftest',)
pytestmark = pytest.mark.asyncio
GREEN = 'https://www.paramount-3d.com/product-page/pla-military-green-1-75mm-1kg-filament-ogrl60037764c'


@pytest_asyncio.fixture
async def publisher_sources(sku_sources, monkeypatch):
    from filabrain import proposals, sources
    client = httpx.AsyncClient
    state = {'status': 503, 'body': b'', 'missing': False}

    def index(request):
        assert str(request.url) == 'https://www.paramount-3d.com/store-products-sitemap.xml'
        return httpx.Response(state['status'], content=state['body'])

    monkeypatch.setattr(sources.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(index), **kwargs))
    monkeypatch.setattr(sources, '_product_sitemaps', TTLCache())
    monkeypatch.setattr(proposals, 'paramount_product_urls', sources.paramount_product_urls)
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(side_effect=sources.SearchEnginesUnavailable('Engines unavailable')))

    async def page(url, **_):
        if state['missing'] or url not in (URL, GREEN):
            raise ValueError('HTTP 404')
        color, sku = ('Decepticon Purple', SKU) if url == URL else ('Military Green', 'OGRL60037764C')
        title = f'PLA ({color}) 1.75mm 1kg Filament [{sku}]'
        return proposals.Evidence(url, title, title, manufacturer_page=True, product={
            'manufacturer': 'Paramount 3D', 'designation': title, 'sku': sku, 'material': 'PLA',
        }, scopes=(title,))

    monkeypatch.setattr(proposals, 'fetch_supplier', page)
    return state


@pytest.mark.parametrize('payload', [
    {'sku_hint': SKU}, {'sku_hint': 'PRL4007_7449C'}, {'query': 'PRL4007-7449C'},
    {'query': 'PRL4007 7449C'}, {'query': 'Paramount PRL4007_7449C'},
    {'query': 'Paramount PLA purple'}, {'query': 'Paramount Decepticon Purple'},
])
async def test_sparse_product_clues_find_verified_cards_with_search_and_live_index_down(installed, publisher_sources, payload):
    response = await installed.post(f'{BASE}/proposals', json={'mode': 'create', **payload})
    assert response.is_success, response.text
    result = response.json()
    assert result['suggestions'] == []
    card = next((item for item in result['issues'] if item['code'] == 'possible_match' and item['product_url'] == URL), None)
    assert card, result
    assert card['identity']['manufacturer'] == 'Paramount 3D'
    assert card['identity']['sku'] == SKU
    assert card['identity']['material'] == 'PLA'


async def test_index_lookup_is_not_specific_to_one_product(installed, publisher_sources):
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': 'OGRL6003_7764C'})).json()
    assert result['suggestions'] == []
    assert any(item['code'] == 'possible_match' and item['product_url'] == GREEN
               and item['identity']['sku'] == 'OGRL60037764C' for item in result['issues']), result


@pytest.mark.parametrize('payload', [
    {'identity': {'manufacturer': 'Polymaker'}, 'sku_hint': SKU},
    {'identity': {'material': 'PETG'}, 'sku_hint': SKU},
    {'identity': {'color': 'Green'}, 'sku_hint': SKU},
    {'identity': {'diameter_mm': 3}, 'sku_hint': SKU},
    {'identity': {'weight_g': 500}, 'sku_hint': SKU},
    {'query': f'Polymaker {SKU}'}, {'query': f'PETG {SKU}'},
])
async def test_index_inference_never_overrides_explicit_constraints(installed, publisher_sources, payload):
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', **payload})).json()
    assert result['suggestions'] == []
    assert not any(item['code'] == 'possible_match' for item in result['issues']), result


async def test_bundled_urls_do_not_become_cards_when_product_pages_are_missing(installed, publisher_sources):
    publisher_sources['missing'] = True
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'sku_hint': SKU})).json()
    assert result['suggestions'] == []
    assert not any(item['code'] == 'possible_match' for item in result['issues'])
    assert any(item['code'] == 'search_incomplete' for item in result['issues'])


async def test_known_manufacturer_prefers_live_index_over_stale_bundled_urls(installed, publisher_sources):
    publisher_sources.update(status=200, body=b'<urlset><url><loc>https://www.paramount-3d.com/product-page/pla-military-green-1-75mm-1kg-filament-ogrl60037764c</loc></url></urlset>')
    result = (await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'identity': {'manufacturer': 'Paramount 3D'}, 'sku_hint': SKU,
    })).json()
    assert not any(item['code'] == 'possible_match' and item['product_url'] == URL for item in result['issues']), result


async def test_missing_bundle_and_offline_index_remain_incomplete(installed, publisher_sources, monkeypatch):
    from filabrain import knowledge
    original = knowledge._data

    def read(path):
        if path == 'publisher_indexes.json':
            raise FileNotFoundError(path)
        return original(path)

    monkeypatch.setattr(knowledge, '_data', read)
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'sku_hint': SKU})).json()
    assert result['suggestions'] == []
    assert not any(item['code'] == 'possible_match' for item in result['issues'])
    assert any(item['code'] == 'search_incomplete' for item in result['issues'])


async def test_a_color_standard_reference_does_not_become_a_manufacturer_sku(installed, publisher_sources):
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': 'Pantone 7449C'})).json()
    assert result['discovery_sku'] == ''


@pytest.mark.parametrize('brand', ['Paramount', 'Paramount 3D'])
async def test_conflicting_brand_clues_do_not_infer_a_publisher(installed, publisher_sources, brand):
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': f'Polymaker {brand} {SKU}'})).json()
    assert not any(item['code'] == 'possible_match' for item in result['issues']), result


async def test_shared_index_code_does_not_infer_a_manufacturer(installed, publisher_sources, monkeypatch):
    from filabrain import knowledge
    original = knowledge._data
    index = original('publisher_indexes.json')[0]
    other = {**index, 'manufacturer': 'Another Manufacturer', 'aliases': [],
             'website': 'https://manufacturer.example', 'product_urls': [f'https://manufacturer.example/products/{SKU.lower()}']}
    monkeypatch.setattr(knowledge, '_data', lambda path: [index, other] if path == 'publisher_indexes.json' else original(path))
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'sku_hint': SKU})).json()
    assert not any(item['code'] == 'possible_match' for item in result['issues']), result


async def test_url_code_never_overrides_the_verified_manufacturer_part_number(installed, publisher_sources, monkeypatch):
    from filabrain import proposals
    url = 'https://www.paramount-3d.com/product-page/petg-pantone-decepticon-purple-7449c-1-75mm-1kg-filament-prl40077449c'
    async def page(link, **_):
        if link != url:
            raise ValueError('HTTP 404')
        title = 'PETG (Decepticon Purple) 1.75mm 1kg Filament [PRL40077449G]'
        return proposals.Evidence(link, title, title, manufacturer_page=True, product={
            'manufacturer': 'Paramount 3D', 'designation': title, 'material': 'PETG', 'sku': SKU, 'mpn': 'PRL40077449G',
        }, scopes=(title,))
    monkeypatch.setattr(proposals, 'fetch_supplier', page)
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'sku_hint': SKU})).json()
    cards = [item for item in result['issues'] if item['code'] == 'possible_match']
    assert len(cards) == 1, result
    assert cards[0]['identity']['sku'] == 'PRL40077449G'
    assert cards[0]['source_product_code'] == SKU
    assert cards[0]['match_kind'] == 'similar_sku'
    assert result['suggestions'] == []


@pytest.mark.parametrize('body', [b'invalid XML', b'x' * (513 * 1024)])
async def test_unusable_live_indexes_fall_back_to_reviewed_product_links(publisher_sources, body):
    from filabrain import sources
    publisher_sources.update(status=200, body=body)
    urls = await sources.paramount_product_urls(strict=True)
    assert URL in urls and GREEN in urls
    assert all(url.startswith('https://www.paramount-3d.com/product-page/') for url in urls)


async def test_publisher_aliases_keep_only_official_index_links():
    from filabrain import knowledge
    index, = knowledge.publisher_indexes('Paramount')
    assert index['manufacturer'] == 'Paramount 3D'
    assert index['source'] == 'https://www.paramount-3d.com/store-products-sitemap.xml'
    assert index['reviewed_at'] == '2026-11-22'
    assert URL in index['product_urls'] and GREEN in index['product_urls']
    assert all(url.startswith('https://www.paramount-3d.com/product-page/') for url in index['product_urls'])
    assert knowledge.publisher_indexes('Unrelated manufacturer') == []


async def test_sku_lookup_needs_no_existing_manufacturer_record(installed, publisher_sources, db_session):
    from app.models import Manufacturer
    from sqlalchemy import delete
    await db_session.execute(delete(Manufacturer).where(Manufacturer.name == 'Paramount 3D'))
    await db_session.commit()
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'sku_hint': SKU})).json()
    assert any(item['code'] == 'possible_match' and item['product_url'] == URL
               and item['identity']['manufacturer'] == 'Paramount 3D' for item in result['issues']), result


@pytest.mark.parametrize('measurement', ['1.75mm', '1.75 mm', '1000g', '1000 g', '1kg'])
async def test_descriptive_measurements_do_not_become_product_codes(installed, publisher_sources, measurement):
    result = (await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'query': f'Paramount PLA purple {measurement}',
    })).json()
    assert result['discovery_sku'] == ''
    assert any(item['code'] == 'possible_match' and item['product_url'] == URL for item in result['issues']), result


async def test_measurements_do_not_infer_an_unmentioned_manufacturer(installed, publisher_sources):
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': 'PLA purple 1.75mm'})).json()
    assert result['discovery_sku'] == ''
    assert not any(item['code'] == 'possible_match' for item in result['issues']), result


async def test_loose_pantone_clue_remains_a_color_clue(installed, publisher_sources, monkeypatch):
    from filabrain import proposals
    url = 'https://www.paramount-3d.com/product-page/petg-pantone-decepticon-purple-7449c-1-75mm-1kg-filament-prl40077449c'
    async def page(link, **_):
        if link != url:
            raise ValueError('HTTP 404')
        title = 'PETG (Decepticon Purple) 1.75mm 1kg Filament [PRL40077449G]'
        return proposals.Evidence(link, title, title, manufacturer_page=True, product={
            'manufacturer': 'Paramount 3D', 'designation': title, 'sku': 'PRL40077449G', 'material': 'PETG',
        }, scopes=(title,))
    monkeypatch.setattr(proposals, 'fetch_supplier', page)
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'query': 'Paramount purple 7449C'})).json()
    assert result['discovery_sku'] == ''
    assert any(item['code'] == 'possible_match' and item['identity']['sku'] == 'PRL40077449G'
               for item in result['issues']), result


@pytest.mark.parametrize('sku', ['NATURAL', 'NAT/URAL'])
async def test_explicit_sku_keeps_existing_catalog_lookup(installed, publisher_sources, monkeypatch, sku):
    from filabrain import discovery
    row = {'maker': 'Another Manufacturer', 'product': 'Natural PLA', 'material': 'PLA',
           'manufacturer_sku': sku, 'product_url': 'https://manufacturer.example/products/natural',
           'url': 'https://catalog.example/source', 'source': 'Public catalog'}
    monkeypatch.setattr(discovery, 'lookup_catalog_code', lambda code, *_args, **_kwargs: [row] if code == sku else [])
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'sku_hint': sku})).json()
    assert any(item['code'] == 'possible_match' and item['identity']['manufacturer'] == 'Another Manufacturer'
               and item['identity']['sku'] == sku for item in result['issues']), result


async def test_numeric_code_after_four_description_words_keeps_catalog_lookup(installed, publisher_sources):
    result = (await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'query': 'please find my Polymaker PA02001',
    })).json()
    assert any(item['code'] == 'possible_match' and item['identity']['manufacturer'] == 'Polymaker'
               and item['identity']['sku'] == 'PA02001' for item in result['issues']), result
