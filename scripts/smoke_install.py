"""Install and exercise the ZIP against an unmodified FilaMan backend."""

import argparse
import asyncio
from hashlib import sha256
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile


def release_version(archive: Path) -> str:
    with ZipFile(archive) as zipped:
        return json.loads(zipped.read("filabrain/plugin.json"))["version"]


async def smoke(host: Path, archive: Path, directory: Path, previous: Path | None, live_model: bool) -> None:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{directory / 'filaman.db'}"
    os.environ["PLUGINS_DIR"] = str(directory / "plugins")
    os.environ["MCP_PLUGIN_READ_TOOLS"] = "filabrain-research_filament"
    os.environ["FILABRAIN_SEARCH_URL"] = ""
    if not live_model:
        os.environ["FILABRAIN_MODEL_URL"] = "http://model.test/v1"
        os.environ["FILABRAIN_MODEL_ID"] = "smoke-fixture"
    sys.path.insert(0, str(host / "backend"))

    from app.api.v1.router import mount_plugin_router_on_app
    from app.core.database import async_session_maker, engine
    from app.core.security import generate_token_secret, hash_token
    from app.core.seeds import run_all_seeds
    from app.main import app
    from app.models import AppSettings, Base, Manufacturer, User, UserSession
    from app.services import plugin_service
    from app.services.plugin_catalog import CatalogRelease, fetch_verified_zip
    from app.services.plugin_service import PluginInstallService
    from httpx import ASGITransport, AsyncClient

    plugin_service.DATA_DIR = directory / "data"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_session_maker() as db:
            await run_all_seeds(db)
            user = User(email="plugin-smoke@example.invalid", is_superadmin=True, is_active=True)
            maker = Manufacturer(name="Plugin Smoke Test")
            flags = await db.get(AppSettings, 1)
            if flags is None:
                flags = AppSettings(id=1)
                db.add(flags)
            flags.mcp_enabled = True
            flags.mcp_write_enabled = True
            db.add_all([user, maker])
            await db.flush()
            secret = generate_token_secret()
            session = UserSession(user_id=user.id, session_token_hash=hash_token(secret))
            db.add(session)
            await db.commit()
            installer = PluginInstallService(db)
            if previous:
                old, upgrade = await installer.install_from_zip(previous.read_bytes(), installed_by=user.id)
                assert not upgrade and old.version == release_version(previous)
            bundle = archive.read_bytes()
            version = release_version(archive)
            release = CatalogRelease(
                plugin_key="filabrain", name="FilaBrain", description="Development preview",
                publisher="akira69", repository_url="https://github.com/akira69/filabrain",
                version=version, min_filaman_version="2.2.2", zip_path=f"filabrain-{version}.zip",
                sha256=sha256(bundle).hexdigest(),
            )
            async def local_catalog_zip(url, limit, **_kwargs):
                assert url == release.download_url and len(bundle) < limit
                return bundle
            with patch("app.services.plugin_catalog._download", local_catalog_zip):
                catalog_zip = await fetch_verified_zip(release)
            plugin, upgrade = await installer.install_from_zip(
                catalog_zip, installed_by=user.id, expected_release=("filabrain", version),
            )
            assert upgrade is bool(previous) and plugin.plugin_key == "filabrain" and plugin.version == version

        assert mount_plugin_router_on_app(app, plugin.plugin_key)
        root = "/api/v1/filabrain"
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as browser:
            csrf = generate_token_secret()
            browser.cookies.update({"session_id": f"sess.{session.id}.{secret}", "csrf_token": csrf})
            headers = {"X-CSRF-Token": csrf}
            page = await browser.get("/plugin-page/filabrain")
            assert page.status_code == 200 and "FilaBrain" in page.text
            nav = (await browser.get("/api/v1/plugin-nav")).json()
            assert any(item["plugin_key"] == "filabrain" and item["name"] == "FilaBrain" for item in nav)
            assert (await browser.get(f"{root}/status")).json()["enabled"] is True
            assert (await browser.get(f"{root}/batches")).json() == []
            art = await browser.get(f"{root}/guide-art")
            assert art.status_code == 200 and art.headers["content-type"] == "image/webp"
            assert art.content[8:12] == b"WEBP"

            key = await browser.post(
                "/api/v1/me/api-keys", headers=headers, json={"name": "Plugin smoke test"},
            )
            assert key.status_code == 201
            async with AsyncClient(
                transport=transport, base_url="https://test",
                headers={"Authorization": f"ApiKey {key.json()['token']}"},
            ) as external:
                async def tool(name, arguments):
                    response = await external.post("/mcp", json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    })
                    assert response.status_code == 200, response.text
                    result = response.json()["result"]
                    assert not result.get("isError"), result
                    return json.loads(result["content"][0]["text"])

                created = await tool("create_filament", {
                    "manufacturer_id": maker.id, "designation": "Smoke PETG",
                    "material_type": "PETG", "diameter_mm": 1.75,
                })
                spool = await tool("create_spool", {"filament_id": created["id"]})
                assert (await browser.get(f"/api/v1/spools/{spool['id']}")).json()["filament_id"] == created["id"]

                removed = await external.post(f"{root}/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                })
                assert removed.status_code == 404

            async with async_session_maker() as db:
                flags = await db.get(AppSettings, 1)
                flags.mcp_write_enabled = False
                await db.commit()

            native_headers = {"Authorization": f"ApiKey {key.json()['token']}"}
            async def rpc(method, params=None):
                async with AsyncClient(transport=transport, base_url="https://test", headers=native_headers) as native:
                    response = await native.post("/mcp", json={
                        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
                    })
                assert response.status_code == 200, response.text
                return response.json()["result"]

            tools = {item["name"] for item in (await rpc("tools/list"))["tools"]}
            assert "filabrain-research_filament" in tools and "create_spool" not in tools

            from filabrain import knowledge, proposals, research

            catalog = knowledge.lookup_catalog("Bambu Lab", "PLA Basic Bambu Green", limit=6)
            assert any(row.get("variant") or row.get("color") for row in catalog), catalog
            target = next(row for row in catalog if row.get("packaging") == "refill" and row.get("manufacturer_sku") and row.get("diameter_mm"))

            async def offline(*_args, **_kwargs):
                raise TimeoutError

            async def model_fixture(_address, _model, _key, messages, **_kwargs):
                assert "evidence" in messages[0]["content"]
                return "Compare the available spool with the material guidance. [spool-1]"

            with patch.multiple(
                proposals, fetch_supplier=offline, fetch_archived_supplier=offline,
                supplier_sitemap_urls=offline, catalog_candidates=offline, ofd_candidates=offline,
                search_web=offline, paramount_product_urls=offline, request_extraction=offline,
            ):
                result = await rpc("tools/call", {
                    "name": "filabrain-research_filament",
                    "arguments": {"identity": {
                        "manufacturer": target["maker"], "designation": target["product"] + " Refill",
                        "material": target["material"], "color": target.get("color") or target.get("variant") or "",
                        "sku": target["manufacturer_sku"], "diameter_mm": target["diameter_mm"],
                        "weight_g": target.get("net_weight_g"),
                    }},
                })
                assert not result.get("isError"), result
                report = json.loads(result["content"][0]["text"])["proposal"]
                assert any("bundled" in source["title"] for row in report["suggestions"] for source in row["sources"]), report

            if not live_model:
                original_model = research.request_completion
                research.request_completion = model_fixture
            try:
                response = await browser.post(f"{root}/chat", headers=headers, json={"messages": [{
                    "role": "user", "content": "What do I have for an outdoor bracket? I need 120 g.",
                }]})
                assert response.status_code == 200, response.text
                answer = response.json()
                assert answer["reply"] and answer["required_g"] == 120
                assert any(row["url"] == f"/spools/{spool['id']}" for row in answer["evidence"]), answer
                assert any(row["kind"] in {"material", "guidance"} for row in answer["evidence"]), answer
                print(f"Grounded chat returned {len(answer['evidence'])} evidence records."
                      + (" The model response used the evidence fallback." if "missing_evidence" in answer["notices"] else ""))
            finally:
                if not live_model:
                    research.request_completion = original_model

            async with async_session_maker() as db:
                from sqlalchemy import func, select
                from app.models import Filament, Spool

                assert await db.scalar(select(func.count()).select_from(Filament)) == 1
                assert await db.scalar(select(func.count()).select_from(Spool)) == 1
                if previous:
                    restored, upgrade = await PluginInstallService(db).install_from_zip(previous.read_bytes(), installed_by=user.id)
                    assert upgrade and restored.version == release_version(previous)
                    assert await db.scalar(select(func.count()).select_from(Spool)) == 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("package", type=Path)
    parser.add_argument("--previous", type=Path, help="Optional previous ZIP to test upgrade and rollback")
    parser.add_argument("--live-model", action="store_true", help="Use the configured FILABRAIN_MODEL_URL and FILABRAIN_MODEL_ID")
    args = parser.parse_args()
    checkout, package = (path.resolve(strict=True) for path in (args.checkout, args.package))
    previous = args.previous.resolve(strict=True) if args.previous else None
    with TemporaryDirectory(prefix="filabrain-install-") as scratch:
        os.chdir(scratch)
        asyncio.run(smoke(checkout, package, Path(scratch), previous, args.live_model))
    print("Verified catalog archive, install, offline MCP research, and grounded chat."
          + (" Upgrade and rollback passed." if previous else ""))
