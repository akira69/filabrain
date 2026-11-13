import json
from pathlib import Path

import pytest
import pytest_asyncio
from app.api.v1 import filamentdb_proxy
from app.api.v1.router import mount_plugin_router_on_app
from app.main import app
from app.models import Filament, InstalledPlugin, Manufacturer, Spool
from app.services import plugin_service
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from sqlalchemy import event, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

pytest_plugins = ("tests.conftest",)
pytestmark = pytest.mark.asyncio
BASE = "/api/v1/filabrain"
ROOT = Path(__file__).resolve().parents[1]


@pytest_asyncio.fixture
async def installed(auth_client, db_session, monkeypatch):
    browser, csrf = auth_client
    browser.base_url = "https://filaman.test"
    browser.headers["X-CSRF-Token"] = csrf
    plugin = InstalledPlugin(
        plugin_key="filabrain",
        name="FilaBrain",
        version=json.loads((ROOT / "filabrain/plugin.json").read_text())["version"],
        plugin_type="integration",
        is_active=True,
    )
    db_session.add(plugin)
    await db_session.commit()

    monkeypatch.setattr(plugin_service, "PLUGINS_DIR", ROOT)
    monkeypatch.setattr(app.router, "routes", list(app.router.routes))
    monkeypatch.setattr(app.router, "lifespan_context", app.router.lifespan_context)
    monkeypatch.setattr(app.state, "plugin_mcp_tools", {}, raising=False)
    assert mount_plugin_router_on_app(app, plugin.plugin_key)
    from filabrain.db import Base as PluginBase

    try:
        yield browser
    finally:
        await db_session.rollback()
        async with db_session.bind.begin() as connection:
            await connection.run_sync(PluginBase.metadata.drop_all)


async def test_stock_host_mounts_plugin_with_its_own_tables(installed, db_session):
    response = await installed.get(f"{BASE}/settings")
    assert response.status_code == 200
    assert response.json()["mcp_enabled"] is True
    async with db_session.bind.connect() as connection:
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        settings_columns = await connection.run_sync(
            lambda conn: {
                field["name"] for field in inspect(conn).get_columns("app_settings")
            }
        )
    assert "filabrain_settings" in tables
    assert "filabrain_mcp_enabled" not in settings_columns

    config = {"model_url": "http://model.test", "model_id": "example"}
    assert (await installed.put(f"{BASE}/settings", json=config)).status_code == 200
    assert (
        await installed.put(f"{BASE}/settings/mcp", json={"mcp_enabled": False})
    ).status_code == 200
    saved = (await installed.get(f"{BASE}/settings")).json()
    assert saved["model_url"] == config["model_url"]
    assert saved["model_id"] == config["model_id"]
    assert saved["mcp_enabled"] is False


async def test_create_research_draft_is_atomic_and_retry_safe(tmp_path):
    import asyncio

    from app.api.v1.schemas_filament import FilamentCreate
    from app.core.security import Principal
    from app.models import User
    from app.models.base import Base as HostBase
    from filabrain.db import Base as PluginBase
    from filabrain.research import open_saved_filament, save_research_filament

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'drafts.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(HostBase.metadata.create_all)
            await connection.run_sync(PluginBase.metadata.create_all)
        async with sessions() as db:
            owner = User(email="review@example.test")
            manufacturer = Manufacturer(name="Fixture Brand", empty_spool_weight_g=180)
            db.add_all([owner, manufacturer, InstalledPlugin(
                plugin_key="filabrain", name="FilaBrain", version="1.0", plugin_type="integration", is_active=True,
            )])
            await db.commit()
        principal = Principal(auth_type="session", user_id=owner.id, is_superadmin=True)
        draft_id = "a" * 32
        data = FilamentCreate(
            manufacturer_id=manufacturer.id, designation="Fixture PLA", material_type="PLA",
            diameter_mm=1.75, density_g_cm3=1.24,
        )

        async def save():
            async with sessions() as db:
                return await save_research_filament(draft_id, data, db, principal)

        first, retry = await asyncio.gather(save(), save())
        assert first["id"] == retry["id"]
        assert {first["already_saved"], retry["already_saved"]} == {True, False}
        saved_id = first["id"]
        async with sessions() as db:
            records = list(await db.scalars(select(Filament)))
            assert [record.id for record in records] == [saved_id]
            assert records[0].density_g_cm3 == 1.24
            assert records[0].default_spool_weight_g == 180
            assert (await open_saved_filament(draft_id, db, principal)).headers["location"] == f"/filaments/{saved_id}"
            await db.delete(records[0])
            await db.commit()
            db.add(Filament(id=saved_id, manufacturer_id=manufacturer.id, designation="Replacement", material_type="PLA", diameter_mm=1.75))
            await db.commit()
            with pytest.raises(HTTPException) as missing:
                await open_saved_filament(draft_id, db, principal)
            assert missing.value.status_code == 404
        with pytest.raises(HTTPException) as deleted:
            await save()
        assert deleted.value.status_code == 409
    finally:
        await engine.dispose()


async def test_search_connection_checks_unsaved_url_and_reports_no_results(installed, monkeypatch):
    from filabrain import settings

    async def search(url, query):
        assert query == "3D printing filament"
        return ["https://fixture.example/filament"] if url == "http://searxng:8080" else []

    monkeypatch.setattr(settings, "search_web", search)
    route = f"{BASE}/settings/test-search"
    before = (await installed.get(f"{BASE}/settings")).json()["search_url"]
    assert (await installed.post(route, json={"search_url": "http://searxng:8080"})).json() == {"ok": True}
    assert (await installed.post(route, json={"search_url": "http://searxng:8081"})).status_code == 502
    assert (await installed.post(route, json={})).status_code == 422
    assert (await installed.get(f"{BASE}/settings")).json()["search_url"] == before


@pytest.mark.parametrize("response", [[], {"results": {}}, {"results": [], "padding": "x" * (512 * 1024)}])
async def test_search_web_rejects_invalid_or_oversized_results(monkeypatch, response):
    from httpx import Response
    from filabrain import sources

    monkeypatch.setattr(sources.httpx, "AsyncClient", lambda **kwargs: AsyncClient(
        transport=MockTransport(lambda _request: Response(200, json=response)), **kwargs,
    ))
    with pytest.raises(ValueError, match="SearXNG"):
        await sources.search_web("https://searxng.test", "PLA")


async def test_research_sources_keep_defaults_and_survive_older_settings_clients(installed):
    url = f"{BASE}/settings"
    current = (await installed.get(url)).json()
    assert current["supplier_sites"] == []
    supplier_sites = [
        {"domain": " WWW.Filament.example. ", "enabled": True},
        {"domain": "other.example", "enabled": False},
    ]
    response = await installed.put(url, json={
        "supplier_sites": supplier_sites,
    })
    assert response.status_code == 200, response.text
    supplier_sites[0]["domain"] = "filament.example"
    assert response.json()["supplier_sites"] == supplier_sites
    assert (await installed.put(url, json={})).status_code == 200
    saved = (await installed.get(url)).json()
    assert saved["supplier_sites"] == supplier_sites


async def test_supplier_download_limit_persists_and_applies_to_research(installed, monkeypatch):
    from filabrain import proposals

    url = f"{BASE}/settings"
    assert (await installed.get(url)).json()["supplier_download_mib"] == 8
    assert (await installed.put(url, json={"supplier_download_mib": 0})).status_code == 422
    assert (await installed.put(url, json={"supplier_download_mib": 33})).status_code == 422
    assert (await installed.put(url, json={"supplier_download_mib": 12})).status_code == 200
    assert (await installed.put(url, json={})).json()["supplier_download_mib"] == 12

    async def fetch(link, *, max_bytes):
        assert max_bytes == 12 * 1024 * 1024
        return proposals.Evidence(link, "Fixture PLA", "Fixture PLA")

    async def catalog(_identity):
        return [], "", True

    monkeypatch.setattr(proposals, "fetch_supplier", fetch)
    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    response = await installed.post(f"{BASE}/proposals", json={
        "mode": "create", "product_url": "https://supplier.example/products/fixture-pla",
        "identity": {"designation": "Fixture PLA"},
    })
    assert response.status_code == 200


@pytest.mark.parametrize("has_sitemap", [True, False])
@pytest.mark.parametrize("regional", [False, True])
async def test_oversized_supplier_page_finds_specific_replacement(installed, db_session, monkeypatch, has_sitemap, regional):
    from filabrain import proposals

    site = "https://supplier.example"
    original = "https://shop.supplier.example/store" if regional else f"{site}/store"
    replacement = f"{site}/products/abs-black"
    heading = "ABS (Black) 1.75mm 1kg Filament [AB-42]"
    searches = []

    async def fetch(url, **_):
        if url == original:
            raise ValueError("Supplier page is too large")
        assert url == replacement
        return proposals.Evidence(url, heading, heading, {"designation": heading}, scopes=(
            heading, 'Spool Size: 2.25" ID / 7.75" OD / 2.25" Depth',
        ))

    async def sitemap(url):
        assert url == (site if regional else original)
        return (site, f"{site}/products/pla-black", replacement) if has_sitemap else ()

    async def search(_url, query):
        searches.append(query)
        return [site, "https://unrelated.example/products/abs-black", replacement] if query.startswith("site:supplier.example ") and "technical data sheet" in query else []

    async def catalog(_identity):
        return [], "", True

    async def ofd(_manufacturer):
        return [], ""

    monkeypatch.setattr(proposals, "fetch_supplier", fetch)
    monkeypatch.setattr(proposals, "supplier_sitemap_urls", sitemap, raising=False)
    monkeypatch.setattr(proposals, "search_web", search)
    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.delenv("FILABRAIN_MODEL_URL", raising=False)
    assert (await installed.put(f"{BASE}/settings", json={"search_url": "http://searxng:8080"})).status_code == 200
    maker = Manufacturer(name="Fixture Manufacturer", url=site)
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(
        manufacturer_id=maker.id, designation="ABS (Black)", material_type="ABS",
        diameter_mm=1.75, shop_url=original,
    )
    db_session.add(filament)
    await db_session.commit()

    response = await installed.post(f"{BASE}/proposals", json={"mode": "edit", "filament_id": filament.id})
    assert response.status_code == 200
    result = response.json()
    width = next((s for s in result["suggestions"] if s["field"] == "spool_width_mm"), None)
    assert width and width["sources"][0]["url"] == replacement
    assert {"code": "supplier_alternative_found", "url": replacement} in result["issues"]
    assert not any(issue.get("url") == original for issue in result["issues"])
    assert any("technical data sheet" in query and "filetype:pdf" not in query for query in searches) is not has_sitemap
    if regional:
        return
    response = await installed.post(f"{BASE}/proposals", json={"mode": "create", "product_url": original})
    assert response.is_success, response.text
    assert any(issue.get("product_url") == replacement for issue in response.json()["issues"])


@pytest.mark.parametrize("store_host", ["us.fixturebrand.example", "fixturebrand.example"])
@pytest.mark.parametrize("ambiguous", [False, True, "sitemap"])
async def test_oversized_storefront_recovers_specs_from_manufacturer_pdf(installed, monkeypatch, store_host, ambiguous):
    from filabrain import proposals

    site = "https://fixturebrand.example/"
    store = f"https://{store_host}/products/matte-pla"
    sheet = f"{site}documents/polyterra.pdf"
    wrong = f"{site}documents/polyterra-plus.pdf"
    shared = f"{site}documents/polyterra-pla.pdf"
    fake = "https://fixturebrand.example.unrelated.example/documents/polyterra.pdf"
    sheet_text = (
        "TECHNICAL\nDATA SHEET\nV6.0\n\nPolyTerra TM PLA\n"
        "Density             ISO1183             1.31g/cm3 at 23°C\n"
        "Nozzle temperature                    250 (°C)\n"
        "RECOMMENDED PRINTING CONDITIONS\n"
        "Nozzle temperature                 190 – 230 (°C)                 Printing speed          50 - 300 (mm/s)\n"
        "Build plate temperature            25 - 50 (˚C)                   Drying temp. and time   55˚C for 6h\n"
    )

    async def fetch(url, *, max_bytes):
        assert max_bytes == 2 * 1024 * 1024
        if url == store:
            raise ValueError(proposals.SUPPLIER_TOO_LARGE)
        if url == site:
            return proposals.Evidence(url, "Fixture Brand | 3D printing filament", "Fixture Brand makes filament.")
        if url == shared:
            return proposals.Evidence(url, "Technical data sheet", sheet_text, datasheet=True)
        assert url in (sheet, wrong)
        if ambiguous:
            content = sheet_text + f"\nSKU: {'PT-001' if url == sheet else 'PT-002'}"
        else:
            content = sheet_text if url == sheet else sheet_text.replace("PolyTerra TM PLA", "PolyTerra™ PLA+")
        return proposals.Evidence(url, "Technical data sheet", content, datasheet=True)

    async def search(_url, query):
        if query == "Fixture Brand filament official website":
            raise proposals.SearchEnginesUnavailable("Upstream search engines failed to return results")
        if query == "site:fixturebrand.example polyterra pla technical data sheet filetype:pdf":
            return [fake, wrong, sheet]
        return []

    async def catalog(_identity):
        return [], "", True

    async def no_results(_manufacturer):
        return []

    async def ofd(_manufacturer):
        return [], ""

    monkeypatch.setattr(proposals, "fetch_supplier", fetch)
    monkeypatch.setattr(proposals, "search_web", search)
    async def sitemap(_url):
        return [shared] if ambiguous == "sitemap" else []

    monkeypatch.setattr(proposals, "supplier_sitemap_urls", sitemap)
    monkeypatch.setattr(proposals, "manufacturer_website", no_results)
    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.delenv("FILABRAIN_MODEL_URL", raising=False)
    assert (await installed.put(f"{BASE}/settings", json={
        "search_url": "http://searxng:8080", "supplier_download_mib": 2,
    })).is_success
    response = await installed.post(f"{BASE}/proposals", json={
        "mode": "create", "product_url": store,
        "identity": {"manufacturer": "Fixture Brand", "designation": "PolyTerra PLA - Forest Green", "color": "Forest Green"},
    })
    assert response.is_success, response.text
    result = response.json()
    if ambiguous is True:
        assert {"code": "supplier_download_limit", "url": store} in result["issues"]
        assert not any(issue["code"] == "supplier_alternative_found" for issue in result["issues"])
        assert not result["suggestions"]
        return
    if ambiguous == "sitemap":
        sheet = shared
        assert any(item["field"] == "density_g_cm3" and item["sources"][0]["url"] == shared
                   for item in result["suggestions"])
        assert not any(issue.get("url") == store for issue in result["issues"])
        return
    assert {"code": "supplier_alternative_found", "url": sheet} in result["issues"]
    assert not any(issue.get("url") in (store, wrong, fake) for issue in result["issues"])
    specs = {suggestion["field"]: suggestion for suggestion in result["suggestions"]}
    expected = {
        "density_g_cm3": 1.31,
        "extruder_temp_range_c": {"min": 190, "max": 230},
        "bed_temp_range_c": {"min": 25, "max": 50},
        "drying_temp_c": 55,
        "drying_time_hours": 6,
        "datasheet_url": sheet,
    }
    assert {field: spec["value"] for field, spec in specs.items()} == expected
    for field, spec in specs.items():
        assert {source["url"] for source in spec["sources"]} == {sheet}
        if field != "datasheet_url":
            assert spec["sources"][0]["evidence"] in " ".join(sheet_text.split())


@pytest.mark.parametrize(("designation", "material", "color", "text", "expected"), [
    ("PLA+", "PLA+", "", (
        "PLA+                          Technical Data Sheet\n"
        "Density     GB/T 1033           1.24g/cm 3\n"
        "Recommended Printing Parameters                   Data\n"
        "Drying Preparation        50℃，8~12h\n"
        "Nozzle Temperature        210-230℃\n"
        "Build Platform Temperature        45~60℃\n"
        "Printing Test Conditions\n"
        "Build Platform Temperature       65℃"
    ), {"density_g_cm3": 1.24, "drying_temp_c": 50,
        "extruder_temp_range_c": {"min": 210, "max": 230},
        "bed_temp_range_c": {"min": 45, "max": 60}}),
    ("Black PLA+ 2.0", "PLA+", "Black", (
        "三绿物性表 ISO                 页码/总页数 1/4\n"
        "产品名称 Product Name: PLA+2.0\n"
        "密度 Density      ISO 1183     23℃    g/cm 3\n"
        "                                          1.21\n"
        "建议打印参数 Recommended Printing Parameters\n"
        "喷嘴温度 Nozzle Temp.\n"
        "                        205-215          50-100\n"
        "                        215-245         100-200\n"
        "平台温度 Plate Temp.            50-60℃"
    ), {"density_g_cm3": 1.21, "bed_temp_range_c": {"min": 50, "max": 60}}),
])
async def test_datasheet_layout_keeps_exact_identity_and_cites_only_supported_values(
    designation, material, color, text, expected,
):
    from filabrain import proposals

    identity = proposals.Identity(designation=designation, material=material, color=color)
    url = "https://manufacturer.example/technical.pdf"
    page = proposals.Evidence(url, "Technical data sheet", text, datasheet=True)
    titles = proposals._product_titles(identity)
    assert proposals._sheet_matches(page, titles, identity)
    wrong = proposals.replace(page, text=text.replace("PLA+", "High Speed PLA+", 1))
    assert not proposals._sheet_matches(wrong, titles, identity)
    specs, issues = proposals._sheet_specs(page)
    assert not issues
    assert {item["field"]: item["value"] for item in specs} == expected
    assert all(item["sources"][0]["url"] == url for item in specs)

async def test_datasheet_product_label_overrides_generic_header_cell():
    from filabrain import proposals

    identity = proposals.Identity(designation="PLA", material="PLA")
    page = proposals.Evidence(
        "https://manufacturer.example/technical.pdf", "Technical data sheet",
        "Technical Data Sheet\nProduct Name: PLA High Speed\nBase Material     PLA",
        datasheet=True,
    )
    assert not proposals._sheet_matches(page, {"PLA"}, identity)


async def test_datasheet_matches_sku_only_with_explicit_exact_label():
    from filabrain import proposals

    identity = proposals.Identity(sku="FP-1234")
    sheet = proposals.Evidence(
        "https://manufacturer.example/technical.pdf", "Technical data sheet",
        "Technical Data Sheet\nFast PLA\nSKU: FP-1234", datasheet=True,
    )
    assert proposals._sheet_matches(sheet, proposals._product_titles(identity), identity)
    assert not proposals._sheet_matches(proposals.replace(sheet, text=sheet.text + "\nSKU: FP-1235"), set(), identity)


@pytest.mark.parametrize("section, expected", [
    ("• Sample Preparation\nNozzle Temperature 240 °C", {}),
    ("• Recommended Printing Settings\nNozzle Temperature 210 °C\n"
     "• Sample Preparation\nBed Temperature 70 °C", {"extruder_temp_range_c": {"min": 210, "max": 210}}),
    ("Recommended Printing Settings\nNozzle Temperature 210 °C\n"
     "Specimen Printing Parameters\nBed Temperature 70 °C", {"extruder_temp_range_c": {"min": 210, "max": 210}}),
    ("Recommended Printing Settings\nNozzle Temperature 210 °C\n"
     "1. Sample Preparation\nBed Temperature 70 °C", {"extruder_temp_range_c": {"min": 210, "max": 210}}),
])
async def test_datasheet_extracts_only_recommended_temperatures(section, expected):
    from filabrain import proposals

    page = proposals.Evidence("https://manufacturer.example/technical.pdf", "Technical data sheet", section, datasheet=True)
    values, _ = proposals._sheet_specs(page)
    assert {item["field"]: item["value"] for item in values} == expected


@pytest.mark.parametrize("pdf_state", ["ok", "oversized", "sitemap", "sitemap_both_oversized", "same_page", "manual", "conflict"])
async def test_datasheet_from_manufacturer_product_page_can_use_a_cdn(installed, db_session, monkeypatch, pdf_state):
    from filabrain import proposals

    site = "https://manufacturer.example/"
    product = site + "products/pla-basic"
    storefront = site + "store/pla-basic"
    downloads = site + "downloads/pla-basic"
    sheet = "https://cdn.example/technical.pdf"
    replacement = site + "pla-basic.pdf"
    unrelated = "https://other.example/products/pla-basic"
    pdf = proposals.Evidence(
        sheet, "Technical data sheet",
        "Technical Data Sheet\nPLA Basic\nDensity 1.24 g/cm³", datasheet=True,
    )

    async def fetch(url, **_):
        if url == storefront:
            raise ValueError(proposals.SUPPLIER_TOO_LARGE)
        if url == downloads:
            return proposals.Evidence(url, "PLA Basic downloads", "", datasheets=(replacement,))
        if url == sheet:
            if pdf_state in ("oversized", "sitemap", "sitemap_both_oversized", "same_page"):
                raise ValueError(proposals.SUPPLIER_TOO_LARGE)
            if pdf_state == "manual":
                return proposals.replace(pdf, manual=True, text="")
            return pdf
        if url == replacement:
            return proposals.replace(pdf, url=url, text=pdf.text.replace("1.24", "1.26") if pdf_state == "conflict" else pdf.text)
        linked = (sheet, replacement) if pdf_state == "conflict" else (sheet,)
        return proposals.Evidence(url, "PLA Basic | Fixture Brand", "PLA Basic Density 1.31 g/cm³", datasheets=linked if url == product else ())

    async def search(_url, query):
        if "filetype:pdf" in query:
            return [replacement] if pdf_state == "oversized" else []
        if pdf_state == "sitemap_both_oversized" and query.startswith("site:manufacturer.example ") and "technical data sheet" not in query:
            return [product]
        return []

    async def sitemap(url):
        assert url.startswith(site)
        return {"sitemap": [replacement], "sitemap_both_oversized": [downloads], "same_page": [product]}.get(pdf_state, [])

    async def extract(*args, **kwargs):
        return [{"field": "density_g_cm3", "value": 1.31, "sources": [{"url": product, "title": "Store", "evidence": "1.31 g/cm³"}]}], []

    async def catalog(_identity):
        return [], "", True

    async def ofd(_manufacturer):
        return [], ""

    monkeypatch.setattr(proposals, "supplier_sitemap_urls", sitemap)
    monkeypatch.setattr(proposals, "fetch_supplier", fetch)
    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.setattr(proposals, "search_web", search)
    monkeypatch.setattr(proposals, "_supplier_scopes", lambda identity, page, snapshot=None: [page] if page.url == product else [])
    monkeypatch.setattr(proposals, "request_extraction", extract)
    monkeypatch.delenv("FILABRAIN_SEARCH_URL", raising=False)
    monkeypatch.delenv("FILABRAIN_MODEL_URL", raising=False)
    db_session.add(Manufacturer(name="Fixture Brand", url=site))
    await db_session.commit()
    assert (await installed.put(f"{BASE}/settings", json={
        "search_url": "http://searxng:8080", "model_url": "http://ollama:11434/v1", "model_id": "fixture",
    })).is_success

    original = storefront if pdf_state == "sitemap_both_oversized" else product
    for url in ((original,) if pdf_state == "sitemap_both_oversized" else (original, unrelated)):
        response = await installed.post(f"{BASE}/proposals", json={
            "mode": "create", "product_url": url,
            "identity": {"manufacturer": "Fixture Brand", "designation": "PLA Basic"},
        })
        assert response.is_success, response.text
        result = response.json()
        density = next((s for s in result["suggestions"] if s["field"] == "density_g_cm3"), None)
        if url == original:
            if pdf_state == "same_page":
                assert {"code": "supplier_download_limit", "url": sheet} in result["issues"]
                assert not any(issue["code"] == "supplier_alternative_found" for issue in result["issues"])
            elif pdf_state == "manual":
                assert {"code": "manual_datasheet", "url": sheet} in result["issues"]
            elif pdf_state == "conflict":
                assert density is None
                assert any(issue["code"] == "conflict" and issue.get("field") == "density_g_cm3" for issue in result["issues"])
            else:
                assert density and density["value"] == 1.24
                assert density["sources"][0]["url"] == (replacement if pdf_state in ("oversized", "sitemap", "sitemap_both_oversized") else sheet)
                assert any(issue["code"] == "source_disagreement" and issue.get("field") == "density_g_cm3" for issue in result["issues"])
        else:
            assert density is None


async def test_supplier_sitemap_follows_product_index_without_fetching_pages(monkeypatch):
    from filabrain import sources

    site = "https://sitemap-fixture.example"
    product = f"{site}/products/abs-black"
    requests = []
    bodies = {
        "/robots.txt": f"Sitemap: {site}/index.xml",
        "/index.xml": (
            f"<sitemapindex><sitemap><loc>{site}/products.xml</loc></sitemap>"
            "<sitemap><loc>https://other.example/index.xml</loc></sitemap></sitemapindex>"
        ),
        "/products.xml": f"<urlset><url><loc>{product}</loc></url></urlset>",
    }

    def fetch(url, _address, _deadline, *, max_bytes, index):
        assert index and max_bytes == 512 * 1024
        assert url.hostname == "sitemap-fixture.example"
        requests.append(url.path)
        return 200, "", "text/xml", bodies[url.path].encode()

    monkeypatch.setattr(sources, "_public_address", lambda *_: "203.0.113.10")
    monkeypatch.setattr(sources, "_get_pinned", fetch)
    assert await sources.supplier_sitemap_urls(f"{site}/oversized") == (product,)
    assert requests == ["/robots.txt", "/index.xml", "/products.xml"]


@pytest.mark.parametrize("domain", ["https://example.org", "localhost", "192.168.0.1", "example.org path"])
async def test_research_settings_reject_non_domain_sources(installed, domain):
    response = await installed.put(f"{BASE}/settings", json={
        "supplier_sites": [{"domain": domain, "enabled": True}],
    })
    assert response.status_code == 422


@pytest.mark.parametrize("case", ["match", "blocked", "specs_after_listing"])
async def test_research_uses_enabled_supplier_domains_and_keeps_builtin_catalogs(installed, monkeypatch, case):
    from filabrain import proposals

    # A client using the earlier preview must not be able to turn off the catalogs.
    response = await installed.put(f"{BASE}/settings", json={
        "search_url": "http://searxng:8080",
        "disabled_sources": ["filamandb", "open_filament_database"],
        "supplier_sites": [
            {"domain": "supplier.example", "enabled": True},
            {"domain": "disabled.example", "enabled": False},
        ],
    })
    assert response.status_code == 200
    queries = []
    fetched = []
    catalogs = set()
    archived = []
    supplier_url = "https://shop.supplier.example/fixture-pla"
    wrong = "https://shop.supplier.example/listing"

    async def search(_url, query):
        queries.append(query)
        if query.startswith("site:supplier.example "):
            return ["https://shop.supplier.example/", wrong, supplier_url]
        return []

    async def catalog(_identity):
        catalogs.add("filamandb")
        return [], "", True

    async def ofd(_manufacturer):
        catalogs.add("open_filament_database")
        return [], ""

    async def fetch(url, **_):
        fetched.append(url)
        if case == "blocked" and url == supplier_url:
            raise ValueError("HTTP 403")
        if case == "specs_after_listing":
            text = "Fixture Brand Fixture PLA"
            return proposals.Evidence(url, "Fixture PLA", text if url == wrong else text + " Density 1.24 g/cm³")
        return proposals.Evidence(url, "Unrelated" if url == wrong else "Fixture PLA", "Other product" if url == wrong else "Fixture PLA")

    async def archive(url, **_):
        archived.append(url)
        return None

    monkeypatch.setattr(proposals, "fetch_archived_supplier", archive)
    monkeypatch.setattr(proposals, "search_web", search)
    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.setattr(proposals, "fetch_supplier", fetch)
    response = await installed.post(f"{BASE}/proposals", json={
        "mode": "create", "identity": {"manufacturer": "Fixture Brand", "designation": "Fixture PLA"},
    })
    assert response.status_code == 200
    assert "site:supplier.example Fixture Brand Fixture PLA" in queries
    assert all("disabled.example" not in query for query in queries)
    assert fetched == [wrong, supplier_url]
    assert archived == ([supplier_url] if case == "blocked" else [])
    assert catalogs == {"filamandb", "open_filament_database"}


async def test_preferred_supplier_uses_catalog_verified_sku(installed, db_session, monkeypatch):
    from filabrain import proposals

    url = "https://supplier.example/fixture-pla"
    maker = Manufacturer(name="Fixture Brand", url="https://manufacturer.example")
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(manufacturer_id=maker.id, designation="Fixture PLA", material_type="PLA", diameter_mm=1.75)
    db_session.add(filament)
    await db_session.commit()

    async def catalog(_identity):
        return [{
            "name": "Fixture PLA", "manufacturer": {"name": maker.name},
            "material_key": "PLA", "diameter_mm": 1.75, "sku": "AB-1234",
        }], "https://catalog.example/fixture-pla", True

    async def ofd(_manufacturer):
        return [], ""

    async def search(_origin, query):
        if query.startswith("site:supplier.example "):
            assert "ab-1234" in query.lower()
            return [url]
        return []

    async def fetch(link, **_):
        if link == url:
            return proposals.Evidence(link, "Fixture PLA", "SKU: AB-1234 Density: 1.24 g/cm³")
        raise ValueError("Unavailable")

    async def extract(_base, _model, _key, _identity, evidence, **_kwargs):
        return ([
            {
                "field": "density_g_cm3", "value": 1.24,
                "sources": [{"url": url, "title": "Supplier", "evidence": "1.24 g/cm³"}],
            }
        ] if any(source.url == url for source in evidence) else []), []

    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.setattr(proposals, "search_web", search)
    monkeypatch.setattr(proposals, "fetch_supplier", fetch)
    monkeypatch.setattr(proposals, "request_extraction", extract)
    assert (await installed.put(f"{BASE}/settings", json={
        "search_url": "http://searxng:8080", "model_url": "http://model.example", "model_id": "fixture",
        "supplier_sites": [{"domain": "supplier.example"}],
    })).is_success
    response = await installed.post(f"{BASE}/proposals", json={"mode": "edit", "filament_id": filament.id})
    assert response.is_success
    assert any(item["field"] == "density_g_cm3" and item["sources"][0]["url"] == url
               for item in response.json()["suggestions"]), response.json()


@pytest.mark.parametrize("catalog_has_site", [True, False])
async def test_missing_manufacturer_site_is_discovered_and_can_be_saved(
    installed, db_session, monkeypatch, normal_user, catalog_has_site,
):
    from app.core.security import generate_token_secret, hash_token
    from app.models import UserSession

    from filabrain import proposals

    website = "https://fixturebrand.example/"
    manufacturer = Manufacturer(name="Fixture Brand")
    db_session.add(manufacturer)
    await db_session.flush()
    filament = Filament(
        manufacturer_id=manufacturer.id, designation="Fixture PLA", material_type="PLA", diameter_mm=1.75,
    )
    db_session.add(filament)
    await db_session.commit()
    record = f"{BASE}/filaments/{filament.id}/research-record"
    response = await installed.get(record)
    assert response.json() == {"record_token": getattr(filament, "record_token", None)}
    monkeypatch.delenv("FILABRAIN_SEARCH_URL", raising=False)
    if not catalog_has_site:
        assert (await installed.put(f"{BASE}/settings", json={"search_url": "http://searxng:8080"})).is_success

    async def catalog_request(path, params):
        assert path == "/manufacturers"
        return {"items": [
            {"name": "Fixture Brand Store", "website": "https://retailer.example/"},
            {"name": "Fixture Brand", "website": website if catalog_has_site else None},
        ]}

    async def catalog(_identity):
        return [], "", True

    async def ofd(_name):
        return [], ""

    async def search(_url, query):
        if query == "Fixture Brand filament official website":
            return ["https://shop.fixturebrand.example/", website, "https://presets.fixturebrand.example/"]
        return []

    async def fetch(url, **_):
        assert url in (website, "https://shop.fixturebrand.example/", "https://presets.fixturebrand.example/")
        return proposals.Evidence(url, "Fixture Brand | 3D printing filament", "Fixture Brand makes filament.")

    monkeypatch.setattr(filamentdb_proxy, "_proxy_get", catalog_request)
    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.setattr(proposals, "search_web", search)
    monkeypatch.setattr(proposals, "fetch_supplier", fetch)
    response = await installed.post(f"{BASE}/proposals", json={"mode": "edit", "filament_id": filament.id})
    assert response.is_success, response.text
    issue = next(issue for issue in response.json()["issues"] if issue["code"] == "manufacturer_website_found")
    assert issue["url"] == website
    assert issue["source"] == ("FilaManDB" if catalog_has_site else "SearXNG")
    assert (await installed.get(f"/api/v1/manufacturers/{manufacturer.id}")).json()["url"] is None
    payload = {"manufacturer_id": manufacturer.id, "name": manufacturer.name, "url": issue["url"]}
    endpoint = f"{BASE}/filaments/{filament.id}/manufacturer-website"
    if catalog_has_site:
        token = generate_token_secret()
        session = UserSession(user_id=normal_user.id, session_token_hash=hash_token(token))
        db_session.add(session)
        await db_session.commit()
        csrf = generate_token_secret()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://filaman.test",
            cookies={"session_id": f"sess.{session.id}.{token}", "csrf_token": csrf},
            headers={"X-CSRF-Token": csrf},
        ) as viewer:
            assert (await viewer.get(f"{BASE}/status")).json()["can_update_manufacturer"] is False
            assert (await viewer.put(endpoint, json=payload)).status_code == 403
            assert (await viewer.get(record)).status_code == 403
    assert (await installed.put(endpoint, json=payload | {"manufacturer_id": manufacturer.id + 1})).status_code == 409
    assert (await installed.put(endpoint, json=payload | {"url": "http://127.0.0.1/"})).status_code == 422
    if getattr(filament, "record_token", None):
        stale = "0" * 32
        assert (await installed.put(endpoint, json=payload | {"record_token": stale})).status_code == 409
        payload["record_token"] = filament.record_token
    assert (await installed.put(endpoint, json=payload)).status_code == 204
    assert (await installed.get(f"/api/v1/manufacturers/{manufacturer.id}")).json()["url"] == website
    assert (await installed.put(endpoint, json=payload | {"url": "https://other.example/"})).status_code == 409


async def test_conversation_model_is_catalog_scoped(installed, monkeypatch):
    from filabrain import research, settings

    config = {"model_url": "http://model.test", "model_id": "fixture:default"}
    assert (await installed.put(f"{BASE}/settings", json=config)).status_code == 200
    calls = []

    async def models(url, _key):
        assert url == config["model_url"]
        return ["fixture:alternate"]

    async def complete(url, model, _key, _messages, **_options):
        assert url == config["model_url"]
        calls.append(model)
        return "PLA is easier to print."

    monkeypatch.setattr(settings, "model_ids", models)
    monkeypatch.setattr(research, "request_completion", complete)
    response = await installed.get(f"{BASE}/models")
    assert response.json() == {"models": ["fixture:alternate"]}
    chat = {"messages": [{"role": "user", "content": "Which filament is easier to print?"}]}
    response = await installed.post(f"{BASE}/chat", json=chat | {"model_id": "fixture:alternate"})
    assert response.status_code == 200
    response = await installed.post(f"{BASE}/chat", json=chat | {"model_id": "unlisted"})
    assert response.status_code == 422
    assert (await installed.post(f"{BASE}/chat", json=chat)).status_code == 200
    assert calls == ["fixture:alternate", "fixture:default"]
    assert (await installed.get(f"{BASE}/settings")).json()["model_id"] == "fixture:default"


@pytest.mark.parametrize(("upstream_response", "expected"), [
    (Response(200, json={"results": [{"url": "https://maker.example/specs"}]}), 200),
    (Response(200, json={"results": []}), 502),
    (Response(200, json={"results": [{"url": ""}]}), 502),
    (Response(200, json={"results": [{"url": "https://maker.example:8443/specs"}]}), 502),
    (Response(200, json={"results": [{"url": "https://maker.example:443/specs"}]}), 200),
    (Response(200, json={"results": [None, {}, {}, {"url": "https://maker.example/specs"}]}), 200),
    (Response(200, json={"status": "ok"}), 502),
    (Response(403), 502),
])
async def test_searxng_connection_uses_unsaved_url(installed, monkeypatch, upstream_response, expected):
    from filabrain import sources

    original = (await installed.get(f"{BASE}/settings")).json()
    requests = []

    def upstream(request):
        requests.append(request)
        return upstream_response

    monkeypatch.setattr(sources.httpx, "AsyncClient", lambda **kwargs: AsyncClient(
        transport=MockTransport(upstream), **kwargs,
    ))
    path = f"{BASE}/settings/test-search"
    assert (await installed.post(path, json={"search_url": "https://user:secret@searxng.test"})).status_code == 422
    assert (await installed.post(path, json={"search_url": " "})).status_code == 422
    assert (await installed.post(path, json={"search_url": "http://searxng.test:8080/", "api_key": "unused"})).status_code == 422
    response = await installed.post(path, json={"search_url": "http://searxng.test:8080/"})
    assert response.status_code == expected, response.text
    assert len(requests) == 1
    request, = requests
    assert request.method == "GET"
    assert request.url.host == "searxng.test"
    assert request.url.path == "/search"
    assert request.url.params["format"] == "json"
    assert "Authorization" not in request.headers
    assert (await installed.get(f"{BASE}/settings")).json() == original


async def test_searxng_connection_requires_plugin_admin(installed, normal_user, db_session):
    from app.core.security import generate_token_secret, hash_token
    from app.models import UserApiKey

    secret = generate_token_secret()
    key = UserApiKey(user_id=normal_user.id, name="Limited key", key_hash=hash_token(secret), scopes=["filaments:read"])
    db_session.add(key)
    await db_session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://filaman.test") as client:
        response = await client.post(
            f"{BASE}/settings/test-search", json={"search_url": "http://searxng.test"},
            headers={"Authorization": f"ApiKey uak.{key.id}.{secret}"},
        )
    assert response.status_code == 403


@pytest.mark.parametrize(("base_url", "limit"), [
    ("https://api.openai.com/v1", "max_completion_tokens"),
    ("https://api.anthropic.com/v1", "max_tokens"),
    ("http://ollama:11434/v1", "max_tokens"),
])
async def test_model_completion_provider_request(monkeypatch, base_url, limit):
    from filabrain import model
    from httpx import Response

    messages = [{"role": "user", "content": "Hi"}]

    def upstream(request):
        assert str(request.url) == f"{base_url}/chat/completions"
        assert request.headers["Authorization"] == "Bearer fixture-key"
        payload = json.loads(request.content)
        assert set(payload) == {"model", "messages", limit}
        assert payload["messages"] == messages
        return Response(200, json={"choices": [{"message": {"content": "Hello"}}]})

    monkeypatch.setattr(model.httpx, "AsyncClient", lambda **kwargs: AsyncClient(
        transport=MockTransport(upstream), **kwargs,
    ))
    assert await model.request_completion(base_url, "fixture-model", "fixture-key", messages) == "Hello"


async def test_stock_host_api_key_authenticates_standalone_mcp(installed, db_session):
    maker = Manufacturer(name="Stock host maker")
    db_session.add(maker)
    await db_session.commit()
    key = await installed.post("/api/v1/me/api-keys", json={"name": "MCP test"})
    assert key.status_code == 201
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://filaman.test",
        headers={"Authorization": f"Bearer {key.json()['token']}"},
    ) as external:

        async def call(tool, **arguments):
            response = await external.post(
                f"{BASE}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
            assert response.status_code == 200
            return response.json()["result"]

        result = await call(
            "create_filament",
            manufacturer_id=maker.id,
            designation="Fixture PLA",
            material_type="PLA",
            diameter_mm=1.75,
        )
        filament = result["structuredContent"]["id"]
        assert (await call("create_spool", filament_id=filament, rfid_uid="DEADBEEF"))[
            "isError"
        ]
        spool = (await call("create_spool", filament_id=filament))["structuredContent"]
        assert await db_session.scalar(select(Spool.id)) == spool["id"]
        assert (
            await installed.delete(f"/api/v1/me/api-keys/{key.json()['id']}")
        ).status_code == 204
        response = await external.post(
            f"{BASE}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code == 401


async def test_mcp_requires_a_configured_proxy_for_forwarded_https(
    installed, monkeypatch
):
    key = await installed.post("/api/v1/me/api-keys", json={"name": "MCP proxy test"})
    headers = {
        "Authorization": f"Bearer {key.json()['token']}",
        "X-Real-IP": "192.0.2.20",
        "X-Forwarded-For": "192.0.2.10, 192.0.2.20",
        "X-Forwarded-Proto": "https",
    }
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    proxy = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
    async with AsyncClient(
        transport=ASGITransport(app=proxy, client=("127.0.0.1", 10000)),
        base_url="http://filaman.test",
    ) as external:
        monkeypatch.delenv("FILABRAIN_MCP_TRUSTED_PROXY_IPS", raising=False)
        assert (
            await external.post(f"{BASE}/mcp", headers=headers, json=payload)
        ).status_code == 403

        monkeypatch.setenv("FILABRAIN_MCP_TRUSTED_PROXY_IPS", "192.0.2.20")
        assert (
            await external.post(f"{BASE}/mcp", headers=headers, json=payload)
        ).status_code == 200
        wrong_peer = headers | {"X-Forwarded-For": "192.0.2.20, 192.0.2.10"}
        assert (
            await external.post(f"{BASE}/mcp", headers=wrong_peer, json=payload)
        ).status_code == 403
        plain = headers | {"X-Forwarded-Proto": "http"}
        assert (
            await external.post(f"{BASE}/mcp", headers=plain, json=payload)
        ).status_code == 403


async def test_stock_host_session_pairing_and_built_in_proposals(
    installed, monkeypatch
):
    from filabrain import research

    async def proposal(_db, _identity, **_hints):
        return {"suggestions": [], "issues": []}

    monkeypatch.setattr(research, "research_products", proposal)
    target = {"mode": "create", "identity": {"designation": "Fixture PLA"}}
    response = await installed.post(f"{BASE}/pairings", json=target)
    assert response.status_code == 201, response.text
    pairing = response.json()
    assert pairing["url"] == f"https://filaman.test{BASE}/mcp"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://filaman.test",
        headers={"Authorization": f"Bearer {pairing['token']}"},
    ) as external:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert (await external.post(f"{BASE}/mcp", json=payload)).status_code == 200
        await installed.put(f"{BASE}/settings/mcp", json={"mcp_enabled": False})
        assert (await external.post(f"{BASE}/mcp", json=payload)).status_code == 404
    assert (await installed.post(f"{BASE}/proposals", json=target)).status_code == 200


@pytest.mark.parametrize("oversized", [False, True])
async def test_host_mcp_can_research_with_legacy_endpoint_off(installed, db_session, monkeypatch, oversized):
    from app.core.config import settings
    from app.models import AppSettings

    if not hasattr(settings, "mcp_plugin_read_tools"):
        pytest.skip("Host does not support MCP plugin tools")

    from filabrain import research

    async def proposal(_db, identity, **hints):
        assert identity.sku == "ABC-123"
        assert hints["product_url"] == "https://manufacturer.test/pla"
        assert hints["deadline"] is not None and hints["release_read"]
        return {
            "suggestions": [{
                "field": "density_g_cm3", "value": 1.24,
                "sources": [{"url": hints["product_url"], "evidence": "1.24 g/cm³"}]
                * (1000 if oversized else 1),
            }],
            "issues": [],
        }

    monkeypatch.setattr(research, "research_products", proposal)
    name = "filabrain-research_filament"
    monkeypatch.setattr(settings, "mcp_plugin_read_tools", name)
    row = await db_session.get(AppSettings, 1)
    if row is None:
        row = AppSettings(id=1)
        db_session.add(row)
    row.mcp_enabled = True
    row.mcp_write_enabled = False
    await db_session.commit()
    key = await installed.post("/api/v1/me/api-keys", json={"name": "Host MCP", "scopes": ["filaments:read"]})
    assert key.status_code == 201
    assert (await installed.put(f"{BASE}/settings/mcp", json={"mcp_enabled": False})).status_code == 200
    token = key.json()["token"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://filaman.test") as client:
        listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        response = await client.post("/mcp", json=listing, headers={"Authorization": f"ApiKey {token}"})
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert name in names
        assert "filabrain-propose_filament" not in names
        assert "create_filament" not in names
        request = {
            **listing, "method": "tools/call",
            "params": {"name": name, "arguments": {
                "identity": {"sku": "ABC-123"}, "product_url": "https://manufacturer.test/pla",
            }},
        }
        response = await client.post("/mcp", json=request, headers={"Authorization": f"ApiKey {token}"})
        result = response.json()["result"]
        parsed = json.loads(result["content"][0]["text"])
        assert result["isError"] is oversized, result
        if oversized:
            assert parsed["http_status"] == 422
        else:
            assert parsed["proposal"]["suggestions"][0]["sources"][0]["evidence"] == "1.24 g/cm³"
        response = await client.post(f"{BASE}/mcp", json=listing, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 404

    assert not (await db_session.scalars(select(Filament))).all()


async def test_interactive_research_keeps_website_discovery_with_read_release(db_session, monkeypatch):
    from filabrain import proposals

    snapshot = dict.fromkeys(proposals.MODE_FIELDS["fill"], "filled")
    snapshot.update(
        designation="Fixture PLA", manufacturer_name="Fixture Brand", manufacturer_url=None,
        material_type="PLA", diameter_mm=1.75, raw_material_weight_g=1000,
    )
    issue = {"code": "manufacturer_website_found", "url": "https://fixture.example"}

    async def research_products(_db, _identity, **kwargs):
        assert kwargs["release_read"]
        return {"suggestions": [], "issues": [issue]}

    monkeypatch.setattr(proposals, "research_products", research_products)
    result = await proposals.batch_proposal(db_session, snapshot, "fill", release_read=True)
    assert result["issues"] == [issue]
    assert await proposals.batch_proposal(db_session, snapshot, "fill", skip_complete=True) == {
        "proposals": [], "issues": [],
    }


async def test_research_deadline_preserves_verified_datasheet(installed, db_session, monkeypatch):
    import asyncio
    from filabrain import proposals

    async def catalog(_identity):
        return [], "", True

    async def fetch(url, **_):
        return proposals.Evidence(url, "Technical Data Sheet", "Technical Data Sheet\nFixture PLA\nDensity 1.24 g/cm³", datasheet=True)

    async def search(*_args):
        await asyncio.sleep(5)

    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "fetch_supplier", fetch)
    monkeypatch.setattr(proposals, "search_web", search)
    assert (await installed.put(f"{BASE}/settings", json={"search_url": "http://searxng.test"})).is_success
    snapshot = {"designation": "Fixture PLA", "manufacturer_name": "Fixture", "material_type": "PLA",
                "manufacturer_url": "https://fixture.example", "datasheet_url": "https://fixture.example/specs.pdf"}
    async with asyncio.timeout(1):
        result = await proposals.research_products(
            db_session, proposals.Identity(designation="Fixture PLA", manufacturer="Fixture", material="PLA"),
            snapshot=snapshot, release_read=True,
            deadline=asyncio.get_running_loop().time() + 0.1,
        )
    assert any(item["field"] == "density_g_cm3" and item["value"] == 1.24 for item in result["suggestions"])
    assert any(issue["code"] == "unavailable_source" and issue["source"] == "Web search" for issue in result["issues"])


async def test_stock_host_batch_claim_and_apply(
    installed, db_engine, db_session, monkeypatch
):
    from filabrain import batch_api, batch_worker

    async def propose(_db, _snapshot, _mode, **_hints):
        return {
            "issues": [],
            "proposals": [
                {
                    "id": "density_g_cm3",
                    "fields": {"density_g_cm3": {"old": None, "value": 1.24}},
                    "sources": [{"title": "Test catalog"}],
                    "reason": "Matching variant",
                    "group_key": None,
                }
            ],
        }

    # Run the real claimant deterministically with the fixture's database.
    monkeypatch.setattr(batch_api, "start_worker", lambda _app: None)
    monkeypatch.setattr(batch_worker, "batch_proposal", propose)
    maker = Manufacturer(name="Batch fixture maker")
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(
        manufacturer_id=maker.id,
        designation="Batch PLA",
        material_type="PLA",
        diameter_mm=1.75,
    )
    db_session.add(filament)
    await db_session.commit()
    identity = {
        "id": filament.id,
        "manufacturer_id": maker.id,
        "designation": filament.designation,
        "material_type": filament.material_type,
    }
    previous_token = getattr(filament, "record_token", None)
    stale = await installed.post(
        f"{BASE}/batches",
        json={"mode": "fill", "filament_ids": [filament.id],
              "expected_filaments": [{**identity, "designation": "Old filament"}]},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "filament_selection_changed"
    await db_session.rollback()
    if previous_token is not None:
        assert (await installed.delete(f"/api/v1/filaments/{identity['id']}")).status_code == 204
        replacement = await installed.post("/api/v1/filaments", json={
            **{key: value for key, value in identity.items() if key != "id"},
            "diameter_mm": 2.85,
        })
        assert replacement.status_code == 201, replacement.text
        assert replacement.json()["id"] == identity["id"]
        stale = await installed.post(
            f"{BASE}/batches",
            json={"mode": "fill", "filament_ids": [identity["id"]],
                  "expected_filaments": [{**identity, "record_token": previous_token}]},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["code"] == "filament_selection_changed"
        await db_session.rollback()
        listed = await installed.get("/api/v1/filaments", params={
            "include_record_token": "true", "filament_id": identity["id"],
        })
        assert listed.is_success, listed.text
        identity["record_token"] = next(
            item["record_token"] for item in listed.json()["items"] if item["id"] == identity["id"]
        )
        assert identity["record_token"] and identity["record_token"] != previous_token
    response = await installed.post(
        f"{BASE}/batches",
        json={"mode": "fill", "filament_ids": [identity["id"]],
              "expected_filaments": [identity]},
    )
    assert response.status_code == 200, response.text
    path = f"{BASE}/batches/{response.json()['id']}"
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    assert await batch_worker.run_next_batch_item(sessions)
    [item] = (await installed.get(path)).json()["items"]
    assert item["status"] == "ready"

    selection = {
        "item_id": item["id"],
        "revision": item["revision"],
        "decision_revision": item["decision_revision"],
        "approved_ids": ["density_g_cm3"],
    }
    decision = await installed.post(
        f"{path}/decisions", json={"items": [{**selection, "decision": "approved"}]}
    )
    assert decision.status_code == 200, decision.text
    selection["decision_revision"] = decision.json()["items"][0]["decision_revision"]
    applied = await installed.post(f"{path}/apply", json={"items": [selection]})
    assert applied.status_code == 200, applied.text
    current = await db_session.get(Filament, identity["id"], populate_existing=True)
    assert current.density_g_cm3 == 1.24


async def test_stock_filamentdb_partial_fuzzy_search_is_incomplete(monkeypatch):
    from filabrain.catalog import lookup_filaments

    monkeypatch.delattr(filamentdb_proxy, "lookup_filaments", raising=False)
    monkeypatch.setattr(
        filamentdb_proxy, "search_probe_terms", lambda _search: ["Galaxy", "PLA"]
    )

    async def get(path, params, *, client=None):
        assert path == "/filaments"
        if params["search"] == "Galaxy Ultra PLA":
            return {"items": [], "total": 0}
        if params["search"] == "PLA":
            raise HTTPException(503, "Unavailable")
        return {"items": [{"id": 1, "designation": "Galaxy Ultra PLA"}], "total": 1}

    monkeypatch.setattr(filamentdb_proxy, "_proxy_get", get)
    result = await lookup_filaments("Galaxy Ultra PLA", report_incomplete=True)
    assert [item["id"] for item in result["items"]] == [1]
    assert result["incomplete"] is True


@pytest.mark.parametrize(("designation", "material", "color", "weight", "slug", "title", "label", "sku", "discovered"), [
    ("Black", "ABS", "RAL 9005 Jet Black", 1000, "absblack", "ABS (Black)", "RAL Color: 9005 Signal Black", "852297007060", True),
    ("PLA Basic Pure White", "PLA", "9010 Pure White", 989, "pla-3", "PLA (White)", "RAL Color: 9010 Pure White", "852297007008", False),
    ("RAL 9010 (Pure White)", "PETG", "White", None, "petg-white-1-75mm-1kg-filament", "PETG (White)", "RAL Color: 9010 Pure White", None, False),
    ("TPU 92A Black", "TPU", "000000", 1000, "tpu-black-1-75mm-1kg-filament", "TPU (Black)", "Shore Hardness: 92A", None, True),
    ("Silk Bi-Color PLA Aztec Gold", "PLA", "", 1000, "paramount-3d-aztec-gold", "PLA (Aztec Gold)", "", "GOCOP2SC", True),
    ("ABS (Graphite Gray)", "ABS", "", 1000, "graphite", "ABS (Graphite Gray)", "PANTONE Color: 425C", "GRAY425C", False),
])
async def test_manufacturer_product_from_its_own_catalog(
    installed, db_session, monkeypatch, designation, material, color, weight,
    slug, title, label, sku, discovered,
):
    from filabrain import proposals
    from filabrain.sources import Evidence

    paramount = slug != "graphite"
    site = "https://www.paramount-3d.com" if paramount else "https://manufacturer.example"
    product_url = f"{site}/product-page/{slug}"
    master_url = f"{site}/product-page/abs-master-spool"
    heading = f"{title} 1.75mm 1kg Filament"
    if sku and (sku == "GOCOP2SC" or not paramount):
        heading += f" [{sku}]"
    options = {"designation": heading}
    if sku:
        options["sku"] = sku
    product = Evidence(product_url, heading, heading, options, scopes=(
        heading, *([f"SKU: {sku}"] if sku else []), label,
        'Spool Size: 2.25" ID / 7.75" OD / 2.25" Depth',
        'Bed / Print Temperature: 60-80 C / 220 - 260 C (428 - 500 F)',

    ))

    async def pages(url, **_):
        if url == product_url:
            return product
        return Evidence(url, "ABS (Master Spool) 1.75mm 1kg Filament", "Multi-color kit")

    async def sitemap():
        return (master_url, product_url)

    async def catalog(_identity):
        return [], "", True

    async def ofd(_manufacturer):
        return [], ""

    monkeypatch.setattr(proposals, "fetch_supplier", pages)
    monkeypatch.setattr(proposals, "paramount_product_urls", sitemap, raising=False)
    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.delenv("FILABRAIN_MODEL_URL", raising=False)
    monkeypatch.delenv("FILABRAIN_SEARCH_URL", raising=False)

    maker = Manufacturer(name="Paramount 3D" if paramount else "Fixture Manufacturer", url=site)
    db_session.add(maker)
    await db_session.flush()
    saved_link = None if discovered else product_url
    if paramount and material == "ABS":
        saved_link = master_url
    if sku == "GOCOP2SC":
        saved_link = sku
    filament = Filament(
        manufacturer_id=maker.id, designation=designation, material_type=material,
        manufacturer_color_name=color, diameter_mm=1.75, raw_material_weight_g=weight,
        shop_url=saved_link,
    )
    db_session.add(filament)
    await db_session.commit()
    response = await installed.post(f"{BASE}/proposals", json={"mode": "edit", "filament_id": filament.id})
    assert response.status_code == 200
    data = response.json()
    suggestions = {item["field"]: item for item in data["suggestions"]}
    assert suggestions["spool_outer_diameter_mm"]["value"] == 196.85
    assert suggestions["spool_width_mm"]["value"] == 57.15
    assert suggestions["spool_width_mm"]["sources"][0]["url"] == product_url
    assert "no_match" not in {issue["code"] for issue in data["issues"]}
    if paramount:
        assert suggestions["bed_temp_range_c"]["value"] == {"min": 60, "max": 80}
        assert suggestions["extruder_temp_range_c"]["value"] == {"min": 220, "max": 260}
    else:
        reference = next(issue for issue in data["issues"] if issue["code"] == "manufacturer_color_reference")
        assert reference["name"] == "Pantone 425C"
        assert suggestions["manufacturer_color_name"]["value"] == "Pantone 425C"
        assert suggestions["manufacturer_color_name"]["sources"][0]["url"] == product_url
    if label.startswith("RAL Color:"):
        reference = next(issue for issue in data["issues"] if issue["code"] == "manufacturer_color_reference")
        assert reference["name"] == label.replace("RAL Color:", "RAL")
        assert reference["url"] == product_url
        assert "manufacturer_color_name" not in suggestions
    if material == "TPU":
        assert suggestions["material_subgroup"]["value"] == "92A"

    if sku:
        assert suggestions["manufacturer_sku"]["value"] == sku
        assert suggestions["manufacturer_sku"]["sources"][0]["evidence"] == f"SKU: {sku}"
    if weight is not None:
        assert "raw_material_weight_g" not in suggestions
    assert not proposals._packaged_product(
        proposals.Identity(designation=designation, material=material, color=color, diameter_mm=3.0),
        proposals.replace(product, manufacturer_page=True), None,
    )


@pytest.mark.parametrize(("label", "expected_color"), [
    ("RAL Color: 9010 Pure White", "RAL 9010 Pure White"),
    ("PANTONE Color: 425C", "Pantone 425C"),
])
async def test_paramount_pasted_url_proposes_identity_and_published_specs(
    installed, db_session, monkeypatch, label, expected_color,
):
    from filabrain import proposals
    from filabrain.sources import Evidence

    site = "https://www.paramount-3d.com"
    url = site + "/product-page/pla-3"
    heading = "PLA (White) 1.75mm 1kg Filament"
    page = Evidence(url, heading, heading, {"designation": heading, "sku": "852297007008"}, scopes=(
        heading, "SKU: 852297007008", label,
        'Spool Size: 2.25" ID / 7.75" OD / 2.25" Depth',
        "Bed / Print Temperature: None / 195 - 210 C (383 - 410 F)",
        "Compatible with Bambu AMS",
    ))

    async def fetch(requested, **_):
        assert requested == url
        return page

    async def catalog(_identity):
        return [], "", True

    async def ofd(_manufacturer):
        return [], ""

    monkeypatch.setattr(proposals, "fetch_supplier", fetch)
    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    async def extract(*_args, **_kwargs):
        return [{
            "field": "manufacturer_color_name", "value": "White",
            "sources": [{"url": url, "title": heading, "evidence": "Color: White"}],
        }], []

    monkeypatch.setattr(proposals, "request_extraction", extract)
    monkeypatch.setenv("FILABRAIN_MODEL_URL", "http://model.test/v1")
    monkeypatch.setenv("FILABRAIN_MODEL_ID", "fixture")
    monkeypatch.delenv("FILABRAIN_SEARCH_URL", raising=False)
    maker = Manufacturer(name="Paramount 3D", url=site)
    db_session.add(maker)
    await db_session.commit()

    response = await installed.post(f"{BASE}/proposals", json={"mode": "create", "product_url": url})
    assert response.status_code == 200
    data = response.json()
    values = {item["field"]: item["value"] for item in data["suggestions"]}
    assert values["manufacturer_id"] == maker.id
    assert values["designation"] == "PLA (White)"
    assert values["material_type"] == "PLA"
    assert values["manufacturer_color_name"] == expected_color
    assert values["manufacturer_sku"] == "852297007008"
    assert values["shop_url"] == url
    assert values["spool_width_mm"] == 57.15
    assert values["extruder_temp_range_c"] == {"min": 195, "max": 210}
    assert values["ams_compatibility"] == ["ams"]
    assert "bed_temp_range_c" not in values
    assert "default_spool_weight_g" not in values


@pytest.mark.parametrize("profile_id", [732, 731])
async def test_research_uses_only_the_linked_spool_profile(installed, db_session, monkeypatch, profile_id):
    from filabrain import proposals

    async def profile(path, params=None):
        if path == "/manufacturers":
            return {"items": []}
        assert path == "/spool-profiles/732"
        return {
            "id": profile_id, "is_active": True, "name": "250g Spool", "empty_weight_g": 250.0,
            "outer_diameter_mm": 200.0, "width_mm": 68.0, "spool_material": "Plastic",
        }

    async def catalog(_identity):
        return [], "", True

    async def ofd(_manufacturer):
        return [], ""

    monkeypatch.setattr(filamentdb_proxy, "_proxy_get", profile)
    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.delenv("FILABRAIN_SEARCH_URL", raising=False)
    maker = Manufacturer(name="Bambu Lab")
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(
        manufacturer_id=maker.id, designation="Bambu Green", material_type="PLA",
        diameter_mm=1.75, filamentdb_spool_profile_id=732, default_spool_weight_g=250.0,
    )
    db_session.add(filament)
    await db_session.commit()
    response = await installed.post(f"{BASE}/proposals", json={"mode": "edit", "filament_id": filament.id})
    assert response.status_code == 200
    values = {s["field"]: s for s in response.json()["suggestions"]}
    if profile_id == 732:
        assert values["spool_outer_diameter_mm"]["value"] == 200.0
        assert values["spool_width_mm"]["value"] == 68.0
        assert values["spool_material"]["value"] == "Plastic"
        assert values["spool_width_mm"]["sources"][0]["url"].endswith("/api/v1/spool-profiles/732")
    else:
        assert not values
    assert "default_spool_weight_g" not in values


@pytest.mark.parametrize(("error", "should_archive"), [
    ("HTTP 403 (access denied)", True),
    ("HTTP 429 (rate limited)", True),
    ("Automated access blocked", True),
    ("HTTP 401", False),
])
async def test_blocked_manufacturer_page_can_use_a_dated_capture(
    installed, db_session, monkeypatch, error, should_archive,
):
    from filabrain import proposals
    from filabrain.sources import Evidence

    site = "https://manufacturer.example"
    url = site + "/product"
    archive = f"https://web.archive.org/web/20250101000000/{url}"
    heading = "ABS (Black) 1.75mm 1kg Filament [AB-42]"
    calls = []

    async def fetch(requested, **_):
        assert requested == url
        raise ValueError(error)

    async def archived(requested, **_):
        calls.append(requested)
        return Evidence(archive, heading, heading, {"designation": heading}, scopes=(
            heading, 'Spool Size: 2.25" ID / 7.75" OD / 2.25" Depth',
        ), archived_at="2025-01-01")

    async def catalog(_identity):
        return [], "", True

    async def ofd(_manufacturer):
        return [], ""

    monkeypatch.setattr(proposals, "fetch_supplier", fetch)
    monkeypatch.setattr(proposals, "fetch_archived_supplier", archived)
    monkeypatch.setattr(proposals, "catalog_candidates", catalog)
    monkeypatch.setattr(proposals, "ofd_candidates", ofd)
    monkeypatch.delenv("FILABRAIN_MODEL_URL", raising=False)
    monkeypatch.delenv("FILABRAIN_SEARCH_URL", raising=False)
    maker = Manufacturer(name="Fixture Manufacturer", url=site)
    db_session.add(maker)
    await db_session.flush()
    filament = Filament(
        manufacturer_id=maker.id, designation="ABS (Black)", material_type="ABS",
        diameter_mm=1.75, shop_url=url,
    )
    db_session.add(filament)
    await db_session.commit()

    response = await installed.post(f"{BASE}/proposals", json={"mode": "edit", "filament_id": filament.id})
    assert response.status_code == 200
    result = response.json()
    if should_archive:
        assert calls == [url]
        width = next(s for s in result["suggestions"] if s["field"] == "spool_width_mm")
        assert width["value"] == 57.15
        assert width["sources"][0]["url"] == archive
        assert "2025-01-01" in width["sources"][0]["title"]
        assert "shop_url" not in {s["field"] for s in result["suggestions"]}
    else:
        assert not calls
        assert not result["suggestions"]


async def test_bundle_product_specs_survive_storefront_navigation(monkeypatch):
    from filabrain import proposals, sources

    menu = "<div>Unrelated storefront navigation with links and offers.</div>" * 240
    html = f"""<title>Fixture CMYK Lithophane PLA Bundle</title>
    <nav>{menu}</nav>
    <div role="main">
      <p>This set includes Cyan, Magenta, Yellow, and White PLA filaments.</p>
      <div>Technical Specifications: 1.75mm diameter, Printing Temperature 200-240°C,
        Bed Temperature 25-60°C, Net Weight 1kg per spool</div>
    </div>"""
    monkeypatch.setattr(sources, "_public_address", lambda *_: "203.0.113.10")
    monkeypatch.setattr(sources, "_get_pinned", lambda *_, **_kwargs: (200, "", "text/html", html.encode()))
    page = sources._fetch_supplier("https://manufacturer.example/bundle")
    identity = proposals.Identity(
        designation="PLA CMYK Yellow", material="PLA", color="CMYK Yellow",
        diameter_mm=1.75, weight_g=1000, finish_type="solid", color_mode="single",
    )
    result = {item["field"]: item["value"] for item in proposals._bundle_temperatures(identity, page)}
    assert result == {
        "extruder_temp_range_c": {"min": 200, "max": 240},
        "bed_temp_range_c": {"min": 25, "max": 60},
    }
    assert not proposals._bundle_temperatures(
        identity.model_copy(update={"designation": "PLA CMYK Black", "color": "CMYK Black"}), page,
    )


async def test_catalog_matches_explicit_pla_plus_version_color_and_package(monkeypatch):
    from filabrain import proposals, sources
    from httpx import Response

    def upstream(request):
        if request.url.path.endswith("/brands/index.json"):
            return Response(200, json={"brands": [{"name": "SUNLU", "slug": "sunlu"}]})
        return Response(200, json={
            "filaments": [{"id": "line", "name": "PLA+ 2.0", "material": "PLA", "density": 1.21}],
            "variants": [
                {"id": "black", "filament_id": "line", "name": "Black", "color_hex": "#000000"},
                {"id": "midnight", "filament_id": "line", "name": "Midnight Black", "color_hex": "#111111"},
            ],
            "sizes": [
                {"id": "exact", "variant_id": "black", "diameter": 1.75, "filament_weight": 1000},
                {"id": "small", "variant_id": "black", "diameter": 1.75, "filament_weight": 500},
                {"id": "other", "variant_id": "midnight", "diameter": 1.75, "filament_weight": 1000},
            ],
        })

    monkeypatch.setattr(sources.httpx, "AsyncClient", lambda **kwargs: AsyncClient(
        transport=MockTransport(upstream), **kwargs,
    ))
    items, _ = await sources.ofd_candidates("SUNLU")
    identity = proposals.Identity(
        manufacturer="SUNLU", designation="Black PLA+ 2.0", material="PLA+", color="Black",
        diameter_mm=1.75, weight_g=1000, material_subgroup="2.0", finish_type="solid", color_mode="single",
    )
    matches = [item for item in items if proposals.same_variant(
        identity, proposals._catalog_variant(identity, item), item=item,
    )]
    assert [item["id"] for item in matches] == ["exact"]
    assert proposals.catalog_values(matches[0])["density_g_cm3"] == 1.21
