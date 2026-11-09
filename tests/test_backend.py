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
        version="1.0",
        plugin_type="integration",
        is_active=True,
    )
    db_session.add(plugin)
    await db_session.commit()

    monkeypatch.setattr(plugin_service, "PLUGINS_DIR", ROOT)
    monkeypatch.setattr(app.router, "routes", list(app.router.routes))
    monkeypatch.setattr(app.router, "lifespan_context", app.router.lifespan_context)
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
