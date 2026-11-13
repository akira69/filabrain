import asyncio
import http.client
import json
import re
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Annotated
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

import httpx
from app.api.deps import DBSession
from app.api.v1.schemas_filament import FilamentUpdate
from app.core.config import settings
from app.models import Color, Filament, Manufacturer
from app.services.extra_field_validation import normalize_numeric_range
from app.services.filamentdb_field_mapping import standard_filamentdb_fields
from app.utils.colors import normalize_hex_color
from fastapi import HTTPException
from pydantic import BaseModel, Field, FiniteFloat, ValidationError
from sqlalchemy import func, or_, select

from .catalog import linked_spool_profile, lookup_filaments, manufacturer_website
from .model import (
    LIMITS,
    SPEC_LABELS,
    extract_proposal,
    request_extraction,
)
from .settings import config_record, model_key, resolve_model
from .sources import (
    SHARED_SPEC,
    SUPPLIER_TOO_LARGE,
    Evidence,
    SearchEnginesUnavailable,
    fetch_archived_supplier,
    fetch_supplier,
    ofd_candidates,
    paramount_product_urls,
    public_link,
    search_web,
    supplier_page_url,
    supplier_sitemap_urls,
)

Text = Annotated[str, Field(max_length=255)]


class Identity(BaseModel):
    manufacturer: Text = ""
    designation: Text = ""
    material: Text = ""
    color: Text = ""
    diameter_mm: FiniteFloat | None = None
    weight_g: FiniteFloat | None = None
    sku: Text = ""
    material_subgroup: Text | None = None
    finish_type: Text | None = None
    color_mode: Text | None = None


IDENTITY_FIELDS = (
    "manufacturer_id", "manufacturer_name", "designation", "material_type",
    "material_subgroup", "manufacturer_color_name", "color_mode", "finish_type",
    "diameter_mm", "raw_material_weight_g", "manufacturer_sku", "filamentdb_spool_profile_id",
)
MODE_FIELDS = {
    "fill": (
        "manufacturer_sku", "manufacturer_color_name", "density_g_cm3",
        "raw_material_weight_g", "extruder_temp_range_c", "bed_temp_range_c",
        "drying_temp_c", "drying_time_hours", "datasheet_url",
        "default_spool_weight_g", "spool_outer_diameter_mm", "spool_width_mm", "spool_material",
        "material_subgroup", "finish_type", "shop_url", "softening_temp_c", "cooling_fan_range_percent",
        "chamber_temp_c", "max_volumetric_speed_mm3_s", "flow_ratio", "pressure_advance_k",
        "image_url", "ams_compatibility", "build_plate_compatibility",
    ),
    "standardize": ("designation", "material_type", "material_subgroup", "manufacturer_color_name"),
}


def filament_snapshot(filament: Filament) -> dict:
    fields = set(IDENTITY_FIELDS + MODE_FIELDS["fill"]) - {"manufacturer_name", "filamentdb_spool_profile_id"}
    return {
        "id": filament.id,
        "manufacturer_name": filament.manufacturer.name,
        "manufacturer_url": filament.manufacturer.url,
        "filamentdb_spool_profile_id": getattr(filament, "filamentdb_spool_profile_id", None),
        **{field: getattr(filament, field) for field in fields},
    }


def is_missing(field: str, value) -> bool:
    return (
        value is None
        or (isinstance(value, str) and not value.strip())
        or (isinstance(value, list) and not value)
        or (
            field in {"extruder_temp_range_c", "bed_temp_range_c", "cooling_fan_range_percent"}
            and isinstance(value, dict)
            and value.get("min") is None
            and value.get("max") is None
        )
    )


def snapshot_identity(snapshot: dict) -> Identity:
    return Identity(
        manufacturer=snapshot["manufacturer_name"],
        designation=snapshot["designation"],
        material=snapshot["material_type"],
        color=snapshot["manufacturer_color_name"] or "",
        diameter_mm=snapshot["diameter_mm"],
        weight_g=snapshot["raw_material_weight_g"],
        sku=snapshot["manufacturer_sku"] or "",
        material_subgroup=(snapshot.get("material_subgroup") or "").strip() or None,
        finish_type=(snapshot.get("finish_type") or "").strip() or None,
        color_mode=snapshot.get("color_mode"),
    )


def _norm(value) -> str:
    return " ".join(value.casefold().split()) if isinstance(value, str) else ""


MATERIAL_SPLITS = {"matte pla": ("PLA", "Matte"), "pla matte": ("PLA", "Matte")}
FINISHES = {"matte", "silk", "glossy", "satin", "translucent", "transparent"}
MATERIAL_ACRONYMS = {"PLA", "PETG", "PET", "ABS", "ASA", "TPU", "TPE", "PC", "PVA", "PA", "HIPS"}
FORM_WORDS = re.compile(r"[^\s-]+")


def _material_family(value):
    key = _norm(value)
    return MATERIAL_SPLITS[key][0].casefold() if key in MATERIAL_SPLITS else key


def _tokens(value):
    return tuple(_norm(value).replace("-", " ").split())


def _line(name, color):
    words, suffix = _tokens(name), _tokens(color)
    if suffix and words[-len(suffix):] == suffix:
        return words[:-len(suffix)]
    return words


def _same_line(identity, candidate):
    line = _line(identity.designation, identity.color)
    return _tokens(identity.designation) == _tokens(candidate.designation) or (
        bool(line) and line == _line(candidate.designation, candidate.color)
    )


def _format_rule(value):
    if not value or not isinstance(value, str):
        return None
    words = FORM_WORDS.findall(value.strip())
    joins = re.findall(r"[\s-]+", value.strip())
    if not words or len(set(joins)) > 1:
        return None
    styles = [word for word in words if word not in MATERIAL_ACRONYMS and word.lower() != word.upper()]
    for style in ("upper", "title", "lower"):
        if styles and all(getattr(word, style)() == word for word in styles):
            return style, joins[0] if joins else None
    return None


def _format_label(value, rule):
    style, join = rule
    words = FORM_WORDS.findall(value)
    if join is None and len(words) > 1:
        return None
    return (join or " ").join(
        word.upper() if word.upper() in MATERIAL_ACRONYMS else getattr(word, style)()
        for word in words
    )


async def _local_conventions(db: DBSession, snapshot: dict) -> dict:
    fields = MODE_FIELDS["standardize"]
    family = _material_family(snapshot.get("material_type"))
    result = {"proposals": [], "issues": []}
    if not snapshot.get("manufacturer_id") or not family:
        return result
    rows = await db.execute(
        select(Filament.id, Filament.finish_type, *(getattr(Filament, field) for field in fields))
        .where(Filament.manufacturer_id == snapshot["manufacturer_id"], Filament.id != (snapshot.get("id") or 0))
        .order_by(Filament.id)
    )
    line = _line(snapshot.get("designation"), snapshot.get("manufacturer_color_name"))
    cohort = [row._mapping for row in rows if _material_family(row.material_type) == family]
    examples = sorted(
        cohort, key=lambda row: not (line and _line(row["designation"], row["manufacturer_color_name"]) == line),
    )[:5]

    def propose(changes, reason, key, evidence):
        try:
            FilamentUpdate.model_validate(changes)
        except ValidationError:
            return
        result["proposals"].append({
            "id": "+".join(changes),
            "fields": {field: {"old": snapshot.get(field), "value": value} for field, value in changes.items()},
            "sources": [
                {"title": f"Local filament #{row['id']}", "quote": quote[:400]}
                for row, quote in evidence
            ],
            "reason": reason,
            "group_key": json.dumps([snapshot["manufacturer_id"], family, *changes, key], ensure_ascii=False),
        })

    for field in ("designation", "manufacturer_color_name"):
        old = snapshot.get(field)
        if not old or not old.strip():
            continue
        choices = {}
        for row in examples:
            label = row[field]
            if not label:
                continue
            rule = _format_rule(label)
            if _tokens(label) == _tokens(old):
                value = label
            elif rule and (
                field != "designation"
                or (line and _line(row["designation"], row["manufacturer_color_name"]) == line)
                or len(set(re.findall(r"[\s-]+", label.strip()))) <= 1
                and rule[1] in (None, " ")
            ):
                value = _format_label(old, rule)
            else:
                continue
            if value and _tokens(value) == _tokens(old):
                choices.setdefault(value, []).append((row, label, rule))
        if len(choices) == 1:
            value, evidence = next(iter(choices.items()))
            if value != old and len(evidence) >= 3:
                rules = {rule for _, _, rule in evidence}
                key = next(iter(rules)) if len(rules) == 1 and None not in rules else [old, value]
                propose(
                    {field: value}, "Formats the same identity tokens using the local naming convention.",
                    key, [(row, quote) for row, quote, _ in evidence],
                )
        elif choices:
            result["issues"].append({"code": "conflict", "field": field})

    material = snapshot["material_type"]
    subgroup = snapshot.get("material_subgroup")
    split = MATERIAL_SPLITS.get(_norm(material))
    if split:
        core, modifier = split
        if (
            _norm(subgroup) not in ("", _norm(modifier))
            or _norm(snapshot.get("finish_type")) not in ("", _norm(modifier))
        ):
            result["issues"].append({"code": "conflict", "field": "material_type"})
            return result
        choices = {}
        for row in examples:
            pair = (row["material_type"], row["material_subgroup"])
            if (
                _norm(pair[0]) == _norm(core) and _norm(pair[1]) == _norm(modifier)
                or MATERIAL_SPLITS.get(_norm(pair[0])) == split
            ) and _norm(row["finish_type"]) in ("", _norm(modifier)):
                choices.setdefault(pair, []).append((row, " / ".join(filter(None, pair))))
        if len(choices) == 1:
            (new_material, new_subgroup), evidence = next(iter(choices.items()))
            if _norm(new_material) == _norm(core) and len(evidence) >= 3:
                changes = {"material_type": new_material}
                if subgroup != new_subgroup:
                    changes["material_subgroup"] = new_subgroup
                propose(changes, "Moves the existing material modifier to the subgroup.", [material, subgroup, new_material, new_subgroup], evidence)
        elif choices:
            result["issues"].append({"code": "conflict", "field": "material_type"})
    else:
        choices = {}
        for row in examples:
            value = row["material_type"]
            if _norm(value) == _norm(material):
                choices.setdefault(value, []).append((row, value))
        if len(choices) == 1:
            value, evidence = next(iter(choices.items()))
            if value != material and len(evidence) >= 3:
                propose({"material_type": value}, "Uses the local spelling of the same material.", [material, value], evidence)
        elif choices:
            result["issues"].append({"code": "conflict", "field": "material_type"})
    return result


def _positive(value):
    return value if type(value) in (int, float) and 0 < value < float("inf") else None


def _site_host(url) -> str:
    link = public_link(url)
    return (urlsplit(link).hostname or "").rstrip(".").removeprefix("www.") if link else ""


def manufacturer_site_url(value) -> str | None:
    if not (link := public_link(value)):
        return None
    parsed = urlsplit(link)
    try:
        if parsed.port not in (None, 443 if parsed.scheme == "https" else 80):
            return None
    except ValueError:
        return None
    if not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]+", _site_host(link)):
        return None
    return urlunsplit(parsed._replace(query="", fragment=""))

def _publisher(url):
    host = _site_host(url)
    labels = host.split(".")
    # shortcut: covers common country suffixes; use a public suffix list if publisher coverage expands.
    width = 3 if len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in {"co", "com", "org", "net"} else 2
    return ".".join(labels[-width:])


def _corroborated_option(options):
    votes = [
        {source.get("origin") or _publisher(source.get("url")) for source in option["sources"]} - {""}
        for option in options
    ]
    for index, sites in enumerate(votes):
        others = set().union(*(other for i, other in enumerate(votes) if i != index))
        if len(sites - others) >= 2 and len(sites - others) > len(others - sites):
            return options[index]
    return None


def _on_manufacturer_site(url, domain: str) -> bool:
    host = _site_host(url)
    return bool(
        domain and (host == domain or host.endswith("." + domain))
        and not host.startswith(("forum.", "community."))
    )


def filament_identity(filament: Filament) -> Identity:
    return snapshot_identity(filament_snapshot(filament))


def identity_matches(left: Identity, right: Identity) -> bool:
    return all(
        (
            _norm(value) == _norm(getattr(right, key))
            if isinstance(value, str)
            else value == getattr(right, key)
        )
        for key, value in left.model_dump().items()
        if key not in {"material_subgroup", "finish_type", "color_mode"} or key in left.model_fields_set
    )


def catalog_identity(item: dict) -> Identity:
    return Identity(
        manufacturer=(item.get("manufacturer") or {}).get("name") or "",
        designation=item.get("name") or item.get("designation") or "",
        material=(item.get("material") or {}).get("key")
        or item.get("material_key")
        or "",
        color=item.get("manufacturer_color_name") or item.get("color_name") or "",
        diameter_mm=_positive(item.get("diameter_mm")),
        weight_g=_positive(item.get("nominal_weight_g")),
        sku=item.get("sku") or "",
    )


def _finish(designation, explicit, material=""):
    if explicit:
        if isinstance(explicit, dict):
            explicit = explicit.get("key") or explicit.get("name")
        if _norm(explicit):
            return _norm(explicit)
    if split := MATERIAL_SPLITS.get(_norm(material)):
        return _norm(split[1])
    found = _finish_markers(designation)
    return next(iter(found)) if len(found) == 1 else ""


def _finish_markers(text):
    return set(re.findall(r"\b[a-z]+\b", _norm(text))) & FINISHES


def _catalog_subgroup(item):
    return item.get("material_subtype") or item.get("material_subgroup") or item.get("subgroup")


def _catalog_finish(item, candidate):
    subgroup = _norm(_catalog_subgroup(item))
    return _finish(
        candidate.designation,
        item.get("finish_type") or item.get("finish") or (subgroup if subgroup in FINISHES else None),
        candidate.material,
    )


def _catalog_color_mode(item):
    colors = item.get("colors")
    return _norm(item.get("color_mode")) or (
        "multi" if isinstance(colors, list) and len(colors) > 1 else "single"
    )


def _catalog_variant(identity: Identity, item: dict) -> Identity:
    candidate = catalog_identity(item)
    name = _product_key(candidate.color)
    line = _product_key(candidate.designation)
    if (
        name and line and _product_key(identity.designation) in ((*line, *name), (*name, *line))
        and (not identity.color or _product_key(identity.color) == name or _swatch_code(identity.color))
    ):
        return candidate.model_copy(update={"designation": identity.designation, "color": identity.color or candidate.color})
    if _norm(identity.material) != "pla basic" or not re.fullmatch(
        r"#?[a-fA-F0-9]{6}", identity.color
    ):
        return candidate
    subtype = _norm(_catalog_subgroup(item))
    swatches = item.get("colors") or []
    if (
        _norm(candidate.material) != "pla"
        or subtype not in ("", "basic")
        or (subtype != "basic" and _norm(candidate.designation) != "pla basic")
        or _catalog_color_mode(item) != "single"
        or (isinstance(swatches, list) and len(swatches) > 1)
    ):
        return candidate

    # Imported inventories can use a hex code as the color and the swatch
    # name as the designation. Require both, plus the explicit product line.
    swatch = swatches[0] if isinstance(swatches, list) and swatches and isinstance(swatches[0], dict) else {}
    hex_values = [
        _norm(value).removeprefix("#")
        for value in (item.get("hex_color"), item.get("color_hex"), swatch.get("hex_code"))
        if value
    ]
    color_name = re.sub(r"\s+\(\d+\)$", "", candidate.color)
    designation = re.sub(r"\s+\(\d+\)$", "", candidate.designation)
    if (
        not hex_values
        or set(hex_values) != {_norm(identity.color).removeprefix("#")}
        or _tokens(color_name) != _tokens(identity.designation)
        or _tokens(designation) not in {_tokens(identity.designation), ("pla", "basic")}
    ):
        return candidate
    return candidate.model_copy(update={
        "designation": identity.designation, "material": identity.material, "color": identity.color,
    })


def _variant_context(identity: Identity, snapshot: dict | None) -> dict:
    if snapshot is not None:
        return snapshot
    return identity.model_dump(include={"finish_type", "material_subgroup", "color_mode"})


def same_variant(identity: Identity, candidate: Identity, snapshot=None, item=None) -> bool:
    sku_match = bool(
        _norm(identity.sku) and _norm(identity.sku) == _norm(candidate.sku)
    )
    if identity.designation and candidate.designation and not _same_line(identity, candidate):
        return False
    for field in (
        "manufacturer",
        "material",
        "color",
        "diameter_mm",
        "weight_g",
        "sku",
    ):
        known = getattr(identity, field)
        found = getattr(candidate, field)
        if known is None or isinstance(known, str) and not _norm(known):
            continue
        if (found is None or found == "") and sku_match:
            continue
        compare = _material_family if field == "material" else _norm
        if compare(known) != compare(found) if isinstance(known, str) else known != found:
            return False
    if item is not None:
        context = _variant_context(identity, snapshot)
        for known, found in (
            (_finish(identity.designation, context.get("finish_type"), identity.material),
             _catalog_finish(item, candidate)),
            (context.get("color_mode"), _catalog_color_mode(item)),
            (context.get("material_subgroup"), _catalog_subgroup(item)),
        ):
            if (
                known and (found or not sku_match) and _norm(known) != _norm(found)
                and (found or _norm(known) not in {"single", "solid", _norm(identity.material)})
            ):
                return False
    return sku_match or (
        bool(_norm(identity.designation))
        and _same_line(identity, candidate)
        and bool(
            candidate.manufacturer and candidate.material and candidate.diameter_mm
        )
    )


def _literal_in(term: str, text: str) -> bool:
    return bool(
        term
        and re.search(
            r"(?<![\w+.-])" + re.escape(_norm(term)) + r"(?![\w+.-])", _norm(text)
        )
    )


def _product_key(value):
    return tuple(re.findall(r"[^\W_]+(?:[.+][\w]+|\+)?", _norm(value)))


def _swatch_code(value):
    return bool(re.fullmatch(r"#?[a-f0-9]{6}", _norm(value)))


_PACKAGED_TITLE = re.compile(
    r"^(?P<name>.+?)\s+(?P<diameter>\d+(?:\.\d+)?)\s*mm\s+"
    r"(?P<weight>\d+(?:\.\d+)?)\s*(?P<unit>kg|g)(?:\s+filament)?"
    r"(?:\s*\[(?P<sku>[\w.+-]+)\])?(?:\s+(?P<suffix>[A-Z]{2,5}))?$", re.IGNORECASE,
)
_REEL_SIZE = re.compile(
    r"""\bspool\s+size\s*:\s*[\d.]+\s*(?:"|in)\s*ID\s*/\s*([\d.]+)\s*(?:"|in)\s*OD\s*/\s*([\d.]+)\s*(?:"|in)\s*(?:depth|width)\b""",
    re.IGNORECASE,
)
_LABELLED_SKU = re.compile(r"\b(?:sku|article number)\s*[:#]\s*([\w.+-]+)", re.IGNORECASE)
_COLOR_CODES = (
    ("RAL", re.compile(r"RAL(?: Colou?r)?\s*:?\s+(\d{4})(?:\s+(.{1,90}))?", re.IGNORECASE)),
    ("Pantone", re.compile(
        r"Pantone(?: Colou?r)?\s*:?\s+((?:[a-z][a-z ]{0,23}\s+)?\d{2,4}(?:-\d{4})?(?:\s*[a-z]{1,4})?)",
        re.IGNORECASE,
    )),
)
_PACKAGED_NAME = re.compile(r"([A-Z][A-Z0-9+ -]{0,19}) \(([^()]+)\)", re.IGNORECASE)
_SHORE = re.compile(r"Shore Hardness:\s*(\d{2,3}[ad])", re.IGNORECASE)
_PARAMOUNT_TEMPERATURE = re.compile(
    r"(\d{1,3})(?:\s*°?\s*C)?\s*[-–]\s*(\d{1,3})\s*°?\s*C(?:\s*\([^()]*\))?",
    re.IGNORECASE,
)
_PARAMOUNT_TEMP_LABELS = (
    ("bed_temp_range_c", re.compile(r"Bed Temp(?:erature)?\s*:?\s+(.+)", re.IGNORECASE)),
    ("extruder_temp_range_c", re.compile(r"(?:Nozzle|Print) Temp(?:erature)?\s*:?\s+(.+)", re.IGNORECASE)),
)


def _published_colors(text: str) -> list[tuple[str, str]]:
    return list(dict.fromkeys(
        (" ".join((system, *(part for part in match.groups() if part))), line)
        for system, pattern in _COLOR_CODES
        for line in text.splitlines()
        if (match := pattern.fullmatch(line))
    ))


def _paramount_specs(text: str):
    for line in text.splitlines():
        if shore := _SHORE.fullmatch(line):
            yield "material_subgroup", shore[1].upper(), line
        if line.casefold() == "compatible with bambu ams":
            yield "ams_compatibility", ["ams"], line
        if combined := re.fullmatch(r"Bed / Print Temperature:\s*(.+?)\s*/\s*(.+)", line, re.IGNORECASE):
            temperatures = zip(("bed_temp_range_c", "extruder_temp_range_c"), combined.groups())
        else:
            temperatures = (
                (field, match[1]) for field, pattern in _PARAMOUNT_TEMP_LABELS
                if (match := pattern.fullmatch(line))
            )
        for field, value in temperatures:
            if match := _PARAMOUNT_TEMPERATURE.fullmatch(value.replace("℃", "°C")):
                low, high = map(int, match.groups())
                minimum, maximum = LIMITS[field]
                if minimum <= low <= high <= maximum:
                    yield field, {"min": low, "max": high}, line



def _filament_diameters(text):
    return {
        float(raw.replace(",", "."))
        for raw in re.findall(r"(?<![\d.,])(1[.,]\d+|2[.,]\d+|3(?:[.,]\d+)?)\s*mm\b", text, re.IGNORECASE)
    }


def _paramount_variant_matches(identity: Identity, page: Evidence, name: str) -> bool:
    if not page.manufacturer_page or _site_host(page.url) != "paramount-3d.com":
        return False
    match = _PACKAGED_NAME.fullmatch(name)
    if not match or _norm(match[1]) != _material_family(identity.material):
        return False
    if identity.color_mode == "multi" or _norm(identity.finish_type) not in ("", "solid"):
        return False

    color = _product_key(match[2])
    ral = next((m for line in page.scopes or () if (
        m := _COLOR_CODES[0][1].fullmatch(line)
    )), None)
    hardness = next((m[1].casefold() for line in page.scopes or () if (
        m := _SHORE.fullmatch(line)
    )), "")
    words = set(_product_key(identity.designation))
    stored_color = _product_key(identity.color)
    matching_ral = bool(ral and (ral[1] in stored_color or ral[1] in words))
    achromatic_hex = {("black",): ("000000",), ("white",): ("ffffff",)}.get(color)
    if stored_color and stored_color not in (color, achromatic_hex) and not matching_ral:
        return False
    if not set(color) <= words and not matching_ral:
        return False
    allowed = {*color, _norm(match[1]), "basic"}
    if ral:
        allowed.update(("ral", ral[1], *_product_key(ral[2])))
    if hardness:
        allowed.add(hardness)
    return words <= allowed and _norm(identity.material_subgroup) in ("", "basic", hardness)


async def _paramount_candidates(identity: Identity, sku: str) -> list[str]:
    material = _material_family(identity.material)
    if not material:
        return []
    keywords = set(_product_key(identity.designation) + _product_key(identity.color)) - {material, "basic", "ral"}
    candidates = []
    for url in await paramount_product_urls():
        slug = urlsplit(url).path.rsplit("/", 1)[-1].casefold().removeprefix("paramount-3d-")
        terms = set(_product_key(slug.removeprefix(material)))
        overlap = len(terms & keywords)
        sku_match = bool(sku and _norm(sku) in terms)
        exact_name = len(terms) >= 2 and terms <= keywords
        if sku_match or exact_name or (slug.startswith(material) and overlap):
            candidates.append(((sku_match, exact_name, overlap, -len(terms - keywords)), url))
    return [url for _, url in sorted(candidates, reverse=True)[:3]]


def _packaged_product(identity: Identity, page: Evidence, snapshot):
    product = page.product or {}
    title = product.get("designation") or re.split(r"\s*[|–]\s*", page.title, maxsplit=1)[0]
    match = _PACKAGED_TITLE.fullmatch(title.strip())
    if not match or not (
        page.manufacturer_page
        or identity.manufacturer and _product_key(product.get("manufacturer")) == _product_key(identity.manufacturer)
    ):
        return None
    name, diameter, weight, unit, sku, suffix = match.groups()
    name_key = _product_key(name)
    paramount_alias = _paramount_variant_matches(identity, page, name)
    nominal_weight = float(weight) * (1000 if unit.lower() == "kg" else 1)
    # A saved weight can reflect a measurement. Allow a small variance only on the linked, verified package.
    linked_weight = paramount_alias and (snapshot or {}).get("shop_url") == page.url
    part = _norm(sku)
    context = _variant_context(identity, snapshot)
    finish = _finish(identity.designation, context.get("finish_type"), identity.material)
    found_finish = _finish_markers(name)
    search_hint = _norm((snapshot or {}).get("shop_url"))
    article_match = bool(part and (part in {_norm(identity.sku), search_hint} or _literal_in(part, identity.designation)))
    if (
        (name_key != _product_key(identity.designation) and not (article_match or paramount_alias))
        or (product.get("manufacturer") and _product_key(product["manufacturer"]) != _product_key(identity.manufacturer))
        or (product.get("color") and identity.color and not _swatch_code(identity.color)
            and _product_key(product["color"]) != _product_key(identity.color))
        or (product.get("finish_type") and finish and _norm(product["finish_type"]) != finish)
        or (suffix and _norm(suffix) != _norm(identity.material))
        or not _literal_in(_material_family(identity.material), name)
        or (identity.sku and _norm(identity.sku) not in {part, _norm(product.get("sku"))})
        or (identity.diameter_mm is not None and float(diameter) != identity.diameter_mm)
        or (identity.weight_g is not None and nominal_weight != identity.weight_g
            and not (linked_weight and abs(nominal_weight - identity.weight_g) <= nominal_weight * 0.02))
        or (found_finish and finish and found_finish != {finish})
        or (finish in FINISHES and finish not in found_finish and not article_match)
        or (identity.color and not (paramount_alias or _swatch_code(identity.color))
            and not set(_product_key(identity.color)).issubset(name_key))
    ):
        return None
    if paramount_alias and not sku and product.get("sku") and any(
        _LABELLED_SKU.findall(line) == [product["sku"]] for line in page.scopes or ()
    ):
        sku = product["sku"]
    return title, sku, float(diameter), nominal_weight


def _paramount_url_identity(page: Evidence) -> Identity | None:
    if _site_host(page.url) != "paramount-3d.com" or not urlsplit(page.url).path.startswith("/product-page/"):
        return None
    product = page.product or {}
    heading = product.get("designation") or page.title.split("|", 1)[0].strip()
    package = _PACKAGED_TITLE.fullmatch(heading)
    if not package or not (name := _PACKAGED_NAME.fullmatch(package["name"])):
        return None
    material, color = name.groups()
    if material.upper() not in MATERIAL_ACRONYMS or _norm(color) == "master spool":
        return None
    candidate = Identity(
        manufacturer="Paramount 3D", designation=package["name"], material=material.upper(),
        color=color, diameter_mm=float(package["diameter"]),
    )
    if verified := _packaged_product(candidate, replace(page, manufacturer_page=True), None):
        return candidate.model_copy(update={"sku": verified[1] or "", "weight_g": verified[3]})
    return None


def _supplier_scopes(identity: Identity, page: Evidence, snapshot=None) -> list[Evidence]:
    product = page.product or {}
    title = product.get("designation") or re.split(r"\s*[|–]\s*", page.title, maxsplit=1)[0]
    if package := _packaged_product(identity, page, snapshot):
        heading, sku, diameter, _ = package
        accepted_skus = {_norm(value) for value in (sku, identity.sku, product.get("sku")) if value}
        blocks = page.scopes if page.scopes is not None else (page.text,)
        has_heading = any(block == heading for block in blocks)
        in_product = not has_heading
        specs = []
        for scope in blocks:
            if scope == heading:
                in_product = True
                continue
            if not in_product:
                continue
            labels = {_norm(value) for value in _LABELLED_SKU.findall(scope)}
            if _PACKAGED_TITLE.fullmatch(scope) or labels - accepted_skus:
                break
            if not re.match(r"^spool\b", scope, re.IGNORECASE):
                diameters = _filament_diameters(scope)
                if diameters and diameters != {diameter}:
                    break
            if (
                re.match(r"^(?:material:|spool\b|density\b)", scope, re.IGNORECASE)
                or any(re.search(label, scope, re.IGNORECASE) for label in SPEC_LABELS.values())
                or any(pattern.fullmatch(scope) for _, pattern in _COLOR_CODES)
                or page.manufacturer_page and _site_host(page.url) == "paramount-3d.com"
                and any(_paramount_specs(scope))
            ):
                specs.append(scope)
        return [replace(page, text="\n".join([heading, *specs])[:3000])]
    product_line = page.manufacturer_page and _norm(identity.material) == "pla basic" and _norm(title) == "pla basic"
    brand = product.get("manufacturer") or (identity.manufacturer if page.manufacturer_page else None)
    sku = product.get("sku")
    context = _variant_context(identity, snapshot)
    finish = _finish(identity.designation, context.get("finish_type"), identity.material)
    if brand and identity.manufacturer and _norm(brand) != _norm(identity.manufacturer):
        return []
    if sku and _norm(identity.sku) and _norm(sku) != _norm(identity.sku):
        return []
    if identity.designation and title and _tokens(title) != _tokens(identity.designation) and not product_line:
        return []
    if any(
        known and found and _norm(known) != _norm(found)
        for known, found in (
            (identity.color, product.get("color")),
            ("pla" if product_line else _material_family(identity.material),
             "pla" if product_line and _norm(product.get("material")) == "pla basic"
             else _material_family(product.get("material"))),
            (finish, product.get("finish_type")),
        )
    ):
        return []

    matches = []
    shared = []
    for text in dict.fromkeys(page.scopes if page.scopes is not None else (page.text,)):
        universal = bool(SHARED_SPEC.search(text))
        row_sku = _literal_in(identity.sku, text)
        labelled_skus = _LABELLED_SKU.findall(text)
        if universal and labelled_skus:
            continue
        if _norm(identity.sku) and (
            any(_norm(found) != _norm(identity.sku) for found in labelled_skus)
            or not (row_sku or universal)
        ):
            continue
        if not row_sku and (
            not identity.designation
            or not identity.manufacturer
            or (not brand and not _literal_in(identity.manufacturer, text))
            or (_tokens(title) != _tokens(identity.designation) and not _literal_in(identity.designation, text))
        ):
            continue
        material = "pla" if product_line else _material_family(identity.material)
        explicit_materials = {
            value.casefold() for value in MATERIAL_ACRONYMS | {"PLA+", "PLA-CF", "PETG-CF", "ABS+", "PC-ABS"}
            if _literal_in(value, text)
        }
        if material and (
            explicit_materials - {material}
            or not (_literal_in(material, text) or row_sku or universal)
        ):
            continue
        if identity.color and not (
            _literal_in(identity.color, text) or universal
            or (product_line and row_sku and _literal_in(identity.designation, text)
                and re.fullmatch(r"#?[a-fA-F0-9]{6}", identity.color))
        ):
            continue
        finishes = _finish_markers(text)
        if finish and (
            finishes - {finish}
            or not (finish in finishes or row_sku or universal)
        ):
            continue
        subgroup = _norm(context.get("material_subgroup"))
        if subgroup and not (_literal_in(subgroup, text) or row_sku or universal):
            continue
        is_multi = bool(re.search(
            r"\b(?:multicolou?r|multi[ -]colou?r|gradient|striped)\b", text, re.IGNORECASE
        ))
        if context.get("color_mode") == ("single" if is_multi else "multi") and not universal:
            continue
        diameters = _filament_diameters(text)
        weights = {
            float(raw.replace(",", ".")) * (1000 if unit.lower() == "kg" else 1)
            for raw, unit in re.findall(r"(?<![\d.,/])(\d+(?:[.,]\d+)?)\s*(kg|g)\b(?!\s*/)", text, re.IGNORECASE)
        }
        if any(
            known is not None and values != {known} and (values or not (row_sku or universal))
            for known, values in ((identity.diameter_mm, diameters), (identity.weight_g, weights))
        ):
            continue
        (shared if universal else matches).append(text)

    if not matches and not (
        brand and identity.manufacturer and (_tokens(identity.designation) == _tokens(title) or product_line)
    ):
        return []
    # shortcut: unlabeled or multiply applicable sections are withheld until their variant can be verified.
    selected = matches if len(matches) == 1 or _norm(identity.sku) else []
    return (
        [replace(page, text=text) for text in selected[:8]]
        + [replace(page, text=text, shared=True) for text in shared[:8]]
    )


def supplier_matches(identity: Identity, page: Evidence) -> bool:
    return bool(_supplier_scopes(identity, page))


def _supplier_variants_disagree(evidence: list[Evidence], identity: Identity, snapshot) -> bool:
    unknown = {key: set() for key, value in _known_variant_fields(identity, snapshot).items() if value in (None, ())}
    for source in evidence:
        if source.shared:
            continue
        if source.datasheet:
            for key, values in _sheet_variant_values(source).items():
                if key in unknown:
                    unknown[key].update(values)
            continue
        product = source.product or {}
        if "sku" in unknown:
            unknown["sku"].update(
                _product_key(value) for value in _LABELLED_SKU.findall(source.text) or [product.get("sku")] if value
            )
        for key in ("color", "finish_type", "material_subgroup"):
            if key in unknown and product.get(key):
                unknown[key].add(_product_key(product[key]))
        if "diameter_mm" in unknown:
            unknown["diameter_mm"].update(_filament_diameters(source.text))
    return any(len(values) > 1 for values in unknown.values())


def _bundle_temperatures(identity: Identity, page: Evidence, snapshot=None) -> list[dict]:
    color = _product_key(identity.color)
    title = set(_product_key(page.title))
    material = _material_family(identity.material)
    if not color or not ({"bundle", "kit"} & title) or material not in title:
        return []
    line = set(_product_key(identity.designation)) - {color[-1]}
    if len(line) < 2 or not line <= title or not set(color[:-1]) <= line:
        return []
    listing = re.search(
        rf"\bthis set includes ([^.!?]{{1,120}}) {re.escape(material)} filaments?\."
        r".{0,1000}?\btechnical specifications:\s*(.{1,300})",
        page.text, re.IGNORECASE | re.DOTALL,
    )
    if not listing:
        return []
    members, specs = listing.groups()
    context = _variant_context(identity, snapshot)
    finish = _finish(identity.designation, context.get("finish_type"), identity.material)
    subgroup = _norm(context.get("material_subgroup"))
    if context.get("color_mode") == "multi":
        return []
    if not any(
        color[-1] in _product_key(member)
        and (not _norm(identity.sku) or _literal_in(identity.sku, member))
        and (finish in (None, "", "solid") or _literal_in(finish, member) or _literal_in(finish, page.title))
        and (not subgroup or _literal_in(subgroup, member) or _literal_in(subgroup, page.title))
        for member in re.split(r"\s*,\s*|\s+and\s+", members)
    ):
        return []
    diameter = re.match(r"(\d+(?:\.\d+)?)\s*mm\b", specs, re.IGNORECASE)
    weight = re.search(r"\bnet weight\s*:?\s*(\d+(?:\.\d+)?)\s*(kg|g)\s+per spool\b", specs, re.IGNORECASE)
    if (
        identity.diameter_mm is not None and (not diameter or float(diameter[1]) != identity.diameter_mm)
        or identity.weight_g is not None and (
            not weight or float(weight[1]) * (1000 if weight[2].lower() == "kg" else 1) != identity.weight_g
        )
    ):
        return []
    entries = []
    for field in ("extruder_temp_range_c", "bed_temp_range_c"):
        label = SPEC_LABELS[field] + r"(?:\s+temp(?:erature)?)?"
        if match := re.search(label + r"\s*:?\s*(\d{1,3})\s*[-–]\s*(\d{1,3})\s*°\s*C\b", specs, re.IGNORECASE):
            entries.append({
                "field": field, "value": {"min": int(match[1]), "max": int(match[2])},
                "source": 1, "quote": match[0],
            })
    return extract_proposal({"suggestions": entries}, [page])[0]


async def catalog_candidates(identity: Identity) -> tuple[list[dict], str, bool]:
    term = identity.sku.strip() or identity.designation
    if not term:
        return [], "", True
    params = {
        "search": term,
        "manufacturer_name": identity.manufacturer or None,
        "material_key": (
            "pla" if _norm(identity.material) == "pla basic" else _material_family(identity.material)
        ) or None,
    }
    result = await lookup_filaments(**params, report_incomplete=True)
    if not result.get("items") and identity.sku and identity.designation:
        params["search"] = identity.designation
        result = await lookup_filaments(**params, report_incomplete=True)
    if not result.get("items") and params["material_key"]:
        params["material_key"] = None
        result = await lookup_filaments(**params, report_incomplete=True)
    items = result.get("items", [])
    url = f"{settings.filamentdb_url.rstrip('/')}/api/v1/filaments?{urlencode({k: v for k, v in params.items() if v})}"
    return items, url, not result.get("incomplete") and result.get("total", len(items)) <= len(items)


def catalog_values(item: dict) -> dict:
    standard = standard_filamentdb_fields(item, base_url=settings.filamentdb_url)
    sku = standard.get("manufacturer_sku")
    reel = item.get("spool_profile")
    reel = reel if isinstance(reel, dict) else {}
    values = {
        "manufacturer_sku": sku
        if isinstance(sku, str) and 0 < len(sku.strip()) <= 255
        else None,
        "density_g_cm3": _positive(item.get("density_g_cm3", item.get("density"))),
        "datasheet_url": public_link(standard.get("datasheet_url")),
        "drying_temp_c": _positive(standard.get("drying_temp_c")),
        "drying_time_hours": _positive(standard.get("drying_time_hours")),
        **{field: _positive(item.get(key) or reel.get(profile_key)) for field, key, profile_key in (
            ("default_spool_weight_g", "empty_spool_weight_g", "empty_weight_g"),
            ("spool_outer_diameter_mm", "spool_outer_diameter_mm", "outer_diameter_mm"),
            ("spool_width_mm", "spool_width_mm", "width_mm"),
        )},
        "spool_material": _norm(item.get("spool_material") or reel.get("spool_material")).title() or None,
        "image_url": public_link(standard.get("image_url")),
        **{key: standard.get(key) for key in (
            "cooling_fan_range_percent", "ams_compatibility", "build_plate_compatibility",
        )},
        **{
            key: value
            for key in ("softening_temp_c", "chamber_temp_c", "max_volumetric_speed_mm3_s", "flow_ratio", "pressure_advance_k")
            if type(value := standard.get(key)) in (int, float) and LIMITS[key][0] <= value <= LIMITS[key][1]
        },
    }
    bed = item.get("temp_bed")
    for field, low, high, maximum in (
        (
            "extruder_temp_range_c",
            item.get("temp_nozzle_min"),
            item.get("temp_nozzle_max"),
            450,
        ),
        (
            "bed_temp_range_c",
            item.get("temp_bed_min", bed),
            item.get("temp_bed_max", bed),
            200,
        ),
    ):
        if (low is not None or high is not None) and all(
            v is None or (type(v) in (int, float) and 0 <= v <= maximum)
            for v in (low, high)
        ):
            try:
                values[field] = normalize_numeric_range({"min": low, "max": high})
            except ValueError:
                pass
    return values


async def duplicate_ids(db: DBSession, identity: Identity) -> list[int]:
    terms = []
    if identity.sku:
        terms.append(
            func.lower(Filament.manufacturer_sku) == identity.sku.strip().lower()
        )
    if identity.designation:
        name_match = (
            func.lower(Filament.designation) == identity.designation.strip().lower()
        )
        if identity.sku:
            name_match = name_match & or_(
                Filament.manufacturer_sku.is_(None), Filament.manufacturer_sku == ""
            )
        terms.append(name_match)
    if not terms:
        return []
    query = select(Filament.id).join(Filament.manufacturer).where(or_(*terms))
    if identity.manufacturer:
        query = query.where(
            func.lower(Manufacturer.name) == identity.manufacturer.strip().lower()
        )
    for value, column in (
        (identity.material, func.lower(Filament.material_type)),
        (identity.color, func.lower(Filament.manufacturer_color_name)),
        (identity.diameter_mm, Filament.diameter_mm),
        (identity.weight_g, Filament.raw_material_weight_g),
    ):
        if value:
            query = query.where(
                or_(
                    column.is_(None),
                    column
                    == (value.strip().lower() if isinstance(value, str) else value),
                )
            )
    return list(await db.scalars(query.order_by(Filament.id).limit(5)))


def _possible_catalog_matches(identity: Identity, items: list[dict], url: str, source: str) -> list[dict]:
    words = set(_tokens(identity.designation))
    ranked = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            candidate = catalog_identity(item)
        except (ValueError, AttributeError, TypeError):
            continue
        if identity.manufacturer and _norm(identity.manufacturer) != _norm(candidate.manufacturer):
            continue
        sku_match = bool(identity.sku and _norm(identity.sku) == _norm(candidate.sku))
        label = set(_tokens(candidate.designation) + _tokens(candidate.color))
        overlap = words & label
        if not (sku_match or overlap):
            continue
        score = 10 * sku_match + 2 * len(overlap) + (words <= label)
        if _material_family(identity.material) == _material_family(candidate.material):
            score += 1
        name = " · ".join(dict.fromkeys(filter(None, (
            candidate.designation, candidate.color, candidate.material, candidate.sku,
        ))))
        if name:
            ranked.setdefault(name, (score, public_link(item.get("shop_url"))))
    return [
        {"code": "possible_match", "name": name, "source": source,
         "url": product or public_link(url), "product_url": product}
        for name, (_, product) in sorted(ranked.items(), key=lambda row: -row[1][0])[:3]
    ]


def _product_titles(identity: Identity) -> set[str]:
    words = _line(identity.designation, identity.color)
    color = _tokens(identity.color)
    if color and words[:len(color)] == color:
        words = words[len(color):]
    line = " ".join(words).rstrip(" -")
    designation = _norm(identity.designation)
    titles = {title for title in (designation, line) if title}
    material = _norm(identity.material)
    # A generic material must not stand in for a more specific grade.
    if len(_tokens(material)) > 1 and not _literal_in(material, line):
        titles.add(material)
    return titles


def _sheet_title(value: str) -> str:
    value = re.sub(r"[™®]|\btm\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"^(?:产品名称\s*)?product name\s*[:：]\s*", "", _norm(value))
    return re.sub(r"\+\s*(?=\d)", "+", value)


def _sheet_variant_values(page: Evidence) -> dict[str, set]:
    rows = [line.strip() for line in page.text.splitlines()]
    def labels(pattern):
        return [
            match[1] for row in rows
            if (match := re.fullmatch(rf"(?:{pattern})(?:\s*[:：]\s*|\s{{2,}})(\S.*)", row, re.IGNORECASE))
        ]

    values = {
        key: {_product_key(value) for value in labels(pattern)}
        for key, pattern in (
            ("sku", r"sku|article number|product code"),
            ("color", r"colou?r|filament colou?r"),
            ("finish_type", r"finish|surface finish"),
            ("material_subgroup", r"subgroup|material subtype"),
        )
    }
    for key, unit, pattern in (
        ("diameter_mm", r"mm", r"(?:nominal |filament )?diameter"),
        ("weight_g", r"kg|g", r"net (?:filament )?weight|filament weight"),
    ):
        values[key] = {
            float(number.replace(",", ".")) * (1000 if suffix == "kg" else 1)
            for row in labels(pattern)
            for number, suffix in re.findall(rf"(\d+(?:[.,]\d+)?)\s*({unit})\b", _norm(row))
        }
    return values


def _known_variant_fields(identity: Identity, snapshot) -> dict:
    context = _variant_context(identity, snapshot)
    return {
        "sku": _product_key(identity.sku),
        "color": _product_key(identity.color),
        "finish_type": _product_key(_finish(identity.designation, context.get("finish_type"), identity.material)),
        "material_subgroup": _product_key(context.get("material_subgroup")),
        "diameter_mm": identity.diameter_mm,
        "weight_g": identity.weight_g,
    }


def _sheet_matches(page: Evidence, titles: set[str], identity: Identity, snapshot=None) -> bool:
    header = [line.strip() for line in page.text.splitlines() if line.strip()][:8]
    marker = r"\btechnical data\s*sheet\b|物性表"
    if not page.datasheet or not re.search(marker, _norm(" ".join(header))):
        return False
    labeled = []
    candidates = []
    for row in header:
        cells = re.split(r"[ \t]{3,}", row)
        for index, cell in enumerate(cells):
            label = re.fullmatch(r"(?:产品名称\s*)?product name\b\s*[:：]?\s*(.*)", cell, re.IGNORECASE)
            if label:
                labeled.append(label[1] or (cells[index + 1] if index + 1 < len(cells) else ""))
        if len(cells) == 1 or any(re.search(marker, cell, re.IGNORECASE) for cell in cells):
            candidates.extend(cells)
    known = _known_variant_fields(identity, snapshot)
    variants = _sheet_variant_values(page)
    if titles:
        if not {_sheet_title(cell) for cell in (labeled or candidates)}.intersection(map(_sheet_title, titles)):
            return False
    elif not known["sku"] or variants["sku"] != {known["sku"]}:
        return False
    return all(known[key] in (None, ()) or not values or values == {known[key]}
               for key, values in variants.items())


def _sheet_specs(page: Evidence) -> tuple[list[dict], list[dict]]:
    # Specimen test settings and heatbed drying are separate from the general
    # printing settings and the oven drying fields in the filament record.
    lines = page.text.splitlines()
    rows = [" ".join(line.split()) for line in lines]
    page = replace(page, text=" ".join(rows).strip())
    heading = re.compile(r"(?:建议打印参数\s+)?recommended printing (?:settings|conditions|parameters)(?: data)?", re.IGNORECASE)
    other_section = re.compile(
        r"(?:sample preparation|test (?:methods?|conditions)|(?:material |physical |mechanical |thermal )?properties|specifications)",
        re.IGNORECASE,
    )
    specimen = re.compile(r"\b(?:specimen (?:test|printing)|printing test conditions)\b", re.IGNORECASE)
    degrees = r"(?:[°˚]\s*C|℃)"
    density_unit = r"g\s*/\s*cm\s*[³3]"
    entries = []
    printing = False

    def add(field, value, quote):
        entries.append({"field": field, "value": value, "source": 1, "quote": quote})

    for index, (line, row) in enumerate(zip(lines, rows)):
        section = re.sub(r"^(?:[•●]\s*|\d+(?:\.\d+)*[.)]\s+)", "", row).strip().rstrip(":：")
        if heading.fullmatch(section):
            printing = True
        elif row.startswith(("•", "●")) or other_section.fullmatch(section) or specimen.search(section):
            printing = False
        density_row = row.removeprefix("密度 ")
        density = re.fullmatch(
            rf"Density\b.*?\s(\d+(?:\.\d+)?)\s*{density_unit}(?:\s+at\s+\d+(?:\.\d+)?\s*{degrees})?",
            density_row, re.IGNORECASE,
        )
        if density:
            add("density_g_cm3", float(density[1]), density_row)
        elif (
            re.fullmatch(rf"Density\b.*\s{density_unit}", density_row, re.IGNORECASE)
            and index + 1 < len(rows) and re.fullmatch(r"\d+\.\d+", rows[index + 1])
            and lines[index + 1].startswith(" ")
        ):
            add("density_g_cm3", float(rows[index + 1]), f"{density_row} {rows[index + 1]}")
        if not printing:
            continue
        for column in re.split(r"[ \t]{3,}(?=[a-zA-Z])", line.strip()):
            column = " ".join(column.split()).removeprefix("平台温度 ")
            temperature = re.fullmatch(
                rf"(Nozzle|Bed|Plate|Build plate|Build platform) Temp(?:erature|\.)\s*:?\s*(\d+)"
                rf"(?:\s*[-–~]\s*(\d+))?\s*(?:{degrees}|\({degrees}\))",
                column, re.IGNORECASE,
            )
            if temperature:
                field = "extruder_temp_range_c" if temperature[1].casefold() == "nozzle" else "bed_temp_range_c"
                add(field, {"min": int(temperature[2]), "max": int(temperature[3] or temperature[2])}, column)
            drying = re.fullmatch(
                rf"(?:Blast Drying Oven|Drying (?:Oven|Preparation|temp\.? and time))\s*:?\s*(\d+)\s*{degrees}"
                r"(?:\s*[,，]\s*|\s+for\s+)(\d+)(?:\s*[-–~]\s*(\d+))?\s*h(?:ours?)?",
                column, re.IGNORECASE,
            )
            if drying:
                add("drying_temp_c", int(drying[1]), column)
                if drying[3] is None:
                    add("drying_time_hours", int(drying[2]), column)
    return extract_proposal({"suggestions": entries}, [page])


async def research_products(
    db: DBSession, identity: Identity, *, product_url=None, sku_hint="", snapshot=None,
    guidance="", release_read=False, model_id: str | None = None, deadline: float | None = None,
) -> dict:
    proposal = {"suggestions": [], "issues": []}
    issues = proposal["issues"]
    guidance = guidance.strip()
    if snapshot is None and not (identity.sku.strip() or identity.designation.strip()) and sku_hint.strip():
        identity = identity.model_copy(update={"sku": sku_hint.strip()})
    if snapshot is None and (duplicates := await duplicate_ids(db, identity)):
        issues.extend({"code": "duplicate", "filament_id": ident} for ident in duplicates)
        return proposal
    _, config = await config_record(db)
    if release_read:
        # The worker supplies a detached authorized snapshot; do not hold a read
        # transaction across supplier, catalog or model calls.
        await db.rollback()
    evidence: list[Evidence] = []
    catalog_matches: list[tuple[list[tuple[dict, Identity]], str, str]] = []
    possible_matches: dict[str, list[dict]] = {}
    suggestions: dict[str, dict] = {}
    conflict: set[str] = set()
    primary_fields: set[str] = set()
    sheet_fields: set[str] = set()
    field_options: dict[str, list[dict]] = {}

    @asynccontextmanager
    async def network_time(seconds=None):
        limit = deadline
        if seconds is not None:
            local_limit = asyncio.get_running_loop().time() + seconds
            limit = local_limit if limit is None else min(limit, local_limit)
        if limit is not None and limit <= asyncio.get_running_loop().time():
            raise TimeoutError
        async with asyncio.timeout_at(limit):
            yield

    def remember(field, value, citations):
        if is_missing(field, value) or (
            snapshot is not None
            and (field not in MODE_FIELDS["fill"] or not is_missing(field, snapshot.get(field)))
        ):
            return False
        options = field_options.setdefault(field, [])
        same = next((option for option in options if option["value"] == value), None)
        if same is None:
            options.append({"value": value, "sources": list(citations)})
        else:
            same["sources"].extend(cite for cite in citations if cite not in same["sources"])
        return True

    def add(field, value, citations, *, primary=False, datasheet=False):
        if not remember(field, value, citations):
            return
        if (datasheet and field not in sheet_fields) or (primary and field not in primary_fields):
            primary_fields.add(field)
            if datasheet:
                sheet_fields.add(field)
            conflict.discard(field)
            suggestions[field] = {"field": field, "value": value, "sources": list(citations)}
            return
        existing = suggestions.get(field)
        if existing is None:
            suggestions[field] = {"field": field, "value": value, "sources": citations}
        elif existing["value"] == value or (
            isinstance(value, str) and _norm(value) == _norm(existing["value"])
        ):
            existing["sources"].extend(citations)
        elif field not in primary_fields or (primary and field not in sheet_fields) or datasheet:
            conflict.add(field)

    def remember_conflict(issue, *, primary, datasheet=False):
        field = issue["field"]
        if datasheet and field not in sheet_fields:
            suggestions.pop(field, None)
            sheet_fields.add(field)
        for option in issue.get("options", []):
            remember(field, option["value"], option["sources"])
        if datasheet or (field not in sheet_fields and (primary or field not in primary_fields)):
            conflict.add(field)
        if primary and field in field_options:
            primary_fields.add(field)

    async def add_draft_identity(candidate: Identity, cite, *, include_color=True):
        if duplicates := await duplicate_ids(db, candidate):
            issues.extend(
                {"code": "duplicate", "filament_id": ident} for ident in duplicates
            )
            return False
        if candidate.manufacturer:
            ids = list(
                await db.scalars(
                    select(Manufacturer.id)
                    .where(
                        func.lower(Manufacturer.name) == candidate.manufacturer.lower()
                    )
                    .limit(2)
                )
            )
            if len(ids) == 1:
                add("manufacturer_id", ids[0], cite(candidate.manufacturer))
            else:
                issues.append(
                    {"code": "manufacturer_required", "name": candidate.manufacturer}
                )
        for field, value in (
            ("designation", candidate.designation),
            ("material_type", candidate.material),
            ("diameter_mm", candidate.diameter_mm),
            ("manufacturer_color_name", candidate.color if include_color else None),
            ("raw_material_weight_g", candidate.weight_g),
            ("manufacturer_sku", candidate.sku),
        ):
            add(field, value, cite(value))
        return True

    checked_pages: dict[str, Evidence | None] = {}
    fetch_errors: dict[str, str] = {}
    supplier_max_bytes = config["supplier_download_mib"] * 1024 * 1024

    async def supplier(url):
        if url not in checked_pages:
            try:
                async with network_time():
                    checked_pages[url] = await fetch_supplier(url, max_bytes=supplier_max_bytes)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                checked_pages[url] = None
                fetch_errors[url] = (
                    "Request timed out" if isinstance(exc, TimeoutError)
                    else str(exc) if isinstance(exc, ValueError)
                    else "Connection failed"
                )
        return checked_pages[url]

    async def check_pages(urls):
        pending = [url for url in dict.fromkeys(urls) if url and url not in checked_pages][:4]
        by_host = {}
        for url in pending:
            by_host.setdefault(_site_host(url), []).append(url)

        async def check_host(urls):
            try:
                async with network_time(8):
                    for url in urls:
                        await supplier(url)
            except TimeoutError:
                for url in urls:
                    if url not in checked_pages:
                        checked_pages[url] = None
                        fetch_errors[url] = "Request timed out"

        await asyncio.gather(*(check_host(urls) for urls in by_host.values()))
        return {url: checked_pages[url] for url in pending}

    def search_terms(sku):
        return " ".join(filter(None, (
            identity.manufacturer, identity.designation,
            identity.material if not _literal_in(identity.material, identity.designation) else "",
            sku, identity.color if not _swatch_code(identity.color) and not _literal_in(identity.color, identity.designation) else "",
        )))

    search_available = bool(config["search_url"])
    search_issue = None

    async def search(term):
        nonlocal search_available, search_issue
        if term and search_available:
            try:
                async with network_time():
                    return await search_web(config["search_url"], term)
            except (httpx.HTTPError, TimeoutError, ValueError, AttributeError, TypeError) as error:
                if not isinstance(error, SearchEnginesUnavailable):
                    search_available = False
                if search_issue is None:
                    search_issue = {"code": "unavailable_source", "source": "Web search"}
                    if isinstance(error, ValueError):
                        search_issue["reason"] = str(error)
                    issues.append(search_issue)
        return []

    # Resolve a pasted product URL first so its brand and SKU can drive both catalogs.
    pasted_page = None
    verified_paramount_url = False
    resolve_pasted_url = (
        snapshot is None
        and product_url
        and not identity.designation
        and not identity.sku
    )
    if resolve_pasted_url and product_url:
        pasted_page = await supplier(product_url)
        paramount = _paramount_url_identity(pasted_page) if pasted_page else None
        if pasted_page and (paramount or pasted_page.product):
            product = paramount or Identity(**pasted_page.product)
            fields = ("material", "color", "diameter_mm", "weight_g")
            constraints = {
                key: getattr(identity, key) for key in fields if getattr(identity, key)
            }
            if (
                not identity.manufacturer
                or _norm(identity.manufacturer) == _norm(product.manufacturer)
            ) and (
                not constraints
                or supplier_matches(
                    product.model_copy(update={**constraints, "sku": ""}),
                    replace(pasted_page, manufacturer_page=True) if paramount else pasted_page,
                )
            ):

                def cite_product(value):
                    return [
                        {
                            "url": pasted_page.url,
                            "title": pasted_page.title,
                            "evidence": str(value),
                        }
                    ]

                if not await add_draft_identity(product, cite_product, include_color=not paramount):
                    return proposal
                identity = product.model_copy(update=constraints)
                verified_paramount_url = bool(paramount)
                if not verified_paramount_url:
                    evidence.extend(_supplier_scopes(identity, pasted_page))

    manufacturer_site = (snapshot or {}).get("manufacturer_url")
    if snapshot is None and identity.manufacturer:
        manufacturer_site = await db.scalar(
            select(Manufacturer.url).where(
                func.lower(Manufacturer.name) == _norm(identity.manufacturer)
            ).limit(1)
        )
    if release_read and db.in_transaction():
        await db.rollback()
    saved_domain = _site_host(manufacturer_site)
    manufacturer_domain = "paramount-3d.com" if verified_paramount_url else saved_domain
    search_domain = manufacturer_domain
    if not saved_domain and identity.manufacturer:
        if verified_paramount_url:
            discovered = "https://www.paramount-3d.com/"
            source = "Manufacturer product page"
        else:
            try:
                async with network_time():
                    discovered = manufacturer_site_url(await manufacturer_website(identity.manufacturer))
            except TimeoutError:
                discovered = None
            source = "FilaManDB"
        if not discovered:
            # Require a brand-owned-looking host and a branded filament homepage.
            brand = re.sub(r"[^a-z0-9]", "", _norm(identity.manufacturer))
            homepages = []
            product_home = None
            if len(brand) >= 3:
                product_publisher = _publisher(product_url or (snapshot or {}).get("shop_url"))
                if re.sub(r"[^a-z0-9]", "", product_publisher.split(".")[0]) == brand:
                    product_home = f"https://{product_publisher}/"
                    homepages.append(product_home)
                for url in await search(f"{identity.manufacturer} filament official website"):
                    if re.sub(r"[^a-z0-9]", "", _publisher(url).split(".")[0]) == brand:
                        link = manufacturer_site_url(url)
                        if link:
                            homepages.append(urlunsplit(urlsplit(link)._replace(path="/")))
            homepage_pages = await check_pages(homepages)
            matches = {
                site for page in homepage_pages.values()
                if page and _literal_in(identity.manufacturer, page.title)
                and re.search(r"filament|3d print", page.title + " " + page.text, re.IGNORECASE)
                and re.sub(r"[^a-z0-9]", "", _publisher(page.url).split(".")[0]) == brand
                and (site := manufacturer_site_url(page.url))
            }
            home_sites = {site for site in matches if _site_host(site) == _publisher(site)}
            candidates = home_sites or matches
            if len(candidates) == 1:
                discovered = next(iter(candidates))
                direct_homepage = homepage_pages.get(product_home)
                source = (
                    "Product URL" if direct_homepage and manufacturer_site_url(direct_homepage.url) == discovered
                    else "SearXNG"
                )
        if discovered:
            manufacturer_site = discovered
            search_domain = _site_host(discovered)
            if source in ("FilaManDB", "Manufacturer product page"):
                manufacturer_domain = search_domain
            if snapshot is not None and is_missing("manufacturer_url", snapshot.get("manufacturer_url")):
                issues.append({
                    "code": "manufacturer_website_found", "url": discovered,
                    "name": identity.manufacturer, "source": source,
                })
    direct_urls = [
        supplier_page_url(link) for url in (
            product_url if not resolve_pasted_url else None,
            (snapshot or {}).get("shop_url"),
            (snapshot or {}).get("datasheet_url"),
        ) if (link := public_link(url))
    ]
    first_urls = [url for url in direct_urls if _on_manufacturer_site(url, manufacturer_domain)]
    first_pages = await check_pages(first_urls)
    if verified_paramount_url:
        first_pages[pasted_page.url] = pasted_page
    imported_sku = (snapshot or {}).get("shop_url") or ""
    if not re.fullmatch(r"(?=[\w.-]*[A-Z])(?=[\w.-]*\d)[A-Z0-9][\w.-]{5,40}", imported_sku, re.IGNORECASE):
        imported_sku = ""
    lookup_sku = identity.sku.strip() or sku_hint.strip() or imported_sku
    initial_query = search_terms(lookup_sku)
    early_web = not search_domain and bool(identity.manufacturer and initial_query)
    web_urls = []
    def official_match():
        return any(
            page and not (page.manual or page.datasheet) and _on_manufacturer_site(page.url, manufacturer_domain)
            and _supplier_scopes(identity, replace(page, manufacturer_page=True), snapshot)
            for page in first_pages.values()
        )

    if manufacturer_domain == "paramount-3d.com" and not official_match():
        try:
            async with network_time():
                candidates = await _paramount_candidates(identity, lookup_sku)
            first_pages.update(await check_pages(candidates))
        except TimeoutError:
            issues.append({"code": "unavailable_source", "source": "Paramount 3D"})
    if search_domain and not official_match():
        official_results = await search(f"site:{search_domain} {initial_query}")
        first_pages.update(await check_pages(
            [url for url in official_results if _on_manufacturer_site(url, search_domain)]
            or ([manufacturer_site] if not first_urls else [])
        ))
    elif early_web:
        web_urls = await search(initial_query)
        first_pages.update(await check_pages(web_urls))

    titles = _product_titles(identity)
    authoritative_docs: dict[str, Evidence] = {}
    archived_official_urls: set[str] = set()

    document_domain = manufacturer_domain or search_domain
    linked_sheets: dict[str, Evidence | None] = {}

    async def check_datasheets(pages):
        linked = []
        for page in pages.values():
            if not page or not _on_manufacturer_site(page.url, document_domain):
                continue
            if _sheet_matches(page, titles, identity, snapshot):
                authoritative_docs[page.url] = replace(page, manufacturer_page=True)
            linked.extend(link for url in page.datasheets if (link := public_link(url)))
        links = list(dict.fromkeys(linked))[:3]
        await check_pages(links)
        for link in links:
            sheet = checked_pages[link]
            linked_sheets[link] = sheet
            if sheet and _sheet_matches(sheet, titles, identity, snapshot):
                authoritative_docs[sheet.url] = replace(sheet, manufacturer_page=True)

    await check_datasheets(first_pages)

    def primary_source(url):
        return url in authoritative_docs or url in archived_official_urls or _on_manufacturer_site(url, manufacturer_domain)

    async def from_catalog(name):
        try:
            async with network_time():
                if name == "FilaManDB":
                    items, url, complete = await catalog_candidates(identity)
                else:
                    items, url = await ofd_candidates(identity.manufacturer)
                    complete = True
        except (httpx.HTTPError, HTTPException, TimeoutError, ValueError, AttributeError, TypeError):
            issues.append({"code": "unavailable_source", "source": name})
            return
        possible_matches[name] = _possible_catalog_matches(identity, items, url, name)
        matches = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                candidate = _catalog_variant(identity, item)
                if same_variant(identity, candidate, snapshot, item):
                    matches.append((item, candidate))
            except (ValueError, AttributeError, TypeError):
                continue
        if matches and complete and (snapshot is not None or len(matches) == 1):
            catalog_matches.append((matches, url, name))
        elif matches or (name == "FilaManDB" and items):
            issues.append({"code": "ambiguous_match", "source": name})

    async def from_linked_spool():
        profile_id = (snapshot or {}).get("filamentdb_spool_profile_id")
        fields = ("default_spool_weight_g", "spool_outer_diameter_mm", "spool_width_mm", "spool_material")
        if type(profile_id) is not int or profile_id <= 0 or not any(
            is_missing(field, snapshot.get(field)) for field in fields
        ):
            return
        try:
            async with network_time():
                profile = await linked_spool_profile(profile_id)
        except (HTTPException, httpx.HTTPError, TimeoutError, ValueError):
            issues.append({"code": "unavailable_source", "source": "Linked FilamentDB spool profile"})
            return
        values = catalog_values({"spool_profile": profile})
        url = f"{settings.filamentdb_url.rstrip('/')}/api/v1/spool-profiles/{profile_id}"
        for field in fields:
            value = values[field]
            if field in LIMITS and (value is None or not LIMITS[field][0] <= value <= LIMITS[field][1]):
                continue
            add(field, value, [{"url": url, "title": "Linked FilamentDB spool profile", "evidence": str(value)}], primary=True)

    await asyncio.gather(
        from_catalog("FilaManDB"), from_catalog("Open Filament Database"), from_linked_spool()
    )
    all_matches = [row for matches, _, _ in catalog_matches for row in matches]
    candidates = [candidate for _, candidate in all_matches]
    variants = {
        (
            _tokens(candidate.color), candidate.diameter_mm, candidate.weight_g,
            _catalog_finish(item, candidate),
            "basic" if _norm(candidate.material) == "pla basic" else _norm(_catalog_subgroup(item)),
            _catalog_color_mode(item),
        )
        for item, candidate in all_matches
    }
    skus = {_norm(candidate.sku) for candidate in candidates if _norm(candidate.sku)}
    swatch_codes = set()
    if _norm(identity.material) == "pla basic":
        for item, _ in all_matches:
            if code := re.search(r"\s+\((\d+)\)$", _norm(item.get("color_name"))):
                swatch_codes.add(code[1])
    unresolved_variants = len(variants) > 1 or len(skus | swatch_codes) > 1
    variant_sku = next(iter(skus)) if not unresolved_variants and len(skus) == 1 else ""
    ambiguous_identity = False
    if len(candidates) > 1:
        names = {_tokens(candidate.designation) for candidate in candidates if candidate.designation}
        lines = {_line(candidate.designation, candidate.color) for candidate in candidates if candidate.designation}
        fields = ("manufacturer", "material", "color", "diameter_mm", "weight_g", "sku")
        if len(names) > 1 and (len(lines) > 1 or () in lines) or (
            snapshot is None and any(
                len({
                    _norm(value) if isinstance(value, str) else value
                    for candidate in candidates if (value := getattr(candidate, field))
                }) > 1
                for field in fields
            )
        ):
            issues.append({"code": "ambiguous_match"})
            ambiguous_identity = True
            catalog_matches.clear()
            suggestions.clear()
            variant_sku = ""
    catalog_urls = []
    if pasted_page and pasted_page.manual:
        issues.append({"code": "manual_datasheet", "url": pasted_page.url})

    def row_values(row, variant):
        return {
            **catalog_values(row),
            "raw_material_weight_g": variant.weight_g,
            "manufacturer_color_name": variant.color,
        }

    shared_fields = set(MODE_FIELDS["fill"])
    if unresolved_variants and snapshot is not None:
        issues.append({"code": "ambiguous_match"})
        available = [row_values(row, variant) for row, variant in all_matches]
        for field in MODE_FIELDS["fill"]:
            values = [row.get(field) for row in available]
            if any(is_missing(field, value) for value in values) or any(
                value != values[0] for value in values[1:]
            ):
                shared_fields.discard(field)
            known = [value for value in values if not is_missing(field, value)]
            if is_missing(field, snapshot.get(field)) and known and any(
                value != known[0] for value in known[1:]
            ):
                conflict.add(field)

    for matches, url, name in catalog_matches:
        item, candidate = matches[0]

        def cite(value, url=url, name=name):
            return [{"url": url, "title": name, "evidence": str(value)}]

        if snapshot is None and not await add_draft_identity(candidate, cite):
            return proposal
        if snapshot is None and candidate.color:
            colors = item.get("colors")
            if not isinstance(colors, list) or len(colors) <= 1:
                swatch = colors[0] if colors and isinstance(colors[0], dict) else {}
                try:
                    hex_code = normalize_hex_color(
                        swatch.get("hex_code")
                        or item.get("color_hex")
                        or item.get("hex_color")
                    )
                except ValueError:
                    hex_code = None
                color_query = select(Color.id).where(
                    func.lower(Color.name) == candidate.color.strip().lower()
                )
                if hex_code:
                    color_query = color_query.where(
                        func.lower(Color.hex_code) == hex_code.lower()
                    )
                color_ids = list(await db.scalars(color_query.limit(2)))
                if len(color_ids) == 1:
                    add("color_id", color_ids[0], cite(candidate.color))
                else:
                    issues.append({"code": "color_required", "name": candidate.color})
        if snapshot is not None:
            for row, variant in matches:
                for field, value in row_values(row, variant).items():
                    if field in shared_fields:
                        add(field, value, cite(value))
                    elif field in conflict:
                        remember(field, value, cite(value))
        else:
            for field, value in row_values(item, candidate).items():
                add(field, value, cite(value))
        for row, variant in matches:
            swatch = row.get("color_hex") or row.get("hex_color")
            if _swatch_code(identity.color) and _swatch_code(swatch) and (
                _norm(identity.color).removeprefix("#") != _norm(swatch).removeprefix("#")
            ):
                issue = {
                    "code": "color_reference", "name": f'#{_norm(identity.color).removeprefix("#").upper()} / #{_norm(swatch).removeprefix("#").upper()}',
                    "source": name, "url": url,
                }
                if issue not in issues:
                    issues.append(issue)
            if (not sku_hint or _norm(sku_hint) == _norm(variant.sku)) and (
                source_url := public_link(row.get("shop_url"))
            ):
                catalog_urls.append(source_url)
    supplier_query = search_terms(identity.sku.strip() or variant_sku or sku_hint.strip() or imported_sku)
    if release_read and db.in_transaction():
        await db.rollback()
    if not early_web:
        web_urls = await search(supplier_query)
    topic = guidance or "filament print settings spool dimensions"
    supplement = await search(f"{search_terms('')} {topic}")
    web_urls = list(dict.fromkeys(
        [*supplement, *web_urls] if guidance else [*web_urls, *supplement]
    ))
    # Try distinct sources first; retailer duplicates can otherwise crowd out profiles.
    different_sites = {}
    for url in web_urls:
        if link := public_link(url):
            different_sites.setdefault(_site_host(link), supplier_page_url(link))

    supplier_identity = identity
    if snapshot is not None and variant_sku and not _norm(identity.sku):
        proven = next(candidate for candidate in candidates if _norm(candidate.sku) == variant_sku)
        supplier_identity = identity.model_copy(update={
            "sku": proven.sku,
            "color": identity.color or proven.color,
            "weight_g": identity.weight_g if identity.weight_g is not None else proven.weight_g,
        })

    async def find_supplier_page(domain):
        urls = await search(f"site:{domain} {supplier_query}")
        matched_pages = {}
        failed_pages = {}
        link = None
        try:
            async with network_time(8):
                for url in urls:
                    if not _on_manufacturer_site(url, domain) or urlsplit(url).path in ("", "/"):
                        continue
                    link = supplier_page_url(url)
                    page = await supplier(link)
                    if page and _supplier_scopes(supplier_identity, page, snapshot):
                        matched_pages[link] = page
                    if page is None:
                        failed_pages[link] = None
        except TimeoutError:
            if link is not None:
                fetch_errors[link] = "Request timed out"
                failed_pages[link] = None
        return matched_pages or failed_pages

    supplier_urls = [*direct_urls, *(supplement if guidance else []), *catalog_urls, *different_sites.values()]
    pages = first_pages | await check_pages(supplier_urls)
    if sum(bool(page and not _on_manufacturer_site(page.url, manufacturer_domain)) for page in pages.values()) < 2:
        pages.update(await check_pages(supplier_urls))
    for preferred in await asyncio.gather(*(
        find_supplier_page(site["domain"]) for site in config["supplier_sites"]
        if site["enabled"] and supplier_query and site["domain"] != search_domain
    )):
        pages.update(preferred)
    await check_datasheets(pages)
    pages.update(linked_sheets)
    if product_url and fetch_errors.get(product_url) == SUPPLIER_TOO_LARGE:
        pages.setdefault(product_url, None)

    def url_words(url):
        return set(re.findall(r"[^\W_]+", unquote(urlsplit(url).path).casefold()))

    oversized = [
        url for url in dict.fromkeys([*direct_urls, *pages])
        if fetch_errors.get(url) == SUPPLIER_TOO_LARGE
    ]
    recovered_urls: set[str] = set()
    fallback_docs: set[str] = set()
    oversized_sheets = set(oversized) & linked_sheets.keys()
    official_oversized = oversized_sheets | {url for url in oversized if _on_manufacturer_site(url, document_domain)}
    product_line = " ".join(_line(identity.designation, identity.color))
    line = set(re.findall(r"[^\W_]+", product_line))
    product_slug = _product_key(product_line)
    color = set(_tokens(identity.color))
    sku = "-".join(_product_key(lookup_sku or variant_sku))

    known_variant = _known_variant_fields(supplier_identity, snapshot)

    def needs_variant(sheet):
        return any(
            values and known_variant[key] in (None, ()) for key, values in _sheet_variant_values(sheet).items()
        )

    def has_usable_fallback():
        return any(not needs_variant(authoritative_docs[url]) for url in fallback_docs)

    def retry_without_slash(link):
        parsed = urlsplit(link)
        path = parsed.path.rstrip("/")
        if path and path != parsed.path and fetch_errors.get(link) == SUPPLIER_TOO_LARGE:
            return urlunsplit(parsed._replace(path=path))
        return None

    async def check_alternatives(host, urls, *, recover_sheet=False, recover_product=True):
        links = list(dict.fromkeys(
            supplier_page_url(link) for url in urls if (link := public_link(url))
            and _site_host(link) == host and urlsplit(link).path not in ("", "/")
        ))[:3]
        alternatives = await check_pages(links)
        alternative = None
        for link in links:
            page = checked_pages.get(link)
            if not page or _site_host(page.url) != host:
                continue
            if page.datasheet:
                if _on_manufacturer_site(page.url, document_domain) and _sheet_matches(page, titles, supplier_identity, snapshot):
                    authoritative_docs[page.url] = replace(page, manufacturer_page=True)
                    fallback_docs.add(page.url)
                continue
            if recover_sheet:
                await check_datasheets({link: page})
                fallback_docs.update(
                    sheet.url for url in page.datasheets
                    if (sheet := linked_sheets.get(url)) and sheet.url in authoritative_docs
                )
            if not recover_product or alternative:
                continue
            if resolve_pasted_url and not pasted_page and page.product and page.product.get("designation"):
                issues.append({
                    "code": "possible_match", "name": page.title[:255], "source": "Web search",
                    "url": page.url, "product_url": page.url,
                })
                alternative = page.url
                continue
            official = _on_manufacturer_site(page.url, manufacturer_domain)
            if (
                _supplier_scopes(supplier_identity, replace(page, manufacturer_page=official), snapshot)
                or (official and _bundle_temperatures(supplier_identity, page, snapshot))
            ):
                pages.update({url: result for url, result in alternatives.items() if result})
                pages.setdefault(link, page)
                alternative = page.url
        return alternative

    product_line = product_line or lookup_sku

    def record_recovery(original, alternative):
        if alternative and original not in recovered_urls:
            recovered_urls.add(original)
            issues.append({"code": "supplier_alternative_found", "url": alternative})

    async def recover_alternatives(host, original, urls, *, recover_sheet, recover_product):
        alternative = await check_alternatives(
            host, urls, recover_sheet=recover_sheet, recover_product=recover_product,
        )
        record_recovery(original, alternative)
        if recover_sheet and not has_usable_fallback():
            # Some product URLs redirect to a larger storefront only when the path ends in a slash.
            for url in urls:
                if (link := public_link(url)) and _site_host(link) == host and (
                    without_slash := retry_without_slash(supplier_page_url(link))
                ):
                    found = await check_alternatives(
                        host, [without_slash], recover_sheet=True, recover_product=recover_product and not alternative,
                    )
                    alternative = alternative or found
                    record_recovery(original, alternative)
                    break
        return alternative

    try:
        async with network_time(15):
            if product_line and document_domain and official_oversized:
                found = await search(f"site:{document_domain} {product_line} technical data sheet filetype:pdf")
                links = [
                    link for url in found if (link := public_link(url))
                    and _on_manufacturer_site(link, document_domain) and urlsplit(link).path not in ("", "/")
                ][:4]
                await check_pages(links)
                for link in links:
                    sheet = checked_pages.get(link)
                    if sheet and _on_manufacturer_site(sheet.url, document_domain) and _sheet_matches(sheet, titles, supplier_identity, snapshot):
                        authoritative_docs[sheet.url] = replace(sheet, manufacturer_page=True)
                        fallback_docs.add(sheet.url)
            # A sheet that requires an unknown variant can still be rejected after supplier checks.
            usable_fallback = has_usable_fallback()
            # shortcut: check two sites per lookup, raise the budget if common searches need more.
            fallback_by_host = {}
            if document_domain and not usable_fallback:
                official = next((url for url in oversized if url in official_oversized), None)
                if official and (official in linked_sheets or _site_host(official) != document_domain):
                    origin = manufacturer_site_url(manufacturer_site)
                    if _site_host(origin) != document_domain:
                        origin = f"https://{document_domain}/"
                    fallback_by_host[document_domain] = (origin, official)
            for url in oversized:
                if url not in linked_sheets and not (usable_fallback and url in official_oversized) and (host := _site_host(url)):
                    fallback_by_host.setdefault(host, (url, url))
            for host, (sitemap_origin, original) in list(fallback_by_host.items())[:2]:
                if original in recovered_urls:
                    continue
                recover_sheet = host == document_domain and bool(official_oversized)
                recover_product = original not in oversized_sheets
                matches = []
                path_terms = url_words(original) - {"store", "shop", "products", "product"}
                for url in await supplier_sitemap_urls(sitemap_origin):
                    if not (link := public_link(url)) or _site_host(link) != host:
                        continue
                    checked_link = supplier_page_url(link)
                    if checked_link in checked_pages and not checked_pages[checked_link] and not (
                        recover_sheet and retry_without_slash(checked_link)
                    ):
                        continue
                    words = url_words(link)
                    path = "-".join(_product_key(unquote(urlsplit(link).path)))
                    sku_match = bool(sku and f"-{sku}-" in f"-{path}-")
                    if sku_match or (line and line <= words) or (resolve_pasted_url and path_terms and path_terms <= words):
                        slug = unquote(urlsplit(link).path.rstrip("/").rsplit("/", 1)[-1])
                        exact_slug = bool(product_slug and _product_key(slug) == product_slug)
                        matches.append((link, sku_match, len(words & color), exact_slug))
                matches.sort(key=lambda match: (-match[1], -match[2], -match[3]))
                alternative = await recover_alternatives(
                    host, original, [url for url, *_ in matches[:2]],
                    recover_sheet=recover_sheet, recover_product=recover_product,
                )
                query = supplier_query if identity.designation or lookup_sku else " ".join(sorted(url_words(original)))
                if query and (not alternative or (recover_sheet and oversized_sheets and not has_usable_fallback())):
                    await recover_alternatives(
                        host, original, await search(f"site:{host} {query} technical data sheet"),
                        recover_sheet=recover_sheet, recover_product=recover_product and not alternative,
                    )
    except TimeoutError:
        pass

    await check_datasheets(pages)
    pages.update(linked_sheets)
    if not any(
        page and _on_manufacturer_site(page.url, manufacturer_domain)
        and (
            _supplier_scopes(identity, replace(page, manufacturer_page=True), snapshot)
            or _bundle_temperatures(identity, page, snapshot)
        )
        for page in pages.values()
    ):
        originals = [
            url for url in dict.fromkeys([*direct_urls, *pages])
            if fetch_errors.get(url, "").startswith(("HTTP 403", "HTTP 429", "HTTP 503", "Automated access blocked"))
            and urlsplit(url).path not in ("", "/") and not urlsplit(url).query
        ]
        originals.sort(key=lambda url: (not _on_manufacturer_site(url, manufacturer_domain), url not in direct_urls))
        try:
            async with network_time(15):
                for original in originals[:3]:
                    try:
                        archived = await fetch_archived_supplier(original, max_bytes=supplier_max_bytes)
                    except (httpx.HTTPError, OSError, ValueError, AttributeError, TypeError):
                        continue
                    if not archived:
                        continue
                    official = _on_manufacturer_site(original, manufacturer_domain)
                    archived = replace(archived, origin=archived.origin or _publisher(original), manufacturer_page=official)
                    pages[archived.url] = archived
                    if official:
                        archived_official_urls.add(archived.url)
                        if _sheet_matches(archived, titles, identity, snapshot):
                            authoritative_docs[archived.url] = archived
                    if archived.url in authoritative_docs or _supplier_scopes(identity, archived, snapshot):
                        break
        except TimeoutError:
            pass
    official_scopes = []
    for url, page in pages.items():
        if page is None:
            reason = fetch_errors.get(url)
            if reason == SUPPLIER_TOO_LARGE:
                continue
            source_name = "Manufacturer page" if url in linked_sheets or _on_manufacturer_site(url, manufacturer_domain) else "Supplier page"
            issues.append({"code": "unavailable_source", "source": source_name,
                           "url": public_link(url), "reason": reason})
            continue
        if page.url in authoritative_docs:
            continue
        official = primary_source(page.url)
        citation_title = f"{page.title} ({page.archived_at})" if page.archived_at else page.title
        if page.archived_at:
            issues.insert(0, {"code": "archived_page", "name": citation_title, "url": page.url})
        elif official:
            issues.insert(0, {"code": "manufacturer_page", "name": page.title, "url": public_link(page.url)})
        if page.manual or page.datasheet:
            issues.append({"code": "manual_datasheet", "url": page.url})
            continue
        try:
            page = replace(page, manufacturer_page=official)
            scopes = _supplier_scopes(supplier_identity, page, snapshot)
            evidence.extend(replace(scope, title=citation_title) for scope in scopes)
            if official:
                official_scopes.append((page, citation_title, scopes))
        except ValueError:
            continue

    matching_docs = [
        sheet for sheet in authoritative_docs.values()
        if _sheet_matches(sheet, titles, supplier_identity, snapshot)
    ]
    if _supplier_variants_disagree([*evidence, *matching_docs], supplier_identity, snapshot):
        if not unresolved_variants:
            issues.append({"code": "ambiguous_match", "source": "Supplier pages"})
        unresolved_variants = True
    recovered_sheet = None
    for sheet in authoritative_docs.values():
        if sheet not in matching_docs or (unresolved_variants and needs_variant(sheet)):
            issues.append({"code": "manual_datasheet", "url": sheet.url})
            continue
        if sheet.url in fallback_docs and recovered_sheet is None:
            recovered_sheet = sheet.url
        issues.insert(0, {"code": "manufacturer_page", "name": sheet.title, "url": sheet.url})
        add("datasheet_url", sheet.url, [{"url": sheet.url, "title": sheet.title, "evidence": "Technical Data Sheet"}], primary=True, datasheet=True)
        sheet_values, sheet_conflicts = _sheet_specs(sheet)
        for result in sheet_values:
            add(result["field"], result["value"], result["sources"], primary=True, datasheet=True)
        for issue in sheet_conflicts:
            remember_conflict(issue, primary=True, datasheet=True)
    if recovered_sheet:
        recovered_urls.update(official_oversized)
        issues.append({"code": "supplier_alternative_found", "url": recovered_sheet})
    issues.extend(
        {"code": "supplier_download_limit", "url": public_link(url)}
        for url in dict.fromkeys([*direct_urls, *pages])
        if fetch_errors.get(url) == SUPPLIER_TOO_LARGE and url not in recovered_urls
    )

    has_published_color = False
    if not unresolved_variants:
        for page, citation_title, scopes in official_scopes:
            for spec in _bundle_temperatures(supplier_identity, replace(page, title=citation_title), snapshot):
                add(spec["field"], spec["value"], spec["sources"], primary=True)
            verified_scopes = [scope for scope in scopes if not scope.shared]
            if not verified_scopes:
                continue

            def cite(quote, url=page.url, title=citation_title):
                return [{"url": url, "title": title, "evidence": quote}]

            if not page.archived_at:
                add("shop_url", page.url, cite(page.title), primary=True)
            colors = dict.fromkeys(
                item for scope in verified_scopes for item in _published_colors(scope.text)
            )
            for color, _ in colors:
                issue = {"code": "manufacturer_color_reference", "name": color, "url": page.url}
                if issue not in issues:
                    issues.append(issue)
            if colors:
                has_published_color = True
                color, quote = next(iter(colors))
                add("manufacturer_color_name", color, cite(quote), primary=True)
            if product := _packaged_product(supplier_identity, page, snapshot):
                title, part, diameter, weight = product
                sku_quote = next((
                    line for line in page.scopes or () if part and _LABELLED_SKU.findall(line) == [part]
                ), title)
                add("manufacturer_sku", part, cite(sku_quote), primary=True)
                add("diameter_mm", diameter, cite(title), primary=True)
                add("raw_material_weight_g", weight, cite(title), primary=True)
                if not page.archived_at and (image := public_link((page.product or {}).get("image_url"))):
                    add("image_url", image, cite(title), primary=True)
                if _site_host(page.url) == "paramount-3d.com":
                    if not colors and (heading := _PACKAGED_TITLE.fullmatch(title)) and (
                        name := _PACKAGED_NAME.fullmatch(heading["name"])
                    ):
                        add("manufacturer_color_name", name[2], cite(title), primary=True)
                    for scope in verified_scopes:
                        for field, value, quote in _paramount_specs(scope.text):
                            add(field, value, cite(quote), primary=True)
                for scope in verified_scopes:
                    if size := _REEL_SIZE.search(scope.text):
                        for field, value in zip(("spool_outer_diameter_mm", "spool_width_mm"), size.groups()):
                            add(field, round(float(value) * 25.4, 2), cite(size[0]), primary=True)

    if unresolved_variants:
        evidence = [source for source in evidence if source.shared]
    if ambiguous_identity:
        evidence = [source for source in evidence if source.manufacturer_page]
    evidence.sort(key=lambda source: not source.manufacturer_page)
    first_by_site = {}
    for source in evidence:
        first_by_site.setdefault(_site_host(source.url), source)
    extraction_evidence = [*first_by_site.values(), *(source for source in evidence if source not in first_by_site.values())]

    if evidence and config["model_url"] and (config["model_id"] or model_id):
        try:
            async with network_time(22):
                model = await resolve_model(config, model_id)
                extracted, model_issues = await request_extraction(
                    config["model_url"],
                    model,
                    model_key(config),
                    supplier_identity.model_dump() | ({
                        field: snapshot[field] for field in ("finish_type", "material_subgroup", "color_mode")
                    } if snapshot is not None else {}),
                    extraction_evidence[:8],
                    guidance=guidance,
                )
            for issue in model_issues:
                if has_published_color and issue["field"] == "manufacturer_color_name":
                    continue
                remember_conflict(issue, primary=any(
                    primary_source(citation["url"])
                    for option in issue.get("options", []) for citation in option["sources"]
                ))
            for result in extracted:
                field = result["field"]
                if has_published_color and field == "manufacturer_color_name":
                    continue
                primary = any(primary_source(citation["url"]) for citation in result["sources"])
                add(field, result["value"], result["sources"], primary=primary)
                for option in result.get("options", []):
                    remember(field, option["value"], option["sources"])
        except (
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            RuntimeError,
        ):
            issues.append({"code": "unavailable_source", "source": "Configured model"})

    if snapshot is None and suggestions:
        resolved = identity.model_copy(
            update={
                key: suggestions[field]["value"]
                for key, field in (
                    ("sku", "manufacturer_sku"),
                    ("material", "material_type"),
                    ("diameter_mm", "diameter_mm"),
                )
                if field in suggestions and field not in conflict
            }
        )
        if duplicates := await duplicate_ids(db, resolved):
            issues.extend(
                {"code": "duplicate", "filament_id": ident} for ident in duplicates
            )
            return proposal
        if "manufacturer_id" in suggestions and "manufacturer_id" not in conflict:
            draft = {
                "manufacturer_id": suggestions["manufacturer_id"]["value"],
                "designation": identity.designation,
                "material_type": identity.material,
                "manufacturer_color_name": identity.color,
                **{field: item["value"] for field, item in suggestions.items() if field not in conflict},
            }
            local = await _local_conventions(db, draft)
            for unit in local["proposals"]:
                if len(unit["fields"]) != 1:
                    continue
                field, change = next(iter(unit["fields"].items()))
                if field not in suggestions or field in conflict:
                    continue
                suggestions[field]["value"] = change["value"]
                suggestions[field]["sources"].extend(
                    {"title": source["title"], "evidence": source["quote"]} for source in unit["sources"]
                )
    corroborated = {}
    if not (unresolved_variants or ambiguous_identity):
        vote_fields = {
            "density_g_cm3", "extruder_temp_range_c", "bed_temp_range_c", "drying_temp_c",
            "drying_time_hours", "softening_temp_c", "cooling_fan_range_percent", "chamber_temp_c",
        }
        for field in (conflict - primary_fields) & vote_fields:
            if option := _corroborated_option(field_options.get(field, [])):
                corroborated[field] = option
                suggestions[field] = {"field": field, **option}
    conflict.difference_update(corroborated)
    disagreements = {field for field in primary_fields if len(field_options[field]) > 1}
    for field in sorted(conflict | disagreements | corroborated.keys()):
        options = sorted(
            field_options.get(field, []),
            key=lambda option: (
                option["value"] != suggestions.get(field, {}).get("value") or field in conflict,
                not any(primary_source(source.get("url")) for source in option["sources"]),
            ),
        )
        issue = {"code": "conflict" if field in conflict else "corroborated" if field in corroborated else "source_disagreement", "field": field}
        if len(options) > 1:
            for option in options:
                option["sources"].sort(key=lambda source: not primary_source(source.get("url")))
            issue["options"] = options
            if field not in primary_fields and (unresolved_variants or ambiguous_identity):
                issue["requires_product_choice"] = True
        issues.append(issue)
    proposal["suggestions"] = [
        suggestions[field]
        for field in sorted(suggestions, key=lambda field: field not in primary_fields)
        if field not in conflict
    ]
    if not proposal["suggestions"]:
        issues.append({"code": "no_verified_values" if all_matches or evidence or authoritative_docs else "no_match"})
    if not all_matches or unresolved_variants or ambiguous_identity:
        for source in ("FilaManDB", "Open Filament Database"):
            issues.extend(possible_matches.get(source, []))
        titles = {url: page.title for url, page in pages.items() if page}
        for url in dict.fromkeys(web_urls):
            if link := public_link(url):
                issues.append({
                    "code": "possible_match", "name": (titles.get(url) or urlsplit(link).hostname or link)[:255],
                    "source": "Web search", "url": link, "product_url": link,
                })
    return proposal


async def batch_proposal(
    db: DBSession, snapshot: dict, mode: str, *, sku_hint="", product_url=None,
    guidance="", release_read=True, skip_complete=False, model_id: str | None = None, deadline: float | None = None,
) -> dict:
    if mode == "standardize":
        return await _local_conventions(db, snapshot)
    result = {"proposals": [], "issues": []}
    if not any(is_missing(field, snapshot.get(field)) for field in MODE_FIELDS["fill"]) and (
        skip_complete or not is_missing("manufacturer_url", snapshot.get("manufacturer_url"))
    ):
        return result
    identity = snapshot_identity(snapshot)
    if _norm(identity.sku) and _norm(sku_hint) and _norm(identity.sku) != _norm(sku_hint):
        return {**result, "issues": [{"code": "needs_match", "field": "manufacturer_sku"}]}
    raw = await research_products(
        db, identity, product_url=product_url, sku_hint=sku_hint, snapshot=snapshot,
        guidance=guidance, release_read=release_read, model_id=model_id, deadline=deadline,
    )
    for suggestion in raw["suggestions"]:
        field = suggestion["field"]
        value = suggestion["value"]
        try:
            FilamentUpdate.model_validate({field: value})
        except ValidationError:
            continue
        result["proposals"].append({
            "id": field,
            "fields": {field: {"old": snapshot.get(field), "value": value}},
            "sources": [
                {
                    "url": source["url"],
                    "title": source["title"],
                    "quote": source["evidence"][:400],
                }
                for source in suggestion["sources"]
            ],
            "reason": "Fills a blank using evidence for the matching product and variant.",
            "group_key": None,
        })
    for issue in raw["issues"]:
        if issue["code"] == "no_match":
            issue = {**issue, "code": "needs_match"}
        result["issues"].append(issue)
    if not result["proposals"] and any(
        issue["code"] == "ambiguous_match" for issue in result["issues"]
    ) and not any(issue["code"] in ("needs_match", "no_verified_values") for issue in result["issues"]):
        result["issues"].append({"code": "needs_match"})
    return result
