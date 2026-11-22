import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from app.models import Manufacturer

from test_backend import BASE, installed

pytest_plugins = ('tests.conftest',)
pytestmark = pytest.mark.asyncio
SITE = 'https://maker.example'
RETAILER = 'https://retailer.example/products/pla-blue?variant=blue'
MERCHANT_SKU = 'STORE-1234'
MPN = 'MAKER-5678'


@pytest_asyncio.fixture
async def authority_sources(installed, db_session, monkeypatch):
    from filabrain import proposals
    db_session.add(Manufacturer(name='Example Maker', url=SITE))
    await db_session.commit()
    pages = {}
    async def supplier(url, **_):
        if url not in pages:
            raise ValueError('No fixture page')
        return pages[url]
    monkeypatch.setattr(proposals, 'fetch_supplier', supplier)
    monkeypatch.setattr(proposals, 'fetch_archived_supplier', AsyncMock(return_value=None))
    monkeypatch.setattr(proposals, 'search_web', AsyncMock(return_value=[]))
    monkeypatch.setattr(proposals, 'manufacturer_website', AsyncMock(return_value=None))
    monkeypatch.setattr(proposals, 'catalog_candidates', AsyncMock(return_value=([], '', True)))
    monkeypatch.setattr(proposals, 'ofd_candidates', AsyncMock(return_value=([], '')))
    monkeypatch.delenv('FILABRAIN_MODEL_URL', raising=False)
    monkeypatch.delenv('FILABRAIN_MODEL_ID', raising=False)
    return pages


def product_page(url=RETAILER, *, mpn=None, sku=MERCHANT_SKU):
    from filabrain.sources import Evidence, _ProductPage
    row = {'@type': 'Product', 'name': 'PLA Blue', 'brand': {'name': 'Example Maker'},
           'material': 'PLA', 'color': 'Blue', 'sku': sku, 'url': url}
    if mpn:
        row['mpn'] = mpn
    parser = _ProductPage()
    parser.feed('<script type="application/ld+json">' + json.dumps(row) + '</script>')
    product, unresolved = parser.product_for_url(url)
    text = f'PLA Blue\nMaterial: PLA\nColor: Blue\nSKU: {sku}\nDensity: 1.24 g/cm3'
    return Evidence(url, 'PLA Blue', text, product=product, scopes=(text,), selected_variant_unresolved=unresolved)


async def test_product_metadata_keeps_merchant_sku_and_explicit_mpn_independent():
    product = product_page(mpn=MPN).product
    assert product['sku'] == MERCHANT_SKU
    assert product.get('mpn') == MPN


@pytest.mark.parametrize(('url', 'mpn', 'expected'), [
    (RETAILER, None, None), (RETAILER, MPN, MPN),
    (SITE + '/products/pla-blue', None, MERCHANT_SKU),
])
async def test_selected_product_requires_manufacturer_sku_authority(installed, authority_sources, url, mpn, expected):
    authority_sources[url] = product_page(url, mpn=mpn)
    response = await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'product_url': url})
    assert response.is_success, response.text
    result = response.json()
    fields = {item['field']: item['value'] for item in result['suggestions']}
    assert fields.get('manufacturer_sku') == expected, result
    assert fields.get('manufacturer_id'), result
    assert fields.get('designation') == 'PLA Blue', result
    assert fields.get('material_type') == 'PLA', result
    assert fields.get('shop_url') == url, result
    assert authority_sources[url].product['sku'] == MERCHANT_SKU


async def test_retailer_model_cannot_reintroduce_merchant_sku(installed, authority_sources, monkeypatch):
    from filabrain import proposals
    authority_sources[RETAILER] = product_page()
    assert (await installed.put(f'{BASE}/settings', json={'model_url': 'http://model.test', 'model_id': 'test'})).is_success
    monkeypatch.setattr(proposals, 'check_model_catalog', AsyncMock())
    extracted = AsyncMock(return_value=([{'field': 'manufacturer_sku', 'value': MERCHANT_SKU, 'sources': [
        {'url': RETAILER, 'title': 'PLA Blue', 'evidence': f'SKU: {MERCHANT_SKU}'},
    ]}], []))
    monkeypatch.setattr(proposals, 'request_extraction', extracted)
    result = (await installed.post(f'{BASE}/proposals', json={'mode': 'create', 'product_url': RETAILER})).json()
    extracted.assert_awaited()
    assert not any(item['field'] == 'manufacturer_sku' for item in result['suggestions']), result


async def test_explicit_manufacturer_sku_does_not_match_merchants_different_mpn(installed, authority_sources):
    authority_sources[RETAILER] = product_page(mpn=MPN)
    result = (await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'product_url': RETAILER, 'sku_hint': MERCHANT_SKU,
    })).json()
    assert not any(item['field'] == 'manufacturer_sku' for item in result['suggestions']), result


async def test_retailer_mpn_can_verify_specs_despite_different_merchant_code():
    from filabrain import proposals
    page = product_page(mpn=MPN)
    identity = proposals.Identity(**page.product).model_copy(update={'sku': MPN})
    assert proposals.supplier_matches(identity, page)


async def test_retailer_codes_do_not_create_false_cross_source_variant_conflicts():
    from filabrain import proposals
    identity = proposals.Identity(manufacturer='Example Maker', designation='PLA Blue', material='PLA', color='Blue')
    pages = [product_page(), product_page('https://other-retailer.example/blue', sku='OTHER-999')]
    assert not proposals._supplier_variants_disagree(pages, identity, None)


async def test_official_identity_can_use_catalog_manufacturer_website(db_session, monkeypatch):
    from filabrain import proposals
    website = AsyncMock(return_value=SITE)
    monkeypatch.setattr(proposals, 'manufacturer_website', website)
    candidate = await proposals.verified_product_identity(db_session, product_page(SITE + '/products/pla-blue'))
    assert candidate.sku == MERCHANT_SKU
    website.assert_awaited_once_with('Example Maker')


@pytest.mark.parametrize('sku_hint', ['', MERCHANT_SKU])
async def test_selected_product_authority_timeout_returns_safe_partial_result(
    installed, authority_sources, db_session, monkeypatch, sku_hint,
):
    import asyncio
    from sqlalchemy import update
    from filabrain import proposals, research

    await db_session.execute(update(Manufacturer).where(Manufacturer.name == 'Example Maker').values(url=None))
    await db_session.commit()
    authority_sources[RETAILER] = product_page()
    monkeypatch.setattr(research, 'INTERACTIVE_RESEARCH_SECONDS', .01)
    async def slow_website(_name):
        await asyncio.sleep(1)
        return SITE
    monkeypatch.setattr(proposals, 'manufacturer_website', slow_website)
    started = asyncio.get_running_loop().time()
    response = await installed.post(f'{BASE}/proposals', json={
        'mode': 'create', 'product_url': RETAILER, 'sku_hint': sku_hint,
    })
    assert asyncio.get_running_loop().time() - started < .5
    assert response.is_success, response.text
    result = response.json()
    fields = {item['field']: item['value'] for item in result['suggestions']}
    assert 'manufacturer_sku' not in fields, result
    assert any(issue['code'] in {'unavailable_source', 'search_incomplete'} for issue in result['issues']), result
    if not sku_hint:
        assert fields.get('designation') == 'PLA Blue', result
        assert fields.get('material_type') == 'PLA', result
    else:
        assert 'designation' not in fields, 'a timed-out authority lookup cannot satisfy an explicit SKU'
