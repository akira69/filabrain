"""Rebuild bundled data for a release with pinned upstream inputs.

Pass a directory containing ofd.tar.gz and polymaker.tar.gz (GitHub archives
at the revisions below), spoolman.json (the compiled Pages export at the
recorded digest), and spoolman-LICENSE (from the pinned commit). All other
knowledge entries are authored and reviewed independently of the generator.
"""

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
import tarfile
from urllib.parse import urlsplit

from sys import path as import_path

import_path.insert(0, str(Path(__file__).resolve().parents[1]))
from filabrain.knowledge import DATA_DIR, _maker_key


SOURCES = {
    "ofd": {
        "title": "Open Filament Database",
        "repo": "https://github.com/OpenFilamentCollective/open-filament-database",
        "url": "https://api.openfilamentdatabase.org/",
        "revision": "60be55d3448c615c4938b949435b1b1c904668b4",
        "revision_date": "2026-11-16",
        "license": "MIT",
    },
    "spoolman": {
        "title": "SpoolmanDB",
        "repo": "https://github.com/Donkie/SpoolmanDB",
        "url": "https://donkie.github.io/SpoolmanDB/filaments.json",
        "revision": "bbdc012ba9270d545f2be0f3e66603640d60d800",
        "revision_date": "2026-11-15",
        "sha256": "379bda12092d47b8edb869f0a1ac085a03c2624f5b5178e4efb1c45d1574d085",
        "license": "MIT",
    },
    "polymaker": {
        "title": "Polymaker Presets",
        "repo": "https://github.com/Polymaker3D/Polymaker-Preset",
        "url": "https://github.com/Polymaker3D/Polymaker-Preset",
        "revision": "25d423c506ad3cfad5a27e12a7f36d9e992454da",
        "revision_date": "2026-11-13",
        "license": "MIT",
    },
}
NOZZLE_FIELDS = {
    "nozzle_temperature_c": "nozzle_temperature",
    "first_layer_nozzle_temperature_c": "nozzle_temperature_initial_layer",
}
PLATE_FIELDS = {
    "Cool Plate": "cool_plate",
    "Engineering Plate": "eng_plate",
    "Smooth PEI Plate / High Temp Plate": "hot_plate",
    "Textured PEI Plate": "textured_plate",
    "Cool Plate SuperTack": "supertack_plate",
}


def open_archive(inputs, source):
    archive = tarfile.open(inputs / f"{source}.tar.gz")
    if archive.pax_headers.get("comment") != SOURCES[source]["revision"]:
        archive.close()
        raise ValueError(f"Unexpected {source} source revision")
    return archive


def archive_root(archive):
    return archive.getmembers()[0].name.rstrip("/")


def compact(value):
    return {key: field for key, field in value.items() if field is not None and field != ""}


def web_url(value):
    if isinstance(value, str) and len(value) < 500 and not any(c.isspace() or ord(c) < 32 or c == "\\" for c in value):
        try:
            parsed = urlsplit(value)
            if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password:
                return value
        except ValueError:
            pass
    return None


def ofd_rows(archive):
    root = archive_root(archive)
    records = {}
    for item in archive:
        path = item.name.removeprefix(root + "/")
        if item.isfile() and path.startswith("data/") and path.rsplit("/", 1)[-1] in {
            "brand.json", "material.json", "filament.json", "variant.json", "sizes.json",
        }:
            records[path] = json.load(archive.extractfile(item))
    for path, sizes in sorted(records.items()):
        parts = path.split("/")
        if len(parts) != 6 or parts[-1] != "sizes.json":
            continue
        brand = records["/".join(parts[:2] + ["brand.json"])]
        material = records["/".join(parts[:3] + ["material.json"])]
        filament = records["/".join(parts[:4] + ["filament.json"])]
        variant = records["/".join(parts[:5] + ["variant.json"])]
        site = web_url(brand.get("website"))
        domain = urlsplit(site).hostname.removeprefix("www.") if site else ""
        for size in sizes:
            links = [
                link for entry in size.get("purchase_links") or [] if (link := web_url(entry.get("url")))
            ]
            official = next((
                link for link in links if domain and (
                    (host := urlsplit(link).hostname) == domain or host.endswith("." + domain)
                )
            ), None)
            package = "refill" if size.get("spool_refill") is True else "spool" if size.get("spool_refill") is False else None
            yield compact({
                "source_id": f"ofd:{size['uuid']}",
                "source_path": path,
                "maker": brand["name"],
                "product": filament["name"],
                "material": material["material"],
                "product_id": filament["uuid"],
                "variant_id": variant["uuid"],
                "variant": variant.get("name"),
                "color": variant.get("name") if variant.get("color_hex") or variant.get("color_hexes") else None,
                "color_hex": variant.get("color_hex"),
                "color_hexes": variant.get("color_hexes"),
                "diameter_mm": size.get("diameter"),
                "net_weight_g": size.get("filament_weight"),
                "spool_weight_g": size.get("spool_weight"),
                "packaging": package,
                "package_material": size.get("container_material"),
                "manufacturer_sku": size.get("article_number"),
                "barcode": size.get("barcode_identifier"),
                "product_url": official or (links[0] if links else None),
                "manufacturer_product": True if official else None,
                "density_g_cm3": variant.get("density", filament.get("density")),
                "datasheet_url": web_url(variant.get("data_sheet_url") or filament.get("data_sheet_url")),
                **{
                    name: variant.get(field, filament.get(field))
                    for name, field in (
                        ("nozzle_temperature_min_c", "min_print_temperature"),
                        ("nozzle_temperature_max_c", "max_print_temperature"),
                        ("bed_temperature_min_c", "min_bed_temperature"),
                        ("bed_temperature_max_c", "max_bed_temperature"),
                        ("drying_temperature_c", "dry_temperature"),
                        ("drying_hours", "dry_time"),
                    )
                },
            })


def spoolman_rows(data):
    for row in data:
        yield compact({
            "source_id": f"spoolman:{row['id']}",
            "maker": row["manufacturer"],
            "product": row["name"],
            "material": row["material"],
            "diameter_mm": row.get("diameter"),
            "net_weight_g": row.get("weight"),
            "spool_weight_g": row.get("spool_weight"),
            "package_material": row.get("spool_type"),
            "color_hex": row.get("color_hex"),
            "color_hexes": row.get("color_hexes"),
            "density_g_cm3": row.get("density"),
            "nozzle_temperature_c": row.get("extruder_temp"),
            "bed_temperature_c": row.get("bed_temp"),
            "nozzle_temperature_min_c": (row.get("extruder_temp_range") or [None, None])[0],
            "nozzle_temperature_max_c": (row.get("extruder_temp_range") or [None, None])[-1],
            "bed_temperature_min_c": (row.get("bed_temp_range") or [None, None])[0],
            "bed_temperature_max_c": (row.get("bed_temp_range") or [None, None])[-1],
        })


def temperature(profile, field):
    values = profile.get(field)
    if isinstance(values, list) and len(values) == 1 and str(values[0]).isdigit():
        value = int(values[0])
        return value if 0 < value <= 350 else None
    return None


def profile_settings(profile):
    settings = {name: temperature(profile, field) for name, field in NOZZLE_FIELDS.items()}
    for name, suffix in (("bed_temperature_c", ""), ("first_layer_bed_temperature_c", "_initial_layer")):
        settings[name] = {
            plate: value
            for plate, prefix in PLATE_FIELDS.items()
            if (value := temperature(profile, prefix + "_temp" + suffix)) is not None
        } or None
    return compact(settings)


def polymaker_rows(archive):
    root = archive_root(archive)
    index = json.load(archive.extractfile(f"{root}/index.json"))
    for item in index["presets"]:
        if item["slicer"] != "BambuStudio" or item["brand"] != "BBL":
            continue
        profile = json.load(archive.extractfile(f"{root}/{item['path']}"))
        if not (settings := profile_settings(profile)):
            continue
        compatible = set(profile.get("compatible_printers") or [])
        if not compatible:
            continue
        yield {
            "source_id": f"polymaker:{item['path']}",
            "source_path": item["path"],
            "maker": "Polymaker",
            "product": item["material"],
            "slicer": "Bambu Studio",
            "compatible": sorted(compatible),
            "settings": settings,
            "profile_version": profile.get("version") or "",
        }


def write_json(path, rows):
    if isinstance(rows, list):
        lines = [json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) for row in rows]
        output = ("[\n" + ",\n".join(lines) + "\n]\n").encode("utf-8")
    else:
        output = (json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(output) >= 1024 * 1024:
        raise ValueError(f"{path.name} is over the FilaMan JSON file limit")
    path.write_bytes(output)


def build(inputs, output):
    spoolman = (inputs / "spoolman.json").read_bytes()
    if sha256(spoolman).hexdigest() != SOURCES["spoolman"]["sha256"]:
        raise ValueError("Unexpected SpoolmanDB export digest")
    spoolman_license = (inputs / "spoolman-LICENSE").read_text(encoding="utf-8")
    if sha256(spoolman_license.encode()).hexdigest() != "0e32583112190cdace7fcba056c3ccadc1e4952b6fa0f6210c8e6faa8048411e":
        raise ValueError("Unexpected SpoolmanDB license")
    license_texts = {"spoolman": spoolman_license}
    catalog = defaultdict(list)
    with open_archive(inputs, "ofd") as ofd, open_archive(inputs, "polymaker") as polymaker:
        for name, archive in (("ofd", ofd), ("polymaker", polymaker)):
            license_texts[name] = archive.extractfile(f"{archive_root(archive)}/LICENSE").read().decode("utf-8")
        for row in ofd_rows(ofd):
            catalog[_maker_key(row["maker"])].append(row)
        for row in spoolman_rows(json.loads(spoolman)):
            catalog[_maker_key(row["maker"])].append(row)
        presets = sorted(polymaker_rows(polymaker), key=lambda row: row["source_id"])

    (output / "catalog").mkdir(parents=True, exist_ok=True)
    written = set()
    for key, rows in sorted(catalog.items()):
        assert key
        chunks = [[]]
        byte_count = 4
        for row in rows:
            byte_size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 2
            if byte_count + byte_size >= 1024 * 1024:
                chunks.append([])
                byte_count = 4
            chunks[-1].append(row)
            byte_count += byte_size
        for index, chunk in enumerate(chunks):
            path = output / "catalog" / f"{key}.{index}.json"
            write_json(path, chunk)
            written.add(path)
    for stale in set((output / "catalog").glob("*.json")) - written:
        stale.unlink()
    write_json(output / "makers.json", {key: sorted({row["maker"] for row in rows}) for key, rows in catalog.items()})
    write_json(output / "polymaker.json", presets)
    write_json(output / "sources.json", SOURCES)
    (output / "THIRD_PARTY_LICENSES.txt").write_text(
        "\n\n".join(f"{SOURCES[key]['title']}\n{SOURCES[key]['repo']}\n\n{license_texts[key].strip()}" for key in SOURCES) + "\n",
        encoding="utf-8",
    )
    print(f"{sum(map(len, catalog.values()))} catalog records across {len(catalog)} makers, {len(presets)} Polymaker presets")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("--output", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    build(args.inputs, args.output)
