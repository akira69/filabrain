"""FilamentDB lookup with completeness information for evidence matching."""

import asyncio

import httpx
from app.api.v1 import filamentdb_proxy as upstream
from fastapi import HTTPException


async def lookup_filaments(
    search: str | None,
    manufacturer_name: str | None = None,
    material_key: str | None = None,
    *,
    report_incomplete: bool = False,
) -> dict:
    if native := getattr(upstream, "lookup_filaments", None):
        return await native(
            search,
            manufacturer_name=manufacturer_name,
            material_key=material_key,
            report_incomplete=report_incomplete,
        )

    params = {"page": 1, "page_size": 20}
    if manufacturer_name:
        makers = await upstream._proxy_get(
            "/manufacturers", {"search": manufacturer_name, "page_size": 5}
        )
        items = upstream._response_items(makers)
        if not items:
            return {**params, "items": [], "total": 0}
        maker = next(
            (
                item
                for item in items
                if item.get("name", "").casefold() == manufacturer_name.casefold()
            ),
            items[0],
        )
        params["manufacturer_id"] = maker["id"]
    if material_key:
        params["material_key"] = material_key

    direct = await upstream._proxy_get(
        "/filaments", {**params, "search": search} if search else params
    )
    if not search or upstream._response_items(direct):
        return direct

    terms = upstream.search_probe_terms(search)
    if not terms:
        return direct

    async def probe(client, term):
        try:
            return await upstream._proxy_get(
                "/filaments",
                {
                    **params,
                    "search": term,
                    "page_size": upstream._FUZZY_PROBE_PAGE_SIZE,
                },
                client=client,
            )
        except HTTPException:
            return None

    try:
        async with httpx.AsyncClient(timeout=upstream._TIMEOUT) as client:
            pages = await asyncio.wait_for(
                asyncio.gather(*(probe(client, term) for term in terms)),
                timeout=upstream._FUZZY_FALLBACK_TIMEOUT,
            )
    except TimeoutError:
        return direct

    keys = upstream._FILAMENT_LOOKUP_TEXT_KEYS
    candidates = {}
    for page in pages:
        for index, item in enumerate(upstream._response_items(page)):
            candidates.setdefault(upstream._item_key(item, index, keys), item)
    scored = []
    for item in candidates.values():
        score = upstream.fuzzy_token_score(search, upstream._lookup_text(item, keys))
        if score >= upstream.FUZZY_MATCH_THRESHOLD:
            label = str(item.get("designation") or item.get("name") or "").lower()
            scored.append((score, label, item))
    if not scored:
        return direct
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    result = {
        **(direct if isinstance(direct, dict) else {}),
        "items": [entry[2] for entry in scored[: params["page_size"]]],
        "total": len(scored),
        "page": params["page"],
        "page_size": params["page_size"],
    }
    if report_incomplete and any(
        not isinstance(page, dict)
        or not isinstance(page.get("total"), int)
        or page["total"] > len(upstream._response_items(page))
        for page in pages
    ):
        result["incomplete"] = True
    return result
