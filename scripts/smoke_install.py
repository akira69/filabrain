"""Install and exercise the ZIP against an unmodified FilaMan backend."""

import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


async def smoke(host: Path, archive: Path, directory: Path) -> None:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{directory / 'filaman.db'}"
    os.environ["PLUGINS_DIR"] = str(directory / "plugins")
    sys.path.insert(0, str(host / "backend"))

    from app.api.v1.router import mount_plugin_router_on_app
    from app.core.database import async_session_maker, engine
    from app.core.security import generate_token_secret, hash_token
    from app.core.seeds import run_all_seeds
    from app.main import app
    from app.models import Base, Manufacturer, User, UserSession
    from app.services.plugin_service import PluginInstallService
    from httpx import ASGITransport, AsyncClient

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_session_maker() as db:
            await run_all_seeds(db)
            user = User(email="plugin-smoke@example.invalid", is_superadmin=True, is_active=True)
            maker = Manufacturer(name="Plugin Smoke Test")
            db.add_all([user, maker])
            await db.flush()
            secret = generate_token_secret()
            session = UserSession(user_id=user.id, session_token_hash=hash_token(secret))
            db.add(session)
            await db.commit()
            plugin, upgrade = await PluginInstallService(db).install_from_zip(
                archive.read_bytes(), installed_by=user.id,
            )
            assert not upgrade and plugin.plugin_key == "filabrain"

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

            key = await browser.post(
                "/api/v1/me/api-keys", headers=headers, json={"name": "Plugin smoke test"},
            )
            assert key.status_code == 201
            async with AsyncClient(
                transport=transport, base_url="https://test",
                headers={"Authorization": f"Bearer {key.json()['token']}"},
            ) as external:
                async def tool(name, arguments):
                    response = await external.post(f"{root}/mcp", json={
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    })
                    assert response.status_code == 200, response.text
                    result = response.json()["result"]
                    assert not result.get("isError"), result
                    return result["structuredContent"]

                created = await tool("create_filament", {
                    "manufacturer_id": maker.id, "designation": "Smoke PETG",
                    "material_type": "PETG", "diameter_mm": 1.75,
                })
                spool = await tool("create_spool", {"filament_id": created["id"]})
                assert (await browser.get(f"/api/v1/spools/{spool['id']}")).json()["filament_id"] == created["id"]

                off = await browser.put(f"{root}/settings/mcp", headers=headers, json={"mcp_enabled": False})
                assert off.json() == {"mcp_enabled": False}
                disabled = await external.post(f"{root}/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                })
                assert disabled.status_code == 404
                assert (await browser.get(f"{root}/status")).json()["enabled"] is True
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python scripts/smoke_install.py FILAMAN_CHECKOUT PLUGIN_ZIP")
    checkout, package = (Path(path).resolve(strict=True) for path in sys.argv[1:])
    with TemporaryDirectory(prefix="filabrain-install-") as scratch:
        os.chdir(scratch)
        asyncio.run(smoke(checkout, package, Path(scratch)))
    print("Installed plugin and verified sidebar, standalone MCP creation, and the off switch.")
