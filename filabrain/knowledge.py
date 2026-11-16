"""Restricted, local retrieval over the release's catalog and printing references."""

from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import quote

DATA_DIR = Path(__file__).with_name("knowledge_data")
MAKER_ALIASES = {"bambu": "bambulab", "prusapolymers": "prusament"}


def _words(value):
    decomposed = unicodedata.normalize("NFKD", str(value or "")).casefold()
    unaccented = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(re.findall(r"[^\W_]+", unaccented))


def _maker_key(value):
    key = _words(value).replace(" ", "")
    return MAKER_ALIASES.get(key, key)


def _without_prefix(value, prefix):
    return value.removeprefix(prefix + " ") if prefix else value


def _includes(value, words):
    return bool(words) and f" {words} " in f" {value} "


@lru_cache(maxsize=40)
def _data(path):
    return json.loads((DATA_DIR / path).read_text(encoding="utf-8"))


def _source_url(source, target, path):
    return f"{source['repo']}/{target}/{source['revision']}/{quote(path)}"


def _catalog_row(row):
    source_key = row["source_id"].partition(":")[0]
    source = _data("sources.json")[source_key]
    item = {key: value for key, value in row.items() if key not in {"source_path", "manufacturer_product"}}
    if row.get("manufacturer_product"):
        item["manufacturer_url"] = row["product_url"]
    item.update(
        kind="catalog",
        title=f"{source['title']} · {row['maker']} {row['product']}",
        url=_source_url(source, "blob", row["source_path"]) if source_key == "ofd"
        else _source_url(source, "tree", "filaments"),
        source=source["title"],
        revision=source["revision"],
        revision_date=source["revision_date"],
        package_verified=False,
    )
    details = [f"{row['maker']} {row['product']}", row.get("variant"), row.get("material")]
    for field, unit in (("diameter_mm", "mm"), ("net_weight_g", "g net"), ("spool_weight_g", "g empty spool")):
        if row.get(field) is not None:
            details.append(f"{row[field]:g} {unit}")
    details.append(row.get("packaging"))
    note = (
        "SpoolmanDB can generate unsold combinations. Confirm the actual package before using its tare."
        if source_key == "spoolman"
        else "Bundled catalog snapshot. Confirm the selected package and current supplier listing."
    )
    item["text"] = "; ".join(filter(None, details)) + ". " + note
    return item


def _catalog_rows(maker_key):
    if maker_key not in _data("makers.json"):
        return ()
    return [
        row
        for shard in sorted((DATA_DIR / "catalog").glob(f"{maker_key}.*.json"))
        for row in _data(f"catalog/{shard.name}")
    ]


def lookup_catalog(maker: str, product: str, *, limit: int = 5) -> list[dict]:
    """Find candidates by known maker and product, variant, SKU, or barcode."""
    maker_key = _maker_key(maker)
    aliases = _data("makers.json").get(maker_key, []) + [maker]
    aliases += [alias for alias, key in MAKER_ALIASES.items() if key == maker_key]
    grams = None
    net = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(kg|g)\b", product, flags=re.IGNORECASE)
    if net:
        grams = float(net[1]) * (1000 if net[2].lower() == "kg" else 1)
        product = f"{product[:net.start()]} {product[net.end():]}"
    product_text = _words(product)
    for alias in sorted(map(_words, aliases), key=len, reverse=True):
        product_text = _without_prefix(product_text, alias)
    package = None
    for name, phrase in (
        ("refill", "refill"), ("refill", "spoolless"), ("refill", "without a spool"), ("refill", "without spool"),
        ("spool", "with spool"), ("spool", "with a spool"), ("spool", "on spool"), ("spool", "spooled"),
    ):
        if _includes(product_text, phrase):
            package = name
            product_text = f" {product_text} ".replace(f" {phrase} ", " ").strip()
            break
    if not maker_key or not product_text or limit <= 0:
        return []

    ranked = []
    for row in _catalog_rows(maker_key):
        if ((package and row.get("packaging") != package)
                or (grams is not None and row.get("net_weight_g") != grams)):
            continue
        name = _words(row["product"])
        variant = _words(row.get("variant"))
        full = " ".join(filter(None, (name, variant)))
        material = _words(row["material"])
        aliases = (
            full, f"{material} {full}", f"{full} {material}",
            _words(row.get("manufacturer_sku")), _words(row.get("barcode")),
        )
        if product_text in aliases:
            score = 0
        elif product_text == name:
            score = 1
        elif any(_includes(value, product_text) for value in aliases[:3]):
            score = 2
        else:
            continue
        ranked.append(((score, row["source_id"].partition(":")[0] != "ofd"), row))
    return [_catalog_row(row) for _, row in sorted(ranked, key=lambda entry: entry[0])[:limit]]


def lookup_named_catalog(query: str, *, limit: int = 2) -> list[dict]:
    """Resolve public catalog terms from the question without returning question text."""
    normalized = _words(query)
    makers = [
        (len(name), key, name)
        for key, aliases in _data("makers.json").items()
        for alias in aliases
        if (name := _words(alias)) and _includes(normalized, name)
    ]
    makers.extend((len(alias), key, alias) for alias, key in MAKER_ALIASES.items() if _includes(normalized, alias))
    for _, key, alias in sorted(makers, reverse=True):
        candidates = []
        for row in _catalog_rows(key):
            name = _words(row["product"])
            if name == alias or not _includes(normalized, name):
                continue
            variant = row.get("variant")
            label = row["product"]
            if variant and _includes(normalized, _words(variant)):
                label += " " + variant
            candidates.append((len(_words(label)), row["source_id"].startswith("ofd:"), label))
        if candidates:
            product = max(candidates)[2]
            if re.search(r"\b(?:refill|spoolless|without (?:a )?spool)\b", normalized):
                product += " refill"
            elif re.search(r"\b(?:with (?:a )?spool|on spool|spooled)\b", normalized):
                product += " with spool"
            if weight := re.search(
                r"(?<![\d.])(\d+(?:\.\d+)?\s*(?:kg|g))\s+(?:refill|spool|package)\b",
                query, flags=re.IGNORECASE,
            ):
                product += " " + weight[1]
            return lookup_catalog(_data("makers.json")[key][0], product, limit=limit)
    return []


def _printer(value):
    return _without_prefix(_without_prefix(_words(value), "bambu lab"), "bambu")


def _nozzle_mm(value):
    match = re.fullmatch(
        r"(?:nozzle\s+)?(\d+(?:\.\d+)?)\s*(?:mm)?(?:\s+(?:stainless(?:\s+steel)?|hardened(?:\s+steel)?|nozzle))*",
        str(value or "").strip().casefold(),
    )
    return float(match[1]) if match else None


def _public_preset(row, printer, nozzle):
    result = {key: value for key, value in row.items() if key not in {"source_path", "compatible"}}
    result.update(kind="preset", printer=printer, nozzle_mm=nozzle)
    if row["source_id"].startswith("polymaker:"):
        source = _data("sources.json")["polymaker"]
        result.update(
            title=f"Polymaker · {row['product']} · {printer} · {nozzle:g} mm",
            url=_source_url(source, "blob", row["source_path"]),
            revision=source["revision"],
            revision_date=source["revision_date"],
            text="Manufacturer slicer preset for the selected product, printer and nozzle diameter. Check the selected build plate and physical feed path.",
        )
    return result


def lookup_presets(maker: str, product: str, *, printer: str, nozzle: str,
                   slicer: str = "Bambu Studio", limit: int = 5) -> list[dict]:
    """Return only manufacturer presets listing this exact printer and nozzle."""
    printer_name = _printer(printer)
    nozzle_mm = _nozzle_mm(nozzle)
    maker_key = _maker_key(maker)
    if not printer_name or not nozzle_mm or not maker_key or limit <= 0:
        return []
    product_name = _words(product)
    for prefix in ("bambu lab", "bambu") if maker_key == "bambulab" else (_words(maker),):
        product_name = _without_prefix(product_name, prefix)
    files = {"bambulab": "bambu_presets.json", "polymaker": "polymaker.json"}
    rows = _data(files[maker_key]) if maker_key in files else ()
    candidates = {}
    for row in rows:
        if product_name != _words(row["product"]) or _maker_key(slicer) != _maker_key(row["slicer"]):
            continue
        for compatibility in row["compatible"]:
            match = re.fullmatch(r"(.+) (\d+(?:\.\d+)?) nozzle", compatibility)
            if match and _printer(match[1]) == printer_name and float(match[2]) == nozzle_mm:
                # Polymaker may cross-list both the A1 and A1 mini. Identical values are one option.
                key = json.dumps(row["settings"], sort_keys=True)
                candidates.setdefault(key, _public_preset(row, match[1], nozzle_mm))
    return list(candidates.values())[:limit]


def lookup_published_specs(maker: str, product: str, material: str) -> list[dict]:
    """Return checked manufacturer facts for the exact grade, independent of the spool package."""
    product_key = _words(product)
    maker_key = _maker_key(maker)
    data = _data("publisher_specs.json")
    return [
        {**row, "reviewed": data["reviewed"]}
        for row in data["records"]
        if _maker_key(row["maker"]) == maker_key and _words(row["product"]) == product_key
        and _words(row["material"]) == _words(material)
    ]


def lookup_guidance(query: str, *, material: str = "", printer: str = "", feeder: str = "",
                    nozzle: str = "", maker: str = "", product: str = "", limit: int = 6) -> list[dict]:
    """Return a short list of relevant notes with their source and scope."""
    if limit <= 0:
        return []
    prompt = _words(f"{query} {material} {printer} {feeder} {nozzle} {maker} {product}")
    ranked = []
    for row in _data("guidance.json"):
        exact = row.get("exact", [])
        if exact and not any(_includes(prompt, _words(value)) for value in exact):
            continue
        if any(not _includes(prompt, _words(required)) for required in row.get("requires", [])):
            continue
        exclude = row.get("exclude", [])
        if any(_includes(prompt, _words(value)) for value in exclude):
            continue
        primary = sum(_includes(prompt, _words(value)) for value in row.get("keywords", []))
        secondary = sum(_includes(prompt, _words(value)) for value in row.get("topics", []))
        if exact or primary or secondary:
            ranked.append((
                6 * bool(exact) + 3 * bool(primary) + bool(secondary),
                {key: value for key, value in row.items() if key not in {
                    "exact", "keywords", "topics", "requires", "exclude",
                }},
            ))
    return [row for _, row in sorted(ranked, reverse=True, key=lambda pair: pair[0])[:limit]]
