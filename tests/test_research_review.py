import httpx
import pytest
from app.models import Filament, Manufacturer

from test_backend import BASE, installed

pytest_plugins = ("tests.conftest",)
pytestmark = pytest.mark.asyncio


async def test_conflicting_catalog_values_show_their_exact_variants(installed, db_session, monkeypatch):
    from filabrain import proposals

    async def catalog(_identity):
        common = {"name": "Fixture PLA", "manufacturer": {"name": "Fixture"}, "material": {"key": "PLA"}, "diameter_mm": 1.75}
        return [
            {**common, "id": 50, "nominal_weight_g": 500, "sku": "FIX-500", "density_g_cm3": 1.21},
            {**common, "id": 51, "nominal_weight_g": 1000, "sku": "FIX-1000", "density_g_cm3": 1.27},
        ], "https://catalog.example/fixture-pla", True

    async def ofd(_manufacturer):
        return [], ""

    async def website(_manufacturer):
        return None

    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.setattr(proposals, "manufacturer_website", website)
    monkeypatch.delenv("FILABRAIN_MODEL_URL", raising=False)
    monkeypatch.delenv("FILABRAIN_SEARCH_URL", raising=False)
    maker = Manufacturer(name="Fixture")
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(manufacturer_id=maker.id, designation="Fixture PLA", material_type="PLA", diameter_mm=1.75)
    db_session.add(filament)
    await db_session.commit()
    response = await installed.post(f"{BASE}/proposals", json={"mode": "edit", "filament_id": filament.id})
    assert response.status_code == 200
    result = response.json()
    assert not any(value["field"] == "density_g_cm3" for value in result["suggestions"])
    density = next(issue for issue in result["issues"] if issue.get("field") == "density_g_cm3")
    assert density["requires_product_choice"]
    variants = {option["value"]: option["sources"][0]["title"] for option in density["options"]}
    assert "500 g" in variants[1.21] and "FIX-500" in variants[1.21]
    assert "1000 g" in variants[1.27] and "FIX-1000" in variants[1.27]


@pytest.mark.parametrize(("source_error", "issue_code"), [
    (None, None),
    ("HTTP 404 (page not found)", "unavailable_source"),
    ("HTTP 429 (rate limited)", "supplier_rate_limited"),
])
async def test_follow_up_fetches_reference_for_the_saved_product(installed, db_session, monkeypatch, source_error, issue_code):
    from filabrain import proposals

    site = "https://fixture.example"
    reference = f"{site}/specs.pdf"

    async def catalog(_identity):
        return [], "", True

    async def ofd(_manufacturer):
        return [], ""

    async def supplier(url, **_):
        if url != reference:
            raise ValueError("Fixture has no other pages")
        if source_error:
            raise ValueError(source_error)
        return proposals.Evidence(
            url, "Fixture PLA technical data sheet", "Technical Data Sheet\nProduct name: Fixture PLA\nDensity 1.24 g/cm³",
            datasheet=True,
        )

    async def unavailable_archive(*_args, **_kwargs):
        return None

    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.setattr(proposals, "fetch_supplier", supplier)
    monkeypatch.setattr(proposals, "fetch_archived_supplier", unavailable_archive)
    monkeypatch.delenv("FILABRAIN_MODEL_URL", raising=False)
    monkeypatch.delenv("FILABRAIN_SEARCH_URL", raising=False)
    maker = Manufacturer(name="Fixture", url=site)
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(manufacturer_id=maker.id, designation="Fixture PLA", material_type="PLA", diameter_mm=1.75)
    db_session.add(filament)
    await db_session.commit()

    result = await installed.post(f"{BASE}/proposals", json={
        "mode": "edit", "filament_id": filament.id, "reference_url": reference,
    })
    assert result.status_code == 200
    data = result.json()
    density = next((value for value in data["suggestions"] if value["field"] == "density_g_cm3"), None)
    if issue_code:
        assert any(issue["code"] == issue_code and issue["url"] == reference for issue in data["issues"])
        assert density is None
    else:
        assert density is not None, "A supplied matching reference must be checked even without optional web search"
        assert density["value"] == 1.24
        assert density["sources"][0]["url"] == reference
    await db_session.refresh(filament)
    assert filament.density_g_cm3 is None


@pytest.mark.parametrize(("status", "message"), [(401, "key"), (429, "rate limit"), (404, "could not find")])
async def test_model_connection_reports_the_action_to_take(installed, monkeypatch, status, message):
    from filabrain import settings

    async def request(*_args, **_kwargs):
        response = httpx.Response(status, request=httpx.Request("POST", "https://model.test/v1/chat/completions"))
        response.raise_for_status()

    monkeypatch.setattr(settings, "request_extraction", request)
    result = await installed.post(f"{BASE}/settings/test", json={"model_url": "https://model.test/v1", "model_id": "fixture"})
    assert result.status_code == 502
    assert message in result.json()["detail"].lower()


async def test_search_connection_suggests_enabling_json_after_rejection(installed, monkeypatch):
    from filabrain import settings

    async def request(*_args):
        response = httpx.Response(403, request=httpx.Request("GET", "https://search.test/search"))
        response.raise_for_status()

    monkeypatch.setattr(settings, "search_web", request)
    result = await installed.post(f"{BASE}/settings/test-search", json={"search_url": "https://search.test"})
    assert result.status_code == 502
    assert "Enable JSON output" in result.json()["detail"]


async def test_publisher_grade_specs_survive_unavailable_supplier(installed, db_session, monkeypatch):
    from filabrain import proposals

    async def catalog(identity):
        return [{
            "name": identity.designation, "id": 751,
            "manufacturer": {"name": "Polymaker"}, "material": {"key": "TPU"}, "density_g_cm3": 1.24,
            "diameter_mm": 1.75, "nominal_weight_g": 1000, "sku": "FIXTURE-751",
        }], "https://db.filaman.app/", True

    async def no_catalog(_manufacturer):
        return [], ""

    async def unavailable(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", no_catalog)
    monkeypatch.setattr(proposals, "fetch_supplier", unavailable)
    monkeypatch.delenv("FILABRAIN_MODEL_URL", raising=False)
    monkeypatch.delenv("FILABRAIN_SEARCH_URL", raising=False)
    maker = Manufacturer(name="Polymaker", url="https://www.polymaker.com/")
    db_session.add(maker)
    await db_session.commit()
    result = await installed.post(f"{BASE}/proposals", json={
        "mode": "create", "identity": {"manufacturer": "Polymaker", "designation": "PolyFlex TPU95-HF", "material": "TPU"},
    })
    assert result.status_code == 200
    density = next(row for row in result.json()["suggestions"] if row["field"] == "density_g_cm3")
    assert density["value"] == 1.16
    assert "wiki.polymaker.com" in density["sources"][0]["url"]
    disagreement = next(row for row in result.json()["issues"] if row.get("field") == "density_g_cm3")
    assert {option["value"] for option in disagreement["options"]} == {1.16, 1.24}


async def test_named_pantone_in_product_slug_preserves_its_part_number():
    from filabrain.color_standards import references_in_product_url

    assert references_in_product_url("https://example.org/petg-pantone-black-6c-1-75mm") == ["Pantone Black 6 C"]
    assert references_in_product_url("https://example.org/petg-pantone-scarlet-186c-1-75mm") == ["Pantone 186 C"]


async def test_unverified_bundled_spool_tare_cannot_be_proposed():
    from filabrain.proposals import Identity, bundled_catalog_candidates

    candidates = bundled_catalog_candidates(Identity(
        manufacturer="3D Printing Canada", designation="Budget", color="Dark Green",
    ))
    matching = next(row for row in candidates["SpoolmanDB"] if row["name"] == "Budget Dark Green")
    assert matching["empty_spool_weight_g"] is None
    assert matching["spool_material"] is None


async def test_model_accepts_a_bare_array_and_keeps_quote_validation(monkeypatch):
    import json

    from filabrain import model

    requests = []

    async def completion(*args, **kwargs):
        requests.append(kwargs)
        return json.dumps([
            {"field": "density_g_cm3", "value": 1.24, "source": 1, "quote": "Density: 1.24 g/cm³"},
            {"field": "density_g_cm3", "value": 1.42, "source": 1, "quote": "Density: 1.42 g/cm³"},
        ])

    monkeypatch.setattr(model, "request_completion", completion)
    suggestions, _ = await model.request_extraction("https://model.example/v1", "fixture", "", {}, [
        model.Evidence("https://maker.example/pla", "Fixture PLA", "Density: 1.24 g/cm³"),
    ])
    assert [item["value"] for item in suggestions] == [1.24]
    assert requests == [{"timeout": 45, "max_tokens": 2400}]


async def test_verified_supplier_ranges_survive_a_model_omitting_the_other_source(installed, db_session, monkeypatch):
    from filabrain import proposals

    maker_url = "https://maker.example/fixture-pla"
    retailer_url = "https://retailer.example/fixture-pla"
    wrong_package = "https://retailer.example/fixture-refill"
    product = {"designation": "Fixture PLA", "manufacturer": "Fixture", "material": "PLA", "sku": "FIX-123"}
    maker_quote = "Nozzle temperature: 200–220 °C"
    retailer_quote = "Test specimen nozzle temperature: 195–205 °C\nNozzle temperature: 210–230 °C"

    async def no_catalog(_identity):
        return [], "", True

    async def no_ofd(_manufacturer):
        return [], ""

    async def fetch(url, **_kwargs):
        details = {
            maker_url: (product, maker_quote),
            retailer_url: (product, retailer_quote),
            wrong_package: ({**product, "sku": "FIX-REFILL"}, "Nozzle temperature: 240–260 °C"),
        }
        if url not in details:
            raise TimeoutError
        identity, quote = details[url]
        return proposals.Evidence(url, "Fixture PLA", f"Fixture PLA SKU: {identity['sku']}.\n{quote}", product=identity)

    async def search(*_args):
        return [retailer_url, wrong_package]

    def model_options(_config, *_args):
        return {"model_url": "http://model.test/v1", "id": "fixture"}, "fixture"

    async def model_ready(*_args):
        pass

    async def extract(*_args, **_kwargs):
        return [{
            "field": "extruder_temp_range_c", "value": {"min": 200, "max": 220},
            "sources": [{"url": maker_url, "title": "Fixture PLA", "evidence": maker_quote}],
        }], []

    monkeypatch.setattr(proposals, "catalog_candidates", no_catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", no_ofd)
    monkeypatch.setattr(proposals, "fetch_supplier", fetch)
    monkeypatch.setattr(proposals, "search_web", search)
    monkeypatch.setattr(proposals, "select_model", model_options)
    monkeypatch.setattr(proposals, "check_model_catalog", model_ready)
    monkeypatch.setattr(proposals, "model_key", lambda _endpoint: "")
    monkeypatch.setattr(proposals, "request_extraction", extract)
    monkeypatch.setenv("FILABRAIN_SEARCH_URL", "http://search.test")
    db_session.add(Manufacturer(name="Fixture", url="https://maker.example"))
    await db_session.commit()
    result = await installed.post(f"{BASE}/proposals", json={
        "mode": "create", "product_url": maker_url, "reference_url": retailer_url,
        "identity": {"manufacturer": "Fixture", "designation": "Fixture PLA", "material": "PLA", "sku": "FIX-123"},
    })
    assert result.status_code == 200, result.text
    reply = result.json()
    values = {row["field"]: row["value"] for row in reply["suggestions"]}
    assert values.get("extruder_temp_range_c") == {"min": 200, "max": 220}, reply
    options = next(issue["options"] for issue in reply["issues"] if issue.get("field") == "extruder_temp_range_c")
    assert {(row["value"]["min"], row["sources"][0]["url"]) for row in options} == {
        (200, maker_url), (210, retailer_url),
    }
