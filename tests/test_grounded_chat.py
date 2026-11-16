import pytest
import pytest_asyncio
from app.core.security import Principal
from app.models import (
    Filament, FilamentPrinterParam, FilamentPrinterProfile, InstalledPlugin, Manufacturer, Printer, Spool, SpoolStatus,
)
from sqlalchemy import select

from filabrain.settings import SettingsInput

pytest_plugins = ("tests.conftest",)
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def chat_db(db_session, monkeypatch):
    from filabrain import research

    db_session.add(InstalledPlugin(
        plugin_key="filabrain", name="FilaBrain", version="1.0",
        plugin_type="integration", is_active=True,
    ))
    await db_session.commit()
    config = SettingsInput(model_url="http://model.test", model_id="test-model").model_dump()

    async def config_record(_db):
        return None, config

    async def check_model_catalog(*_args):
        pass

    monkeypatch.setattr(research, "config_record", config_record)
    monkeypatch.setattr(research, "check_model_catalog", check_model_catalog)
    return db_session, config, research


async def ask(chat_db, monkeypatch, prompt, answer="Consider UV resistance.", *, scopes=None, previous=None, **options):
    db, _, research = chat_db

    async def complete(_url, _model, _key, messages, **_kwargs):
        return answer(messages) if callable(answer) else answer

    monkeypatch.setattr(research, "request_completion", complete)
    principal = Principal(auth_type="session", user_id=1, is_superadmin=True, scopes=scopes)
    messages = [] if previous is None else [
        {"role": "user", "content": previous}, {"role": "assistant", "content": "Here are the available spools."},
    ]
    data = research.ChatRequest(messages=[*messages, {"role": "user", "content": prompt}], **options)
    return await research.research_chat(data, db, principal)


async def test_inventory_keeps_unknown_weight_separate_from_eligible_spools(chat_db, monkeypatch):
    db, _, _ = chat_db
    statuses = {row.key: row.id for row in await db.scalars(select(SpoolStatus))}
    maker = Manufacturer(name="Bambu Lab")
    filament = Filament(
        manufacturer=maker, designation="PETG HF", material_type="PETG", diameter_mm=1.75,
    )
    db.add_all([
        Spool(id=27, filament=filament, status_id=statuses["opened"], remaining_weight_g=120),
        Spool(id=28, filament=filament, status_id=statuses["new"], remaining_weight_g=None),
        Spool(id=29, filament=filament, status_id=statuses["active"], remaining_weight_g=45),
        Spool(id=30, filament=filament, status_id=statuses["archived"], remaining_weight_g=1000),
        Spool(id=31, filament=filament, status_id=statuses["empty"], remaining_weight_g=10),
    ])
    await db.commit()
    def answer(messages):
        context = "\n".join(row["content"] for row in messages if row["role"] == "system")
        assert "spool-27" in context and "spool-30" not in context and "spool-31" not in context
        return "Consider PETG for this job [spool-27]."

    result = await ask(
        chat_db, monkeypatch, "Which spools could I use outdoors? I have 20 g for tests and need 0.12 kg for the bracket.",
        answer,
    )
    assert result["reply"] == "Consider PETG for this job [spool-27]."
    assert result["required_g"] == 120
    facts = {row["id"]: row for row in result["evidence"] if row["kind"] == "spool"}
    assert set(facts) == {"spool-27", "spool-28", "spool-29"}
    assert facts["spool-27"]["url"] == "/spools/27"
    assert facts["spool-27"]["remaining_g"] == 120
    assert facts["spool-27"]["meets_required"] is True
    assert facts["spool-28"]["remaining_g"] is None
    assert facts["spool-28"]["meets_required"] is None
    assert facts["spool-29"]["meets_required"] is False

    follow_up = await ask(
        chat_db, monkeypatch, "I need 1,200 g.", previous="Which spools could I use outdoors?",
    )
    assert follow_up["required_g"] == 1200
    assert all(row["meets_required"] is not True for row in follow_up["evidence"] if row["kind"] == "spool")
    grouped_twice = await ask(chat_db, monkeypatch, "I need 0,001,100 g.", previous="Which spools could I use outdoors?")
    assert grouped_twice["required_g"] == 1100

    denied = await ask(chat_db, monkeypatch, "What do I have for outdoors?", scopes=["filaments:read"])
    assert "spools_forbidden" in denied["notices"]
    assert all(row["kind"] != "spool" for row in denied["evidence"])


@pytest.mark.parametrize("reply", [
    "Use 999 °C [saved-profile-2].",
    "Use the matching profile [source-made-up].",
])
async def test_settings_use_only_the_selected_nozzle_profile_and_strip_unverified_numbers(chat_db, monkeypatch, reply):
    db, _, _ = chat_db
    filament = Filament(
        manufacturer=Manufacturer(name="Other Brand"), designation="Fixture PLA",
        material_type="PLA", diameter_mm=1.75,
    )
    printer = Printer(name="My Bambu A1", driver_key="manual", driver_config={"token": "private-token"})
    db.add_all([
        FilamentPrinterProfile(
            filament=filament, printer=printer, profile_name="Smaller nozzle",
            nozzle_diameter_mm=0.2, nozzle_temp_c=199,
        ),
        FilamentPrinterProfile(
            filament=filament, printer=printer, profile_name="Matched nozzle",
            nozzle_diameter_mm=0.4, nozzle_temp_c=210,
        ),
    ])
    await db.commit()
    filament_id, printer_id = filament.id, printer.id

    def answer(messages):
        context = "\n".join(row["content"] for row in messages if row["role"] == "system")
        assert "199" not in context and "private-token" not in context
        return reply

    missing = await ask(chat_db, monkeypatch, "What settings should I start with?")
    assert {"printer_required", "filament_required"} <= set(missing["notices"])
    result = await ask(
        chat_db, monkeypatch, "What settings should I start with?", answer,
        filament_id=filament_id, printer_id=printer_id, nozzle_mm=0.4,
    )
    saved = [row for row in result["evidence"] if row["kind"] == "saved_profile"]
    assert [row["details"]["nozzle_temp_c"] for row in saved] == [210]
    assert "999" not in result["reply"]
    assert "source-made-up" not in result["reply"]
    assert "missing_evidence" in result["notices"]


async def test_forced_web_lookup_uses_only_public_terms_and_keeps_local_facts_on_timeout(chat_db, monkeypatch):
    from filabrain import sources

    _, config, _ = chat_db
    config["search_url"] = "http://search.test"
    queries = []

    async def timeout(origin, query):
        queries.append((origin, query))
        raise TimeoutError

    monkeypatch.setattr(sources, "search_web", timeout)
    result = await ask(
        chat_db, monkeypatch, "What settings for my Bambu Lab A1? Private order number 182833.",
        web_search=True,
    )
    assert len(queries) == 1
    assert "A1" in queries[0][1]
    assert "182833" not in queries[0][1] and "Private order" not in queries[0][1]
    assert result["search"] == "failed"
    assert any(row["kind"] == "guidance" for row in result["evidence"])


async def test_live_printer_lookup_ignores_other_domains_and_nearby_printer_names(chat_db, monkeypatch):
    from filabrain import sources

    _, config, _ = chat_db
    config["search_url"] = "http://search.test"
    untrusted = "https://search-result.example/bambu-a1"
    wrong = "https://wiki.bambulab.com/en/a1-mini/settings"
    matching = "https://wiki.bambulab.com/en/a1/settings"
    fetched = []

    async def search(*_args):
        return [untrusted, wrong, matching]

    async def fetch(url, **_kwargs):
        fetched.append(url)
        title = "Bambu A1 Mini print settings" if url == wrong else "Bambu A1 print settings"
        return sources.Evidence(url, title, "A supplier excerpt about settings.")

    monkeypatch.setattr(sources, "search_web", search)
    monkeypatch.setattr(sources, "fetch_supplier", fetch)
    result = await ask(chat_db, monkeypatch, "What are the latest Bambu Lab A1 print settings?")
    assert fetched == [wrong, matching]
    assert result["search"] == "complete"
    assert [row["url"] for row in result["evidence"] if row["kind"] == "live"] == [matching]
    assert all(row["kind"] != "preset" for row in result["evidence"])


async def test_generic_chat_retains_its_existing_response_contract(chat_db, monkeypatch):
    result = await ask(chat_db, monkeypatch, "Open the filaments page.", "Here you go.\n[[open:filaments]]")
    assert result == {"reply": "Here you go.", "action": {"page": "filaments", "url": "/filaments"}}


async def test_ordinary_material_question_uses_offline_comparisons(chat_db, monkeypatch):
    result = await ask(chat_db, monkeypatch, "Which materials hold up to outdoor heat, UV, and flex?")
    assert result["search"] is None
    assert any("ASA" in row["title"] for row in result["evidence"])
    assert any("TPU" in row["title"] for row in result["evidence"])


async def test_material_measurement_keeps_preparation_conditions_and_secondary_links(chat_db, monkeypatch):
    result = await ask(chat_db, monkeypatch, "How heat resistant is Bambu PETG HF?")
    data = next(row for row in result["evidence"] if row["id"] == "measurement:bambu-petg-hf")
    assert any("then annealed and dried" in condition for condition in data["details"]["test_conditions"])
    assert data["links"][0]["title"] == "Bambu Filament Guide"
    assert data["links"][0]["url"].startswith("https://cdn1.bambulab.com/filament/")


async def test_named_catalog_question_reads_offline_without_replacing_navigation(chat_db, monkeypatch):
    result = await ask(chat_db, monkeypatch, "Tell me about Bambu Lab PLA Basic Jade White.")
    assert result["search"] is None
    assert any(row["kind"] == "catalog" and "PLA Basic" in row["title"] for row in result["evidence"])

    action = await ask(
        chat_db, monkeypatch, "Open the new filament page for Bambu Lab PLA Basic Jade White.",
        "Here you go.\n[[open:new_filament|Bambu Lab PLA Basic Jade White]]",
    )
    assert action == {
        "reply": "Here you go.",
        "action": {"page": "new_filament", "url": "/filaments/new?designation=Bambu+Lab+PLA+Basic+Jade+White"},
    }


async def test_stock_printer_calibration_is_available_without_an_exact_nozzle_profile(chat_db, monkeypatch):
    db, _, _ = chat_db
    filament = Filament(
        manufacturer=Manufacturer(name="Bambu Lab"), designation="PETG HF",
        material_type="PETG", diameter_mm=1.75,
    )
    printer = Printer(name="Bambu A1", driver_key="manual")
    expected = {
        "bambu_k_value": "0.025", "bambu_nozzle_temp_min": "225",
        "bambu_nozzle_temp_max": "245", "bambu_bed_temp": "60",
    }
    db.add_all([
        FilamentPrinterParam(filament=filament, printer=printer, param_key=key, param_value=value)
        for key, value in {**expected, "other_setting": "hidden"}.items()
    ])
    await db.commit()

    result = await ask(
        chat_db, monkeypatch, "What settings should I start with?",
        filament_id=filament.id, printer_id=printer.id,
    )
    assert "nozzle_required" in result["notices"]
    assert [row["details"] for row in result["evidence"] if row["kind"] == "saved_calibration"] == [expected]
    assert all(row["kind"] not in {"preset", "saved_profile"} for row in result["evidence"])


@pytest.mark.parametrize(("printer_name", "expected"), [("A1", True), ("Custom Voron A1", False)])
async def test_manufacturer_presets_require_a_recognized_printer_model(chat_db, monkeypatch, printer_name, expected):
    db, _, _ = chat_db
    filament = Filament(
        manufacturer=Manufacturer(name="Bambu Lab"), designation="PLA Basic", material_type="PLA", diameter_mm=1.75,
    )
    printer = Printer(name=printer_name, driver_key="manual")
    db.add_all([filament, printer])
    await db.commit()

    result = await ask(
        chat_db, monkeypatch, "What settings should I start with?",
        filament_id=filament.id, printer_id=printer.id, nozzle_mm=0.4,
    )
    assert any(row["kind"] == "preset" for row in result["evidence"]) is expected


async def test_saved_catalog_uses_color_sku_diameter_and_net_weight(chat_db, monkeypatch):
    db, _, _ = chat_db
    filament = Filament(
        manufacturer=Manufacturer(name="Bambu Lab"), designation="PLA Basic", material_type="PLA",
        diameter_mm=1.75, manufacturer_color_name="Jade White", manufacturer_sku="10100", raw_material_weight_g=1000,
    )
    db.add(filament)
    await db.commit()
    result = await ask(chat_db, monkeypatch, "Tell me about this filament.", filament_id=filament.id)
    catalog = [row for row in result["evidence"] if row["kind"] == "catalog"]
    assert catalog
    assert {(row["details"]["color"], row["details"]["diameter_mm"], row["details"]["net_weight_g"]) for row in catalog} == {
        ("Jade White", 1.75, 1000),
    }


@pytest.mark.parametrize("verified", [True, False])
async def test_live_product_preserves_a_verified_variant_selector_and_labels_grade_only_sources(chat_db, monkeypatch, verified):
    from filabrain import knowledge, sources

    _, config, _ = chat_db
    config["search_url"] = "http://search.test"
    base = "https://store.bambulab.com/products/pla-basic"
    manufacturer_url = f"{base}?variant=123456&customer=private"
    published_url = f"{base}?variant=123456" if verified else base
    monkeypatch.setattr(knowledge, "lookup_named_catalog", lambda *_args, **_kwargs: [{
        "source_id": "fixture-pla", "title": "PLA Basic", "kind": "catalog", "text": "Bundled fixture.",
        "url": "https://catalog.example/pla", "maker": "Bambu Lab", "product": "PLA Basic", "material": "PLA",
        "manufacturer_url": manufacturer_url, "product_url": manufacturer_url, "color": "Jade White",
        "variant": "Jade White", "diameter_mm": 1.75, "net_weight_g": 1000, "manufacturer_sku": "10100",
        "packaging": "refill", "package_verified": verified,
    }])

    async def fetch(url, **_kwargs):
        assert url == published_url
        specs = "PLA Basic Jade White SKU: 10100 1.75 mm 1 kg refill" if verified else "PLA Basic manufacturer specifications"
        return sources.Evidence(url, "PLA Basic", specs)

    async def search(*_args):
        pytest.fail("Matching manufacturer page should be read first")

    monkeypatch.setattr(sources, "fetch_supplier", fetch)
    monkeypatch.setattr(sources, "search_web", search)
    result = await ask(chat_db, monkeypatch, "Verify Bambu Lab PLA Basic Jade White refill specs.")
    assert result["search"] == "complete"
    live = next(row for row in result["evidence"] if row["kind"] == "live")
    assert (live["url"], live["scope"]) == (published_url, "variant" if verified else "grade")


async def test_ams_comparison_retrieves_both_rules_and_allows_the_cited_device_name(chat_db, monkeypatch):
    question = "Does regular TPU work in the AMS 2 Pro? How about Bambu TPU for AMS?"
    answer = (
        "Use the external feed path for regular TPU on the AMS 2 Pro [compatibility:ams2-pro-tpu]. "
        "Bambu TPU for AMS is supported in the feeder [compatibility:bambu-tpu-for-ams]."
    )
    result = await ask(chat_db, monkeypatch, question, answer)
    assert {"compatibility:ams2-pro-tpu", "compatibility:bambu-tpu-for-ams"} <= {
        row["id"] for row in result["evidence"]
    }
    assert result["reply"] == answer
    invalid = await ask(chat_db, monkeypatch, question, answer + " Dry it at 999 °C.")
    assert "missing_evidence" in invalid["notices"]
    unknown = await ask(chat_db, monkeypatch, question, answer.replace("AMS 2 Pro", "AMS 3 Pro"))
    assert "missing_evidence" in unknown["notices"]
