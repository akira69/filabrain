"""FilamentDB lookup with completeness information for evidence matching."""

import asyncio
from urllib.parse import urlencode

import httpx
from app.api.v1 import filamentdb_proxy as upstream
from app.services.filamentdb_field_mapping import normalize_spool_image_file
from fastapi import HTTPException

from .knowledge import _words
from .model import LIMITS

FILAMANDB_SPOOL_PATH = "/spool-profiles"
FILAMANDB_API_URL = "https://db.filaman.app/api/v1"


async def manufacturer_website(name: str) -> str | None:
    try:
        async with asyncio.timeout(4):
            result = await upstream._proxy_get("/manufacturers", {"search": name, "page_size": 5})
    except (TimeoutError, HTTPException, httpx.HTTPError, ValueError):
        return None
    matches = [
        item for item in upstream._response_items(result)
        if str(item.get("name", "")).strip().casefold() == name.strip().casefold()
        and item.get("is_active") is not False
    ]
    website = matches[0].get("website") if len(matches) == 1 else None
    return website if isinstance(website, str) else None


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


async def linked_spool_profile(profile_id: int) -> dict:
    profile = await upstream._proxy_get(f"{FILAMANDB_SPOOL_PATH}/{profile_id}")
    if (
        isinstance(profile, dict) and type(profile.get("id")) is int
        and profile["id"] == profile_id and profile.get("is_active") is not False
    ):
        return profile
    return {}


async def linked_spool_card(profile_id: int | None) -> dict | None:
    if type(profile_id) is not int or profile_id <= 0:
        return None
    path = f"{FILAMANDB_SPOOL_PATH}/{profile_id}"
    if upstream._api_url(path) != FILAMANDB_API_URL + path:
        return None
    try:
        async with asyncio.timeout(4):
            profile = await linked_spool_profile(profile_id)
    except (TimeoutError, HTTPException, httpx.HTTPError, ValueError):
        return None
    return _spool_card(profile)


async def search_spool_cards(
    search: str, manufacturer: str, product: str, material: str, sizes: set[float],
) -> list[tuple[dict, str, str]]:
    path = FILAMANDB_SPOOL_PATH
    if not search or upstream._api_url(path) != FILAMANDB_API_URL + path:
        return []

    async def items(lookup):
        try:
            async with asyncio.timeout(4):
                return upstream._response_items(await lookup)
        except (TimeoutError, HTTPException, httpx.HTTPError, AttributeError, KeyError, TypeError, ValueError):
            return []

    direct = items(upstream.search_spool_profiles(None, search=search, page=1, page_size=50))
    filaments, profiles = await asyncio.gather(
        items(lookup_filaments(product, manufacturer_name=manufacturer)), direct,
    ) if manufacturer and product else ([], await direct)

    cards = {}
    low, high = LIMITS["raw_material_weight_g"]

    def add(profile, label="", net_weight_g=None):
        if not (card := _spool_card(profile)) or not any(
            key in card for key in ("photo", "empty_weight_g", "width_mm", "outer_diameter_mm")
        ):
            return
        style = profile.get("spool_material")
        if type(net_weight_g) in (int, float) and low <= net_weight_g <= high and net_weight_g in sizes:
            card["net_weight_g"] = net_weight_g
        existing = cards.get(card["id"])
        if not existing or ("net_weight_g" in card and "net_weight_g" not in existing[0]):
            cards[card["id"]] = (card, label, style[:200] if isinstance(style, str) else "")

    product_words = set(_words(product).split())
    material_words = set(_words(material).split())
    for item in filaments:
        maker = item.get("manufacturer")
        name = maker.get("name") if isinstance(maker, dict) else item.get("manufacturer_name")
        if not isinstance(name, str) or _words(name).replace(" ", "") != _words(manufacturer).replace(" ", ""):
            continue
        designation = item.get("designation")
        material_scope = item.get("material")
        family = material_scope.get("name") if isinstance(material_scope, dict) else item.get("material_name")
        label = " ".join(value for value in (designation, family) if isinstance(value, str))
        if not product_words <= set(_words(label).split()) or not material_words <= set(_words(family).split()):
            continue
        profile = item.get("spool_profile")
        profile_id = item.get("spool_profile_id")
        if not isinstance(profile, dict) or type(profile_id) is not int or profile.get("id") != profile_id:
            continue
        weight = item.get("nominal_weight_g")
        if sizes and type(weight) in (int, float) and weight > 0 and weight not in sizes:
            continue
        add(profile, f"{manufacturer} {label}", weight)

    for profile in profiles:
        add(profile)
    return list(cards.values())


def _spool_card(profile: dict) -> dict | None:
    profile_id = profile.get("id")
    if type(profile_id) is not int or profile_id <= 0 or profile.get("is_active") is False:
        return None
    name = profile.get("name")
    if not isinstance(name, str) or not (title := " ".join(name.split())[:200]):
        return None

    card = {
        "id": f"filamandb-{profile_id}", "title": title, "source": "FilaManDB",
        "url": f"{FILAMANDB_API_URL}{FILAMANDB_SPOOL_PATH}/{profile_id}",
    }
    for key, field in (
        ("empty_weight_g", "default_spool_weight_g"),
        ("outer_diameter_mm", "spool_outer_diameter_mm"),
        ("width_mm", "spool_width_mm"),
        ("net_weight_g", "raw_material_weight_g"),
    ):
        low, high = LIMITS[field]
        value = profile.get(key)
        if type(value) in (int, float) and low <= value <= high:
            card[key] = value

    try:
        image_file = normalize_spool_image_file(profile.get("image_file"))
    except ValueError:
        image_file = None
    if image_file and image_file.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        card["photo"] = "/api/v1/filamentdb/spool-profile-image?" + urlencode({"image_file": image_file})
    return card
