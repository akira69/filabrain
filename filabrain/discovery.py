"""Bounded product discovery; candidate links require explicit selection."""
import asyncio
import re
from difflib import SequenceMatcher

from sqlalchemy import select
from app.models import Manufacturer

from . import proposals as product
from .knowledge import lookup_catalog_code, lookup_named_catalog
from .settings import config_record


def _queries(query: str, sku: str) -> list[str]:
    values = [query]
    if sku:
        key = product._sku_key(sku)
        if key and key != sku.casefold():
            values.extend((re.sub(r"[^a-zA-Z0-9]", "", sku), " ".join(product._product_key(sku))))
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))[:3]


async def discover_products(db, identity: product.Identity, query: str = "", sku_hint: str = "", guidance: str = "") -> dict:
    query = query.strip() or sku_hint.strip() or identity.sku.strip()
    search_query = " ".join(filter(None, (query, guidance.strip())))
    sku = sku_hint.strip() or identity.sku.strip()
    clue_sku = not sku
    _, config = await config_record(db)
    makers = list((await db.execute(select(Manufacturer.name, Manufacturer.url))).all())
    await db.rollback()
    words = set(product._product_key(search_query))
    known = identity.manufacturer
    if not known:
        matches = [name for name, _ in makers if set(product._product_key(name)) <= words]
        if len(matches) == 1:
            known = matches[0]
    if (not sku and len(query) >= 6 and product._sku_key(query) and not re.search(r"\s", query)
            and len(re.findall(r"\d", query)) >= 2
            and product._norm(query) != product._norm(known)
            and not product.normalize_reference(query)
            and not re.fullmatch(r"PA(?:6|11|12|66|612)(?:[-+]?(?:CF|GF)\d*)?", query, re.I)):
        sku = query
    site = next((url for name, url in makers if product._norm(name) == product._norm(known)), None)
    inferred = identity.model_copy(update={"manufacturer": known, "designation": identity.designation or search_query})
    if not inferred.material:
        materials = [material for material in product.MATERIAL_ACRONYMS if product._literal_in(material, query)]
        if len(materials) == 1:
            inferred = inferred.model_copy(update={"material": materials[0]})
    issues, pages, errors = [], {}, []
    identities = {}
    catalog_cards = []
    search_ok = False
    dropped_links = False
    max_bytes = config['supplier_download_mib'] * 1024 * 1024

    host_locks = {}

    async def check(urls):
        nonlocal dropped_links
        pending = list(dict.fromkeys(link for url in urls if (link := product.public_link(url)) and link not in pages))
        pending.sort(key=lambda url: not product._sku_in_url(sku, url))
        urls = pending[:max(0, min(3, 8 - len(pages)))]
        dropped_links |= len(pending) > len(urls)
        async def read(url):
            pages[url] = None
            try:
                lock = host_locks.setdefault(product._publisher(url), asyncio.Lock())
                async with lock, asyncio.timeout(8):
                    pages[url] = await product.fetch_supplier(url, max_bytes=max_bytes)
            except (ValueError, OSError, TimeoutError) as error:
                errors.append(str(error)[:200] if isinstance(error, ValueError) else 'Source request failed')
            except product.httpx.HTTPError:
                errors.append('Source request failed')
        await asyncio.gather(*(read(url) for url in urls))
        # Resolve authority serially: the request owns one database session.
        for url in urls:
            page = pages[url]
            if page and not page.selected_variant_unresolved:
                try:
                    identities[url] = await product.verified_product_identity(db, page)
                except ValueError:
                    errors.append('Product identity could not be read')

    def candidates():
        found = []
        rows = [(url, page, identities.get(url), None) for url, page in pages.items()]
        rows.extend((item['product_url'], None, item['identity'], item) for item in catalog_cards)
        for url, page, candidate, catalog in rows:
            if (page and (page.datasheet or page.manual or page.selected_variant_unresolved)) or candidate is None:
                continue
            if not candidate.designation or not candidate.manufacturer:
                continue
            if any(known and found and product._norm(known) != product._norm(found) for known, found in (
                (identity.manufacturer, candidate.manufacturer), (identity.material, candidate.material),
                (identity.color, candidate.color),
            )):
                continue
            if any(known is not None and found is not None and known != found for known, found in (
                (identity.diameter_mm, candidate.diameter_mm), (identity.weight_g, candidate.weight_g),
            )):
                continue
            kind = 'search'
            page_code = (page.product or {}).get('sku') if page else ''
            if sku and candidate.sku:
                wanted, actual = product._sku_key(sku), product._sku_key(candidate.sku)
                if product._norm(sku) == product._norm(candidate.sku):
                    kind = 'sku_exact'
                elif wanted and wanted == actual:
                    kind = 'sku_format'
                elif (len(wanted) >= 6 and (actual.startswith(wanted) or (abs(len(wanted) - len(actual)) <= 1
                      and SequenceMatcher(None, wanted, actual).ratio() >= .85))):
                    kind = 'similar_sku'
                elif clue_sku and wanted and wanted == product._sku_key(page_code or ''):
                    kind = 'search'
                else:
                    continue
            elif sku and page_code and product._sku_key(sku) != product._sku_key(page_code):
                continue
            url = product.public_link(page.url if page else url)
            if not url:
                continue
            found.append({'code': 'possible_match', 'name': candidate.designation, 'source': catalog['source'] if catalog else product._publisher(url),
                          'url': catalog['url'] if catalog else url, 'product_url': url, 'match_kind': kind, 'identity': candidate.model_dump(),
                          'source_product_code': page_code if page_code and product._norm(page_code) != product._norm(candidate.sku) else None})
        order = {'sku_exact': 0, 'sku_format': 1, 'similar_sku': 2, 'search': 3}
        unique = {}
        for item in sorted(found, key=lambda item: order[item['match_kind']]):
            unique.setdefault(item['product_url'], item)
        return list(unique.values())

    try:
        async with asyncio.timeout(25):
            code_terms = [sku] if sku else [
                term.strip('.,;()[]') for term in query.split()
                if re.search(r'\d', term) and product._sku_key(term.strip('.,;()[]'))
            ][:4]
            bundled = [row for term in code_terms for row in lookup_catalog_code(term, known, context=query, limit=24)]
            if not bundled and not sku:
                bundled = lookup_named_catalog(query, limit=24)
            for row in bundled:
                link = product.public_link(row.get('product_url'))
                if not link:
                    continue
                candidate = product.Identity(
                    manufacturer=row['maker'], designation=row['product'], material=row['material'],
                    color=row.get('color') or row.get('variant') or '', sku=row.get('manufacturer_sku') or '',
                    diameter_mm=row.get('diameter_mm'), weight_g=row.get('net_weight_g'),
                )
                catalog_cards.append({'identity': candidate, 'product_url': link,
                                      'url': row['url'], 'source': row['source'] + ' (bundled)'})
            if site:
                domain = product._site_host(site)
                if domain == 'paramount-3d.com':
                    try:
                        urls = await product._paramount_candidates(inferred, sku, strict=True)
                    except ValueError as error:
                        urls = []
                        issues.append({'code': 'unavailable_source', 'source': known, 'reason': str(error)})
                else:
                    urls = await product._manufacturer_sitemap_candidates(site, domain, inferred, sku)
                    if not urls:
                        issues.append({'code': 'unavailable_source', 'source': known,
                                       'reason': 'Manufacturer index could not confirm product links'})
                await check(urls)
            if guidance.strip() or not any(item['match_kind'] in {'sku_exact', 'sku_format'} for item in candidates()):
                try:
                    async with asyncio.timeout(4):
                        live, source_url, complete = await product.catalog_candidates(inferred.model_copy(update={'sku': sku}))
                    for row in live[:24]:
                        if not isinstance(row, dict) or not (link := product.public_link(row.get('shop_url'))):
                            continue
                        try:
                            candidate = product.catalog_identity(row)
                        except (ValueError, AttributeError, TypeError):
                            continue
                        catalog_cards.append({'identity': candidate, 'product_url': link,
                                              'url': product.public_link(source_url) or link, 'source': 'FilaManDB'})
                    if not complete:
                        issues.append({'code': 'unavailable_source', 'source': 'FilaManDB', 'reason': 'Catalog search incomplete'})
                except (product.httpx.HTTPError, product.HTTPException, ValueError, TimeoutError):
                    issues.append({'code': 'unavailable_source', 'source': 'FilaManDB'})
            if guidance.strip() or not any(item['match_kind'] in {'sku_exact', 'sku_format'} for item in candidates()):
                if config['search_url']:
                    for term in _queries(search_query, sku):
                        if len(pages) >= 8:
                            break
                        try:
                            urls = await product.search_web(config['search_url'], term)
                            search_ok = True
                            await check(urls)
                        except (product.httpx.HTTPError, ValueError, TimeoutError) as error:
                            issues.append({'code': 'unavailable_source', 'source': 'Web search',
                                           'reason': product.search_connection_problem(error)})
                            break
                        if any(not sku or item['match_kind'] in {'sku_exact', 'sku_format'} for item in candidates()):
                            break
    except TimeoutError:
        issues.append({'code': 'unavailable_source', 'source': 'Product discovery', 'reason': 'Lookup timed out'})
    found = candidates()
    if errors and not issues:
        issues.append({'code': 'unavailable_source', 'source': 'Product pages', 'reason': errors[0]})
    if not found:
        issues.append({'code': 'search_incomplete' if issues or errors or dropped_links or not search_ok else 'no_match'})
    return {'suggestions': [], 'discovery_sku': sku, 'issues': [*found[:4], *issues]}
