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
from httpx import ASGITransport, AsyncClient, MockTransport
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker
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


async def test_research_deadline_preserves_verified_datasheet(installed, db_session, monkeypatch):
    import asyncio
    from filabrain import proposals

    async def catalog(_identity):
        return [], "", True

    async def fetch(url):
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
    response = await installed.post(
        f"{BASE}/batches",
        json={"mode": "fill", "filament_ids": [filament.id]},
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
    await db_session.refresh(filament)
    assert filament.density_g_cm3 == 1.24


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

    async def pages(url):
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

    async def fetch(requested):
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

    async def fetch(requested):
        assert requested == url
        raise ValueError(error)

    async def archived(requested):
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
    monkeypatch.setattr(sources, "_get_pinned", lambda *_: (200, "", "text/html", html.encode()))
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
