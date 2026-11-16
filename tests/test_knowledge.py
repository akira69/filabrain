from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from filabrain import knowledge


def test_catalog_keeps_bambu_package_choices_and_source_ids():
    records = knowledge.lookup_catalog("Bambu Lab", "PETG Matte Avocado Green", limit=20)
    ofd = [entry for entry in records if entry["source_id"].startswith("ofd:")]
    assert {entry.get("packaging") for entry in ofd} >= {"refill", "spool"}
    assert len({entry["source_id"] for entry in ofd}) == len(ofd)
    assert all(entry["maker"] == "Bambu Lab" and entry["color"] == "Avocado Green" for entry in ofd)
    assert all(entry["product_url"].startswith("https://") for entry in ofd)
    assert knowledge.lookup_catalog("Bambu Lab", "not a real grade") == []
    assert {entry.get("packaging") for entry in knowledge.lookup_catalog(
        "Bambu Lab", "PETG Matte Avocado Green 1 kg refill",
    )} == {"refill"}
    jade = knowledge.lookup_catalog("Bambu Lab", "PLA Basic Jade White")[0]
    assert jade.get("packaging") is None
    assert jade["manufacturer_url"].startswith("https://us.store.bambulab.com/")


def test_named_catalog_resolves_public_grade_and_color_without_question_text():
    matches = knowledge.lookup_named_catalog("Is my Bambu Lab PLA Basic Jade White from my locked desk any good?")
    assert matches[0]["product"] == "PLA Basic"
    assert matches[0]["color"] == "Jade White"
    assert "locked desk" not in str(matches)


def test_catalog_spoolman_weights_stay_package_candidates():
    records = knowledge.lookup_catalog("3D Printing Canada", "Budget Dark Green")
    result = next(entry for entry in records if entry["source_id"].startswith("spoolman:"))
    assert result["net_weight_g"] == 1000
    assert result["spool_weight_g"] == 230
    assert result["package_verified"] is False


def test_exact_presets_require_matching_printer_nozzle_product_and_slicer():
    args = {"printer": "Bambu Lab A1", "nozzle": "0.4 mm"}
    result, = knowledge.lookup_presets("Polymaker", "Panchroma CoPE", **args)
    assert result["settings"]["nozzle_temperature_c"] == 230
    assert result["settings"]["bed_temperature_c"]["Textured PEI Plate"] == 60
    assert result["nozzle_mm"] == 0.4
    assert knowledge.lookup_presets("Polymaker", "Panchroma CoPE", printer="A1", nozzle="") == []
    assert knowledge.lookup_presets("Polymaker", "Panchroma CoPE", printer="A1", nozzle="0.8 mm") == []
    assert knowledge.lookup_presets("Polymaker", "Panchroma CoPE", **args, slicer="OrcaSlicer") == []
    assert knowledge.lookup_presets("Polymaker", "CoPE", **args) == []
    assert knowledge.lookup_presets("Unrelated maker", "Panchroma CoPE", **args) == []
    official, = knowledge.lookup_presets("Bambu Lab", "PETG HF", **args)
    assert official["settings"]["nozzle_temperature_c"] == 240
    assert official["settings"]["first_layer_nozzle_temperature_c"] == 230
    assert "Cool Plate" not in official["settings"]["bed_temperature_c"]


def test_compatibility_distinguishes_stock_nozzle_and_exact_feeder_grade():
    filled = knowledge.lookup_guidance("A1 with stock nozzle and filled filament")
    assert any(entry["source_id"] == "hardware:bambu-a1" for entry in filled)
    assert any(entry["source_id"] == "material:filled" for entry in filled)
    assert all(entry.get("product") != "PLA-CF" for entry in filled)

    generic = knowledge.lookup_guidance("Does generic TPU work?", feeder="AMS 2 Pro")
    assert any(entry["source_id"] == "compatibility:ams2-pro-tpu" for entry in generic)
    assert all(entry.get("product") != "TPU for AMS" for entry in generic)

    exact = knowledge.lookup_guidance("Bambu TPU for AMS on an AMS 2 Pro")
    assert any(entry.get("product") == "TPU for AMS" for entry in exact)
    assert all(entry.get("product") != "TPU 95A HF" for entry in exact)


def test_outdoor_evidence_and_mismatched_test_conditions_are_distinct():
    records = knowledge.lookup_guidance("outdoor bracket with heat, sun and flex", limit=3)
    ids = {entry["source_id"] for entry in records}
    assert {"material:asa", "material:petg", "material:tpu"} <= ids
    assert all("measurements" not in entry for entry in records)
    records = knowledge.lookup_guidance(
        "compare impact of Prusament PETG with Polymaker Fiberon PA6-CF20",
    )
    measured = {entry["source_id"]: entry for entry in records if entry["kind"] == "measurement"}
    assert {"measurement:prusament-petg", "measurement:fiberon-pa6-cf20"} <= measured.keys()
    prusa = measured["measurement:prusament-petg"]["measurements"]
    fiberon = measured["measurement:fiberon-pa6-cf20"]["measurements"]
    assert {entry.get("notched") for entry in prusa if "notched" in entry} == {True, False}
    assert all("annealed" in entry["conditions"].lower() for entry in fiberon)
    assert any(entry["source_id"] == "guidance:comparing-materials" for entry in records)


def test_bambu_published_measurements_keep_load_and_sample_preparation():
    entries = knowledge.lookup_guidance("How heat resistant is Bambu PETG HF?")
    result = next(entry for entry in entries if entry["source_id"] == "measurement:bambu-petg-hf")
    heat_deflection = [entry for entry in result["measurements"] if entry["property"] == "heat_deflection"]
    assert {(entry["value"], entry["load_mpa"]) for entry in heat_deflection} == {(62, 1.8), (69, 0.45)}
    assert all("annealed" in entry["conditions"] and entry["specimen"] == "printed" for entry in heat_deflection)


def test_bundle_fits_the_installer_json_and_archive_limits(tmp_path):
    from scripts.package_plugin import PACKAGE

    archive = tmp_path / "filabrain.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        for path in PACKAGE.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                bundle.write(path, path.relative_to(PACKAGE.parent))
    with ZipFile(archive) as bundle:
        snapshots = [item for item in bundle.infolist() if "knowledge_data/" in item.filename]
        assert snapshots
        assert all(item.file_size < 1024 * 1024 for item in snapshots if item.filename.endswith(".json"))
    assert archive.stat().st_size < 10 * 1024 * 1024
    assert (Path(knowledge.DATA_DIR) / "sources.json").is_file()


def test_named_catalog_keeps_refill_and_package_size():
    from filabrain.knowledge import lookup_named_catalog

    refill = lookup_named_catalog("Tell me about Bambu Lab PETG Matte Avocado Green 1 kg refill")
    with_spool = lookup_named_catalog("Tell me about Bambu Lab PETG Matte Avocado Green with spool")
    assert refill and all(row["packaging"] == "refill" and row["net_weight_g"] == 1000 for row in refill)
    assert with_spool and all(row["packaging"] == "spool" for row in with_spool)
