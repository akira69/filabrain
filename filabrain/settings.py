import asyncio
import json
import re
from os import getenv
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from app.api.deps import DBSession, RequirePermission
from app.core.config import settings
from app.core.oidc_crypto import decrypt_secret, encrypt_secret
from app.models import InstalledPlugin
from fastapi import APIRouter, HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StringConstraints,
    field_validator,
)
from sqlalchemy import select, text

from .db import FilamentResearchSettings
from .model import request_extraction
from .sources import DEFAULT_SUPPLIER_DOWNLOAD_MIB, Evidence, search_web

router = APIRouter(tags=["FilaBrain"])
admin = RequirePermission("admin:plugins_manage")
SUPPLIER_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")
EndpointId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9-]{1,64}$")]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150, pattern=r"^\S+$")]
LEGACY_ENDPOINT_ID = "default"


async def lock_research_plugin(db: DBSession) -> bool:
    query = select(InstalledPlugin.is_active).where(InstalledPlugin.plugin_key == "filabrain")
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(text("BEGIN IMMEDIATE"))
    else:
        query = query.with_for_update()
    return bool(await db.scalar(query))


async def mcp_access_enabled(db: DBSession, *, locked: bool = False) -> bool:
    query = select(FilamentResearchSettings.mcp_enabled).where(FilamentResearchSettings.id == 1)
    if locked:
        query = query.with_for_update()
    return await db.scalar(query) is not False


def _model_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    url = urlsplit(value)
    if url.port == 11434 and not (url.path or url.query or url.fragment):
        return f"{value}/v1"
    return value


def _validate_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if value:
        url = urlsplit(value)
        if (
            url.scheme not in ("https", "http")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or "\\" in value
            or any(ch.isspace() or ord(ch) < 32 for ch in value)
            or url.port == 0
        ):
            raise ValueError(
                "Use an HTTP(S) base URL without credentials, query or fragment"
            )
    return value


class SupplierSite(BaseModel):
    domain: str = Field(max_length=254)
    enabled: StrictBool = True

    @field_validator("domain")
    @classmethod
    def normalize_domain(cls, value: str) -> str:
        domain = value.strip().lower().removesuffix(".").removeprefix("www.")
        if not SUPPLIER_DOMAIN.fullmatch(domain):
            raise ValueError("Enter a supplier domain such as example.com")
        return domain


class ModelConnection(BaseModel):
    model_url: str = Field("", max_length=500)
    model_id: str = Field("", max_length=150)
    api_key: SecretStr | None = Field(None, max_length=1000)
    clear_key: bool = False
    endpoint_id: EndpointId | None = None

    validate_url = field_validator("model_url")(_validate_base_url)

    @field_validator("model_url")
    @classmethod
    def normalize_model_url(cls, value: str) -> str:
        return _model_base_url(value)

    @field_validator("model_id")
    @classmethod
    def strip_id(cls, value: str) -> str:
        return value.strip()


class ModelEndpointInput(ModelConnection):
    id: EndpointId
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    model_id: ModelId
    models: list[ModelId] | None = None
    hidden_models: list[ModelId] = Field(default_factory=list)


class SettingsInput(ModelConnection):
    search_url: str = Field("", max_length=500)
    mcp_enabled: StrictBool | None = None
    supplier_download_mib: int = Field(DEFAULT_SUPPLIER_DOWNLOAD_MIB, ge=1, le=32, strict=True)
    supplier_sites: list[SupplierSite] = Field(default_factory=list, max_length=4)
    model_endpoints: list[ModelEndpointInput] | None = None
    default_endpoint_id: EndpointId | None = None

    @field_validator("supplier_sites")
    @classmethod
    def unique_supplier_domains(cls, sites: list[SupplierSite]) -> list[SupplierSite]:
        if len({site.domain for site in sites}) != len(sites):
            raise ValueError("Each supplier domain can only be added once")
        return sites

    validate_search_url = field_validator("search_url")(_validate_base_url)


class McpSettingsInput(BaseModel):
    mcp_enabled: StrictBool


class SearchTestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search_url: str = Field(max_length=500)

    validate_url = field_validator("search_url")(_validate_base_url)


def _deployment_model_url() -> str:
    return _model_base_url(getenv("FILABRAIN_MODEL_URL", ""))


def _deployment_search_url() -> str:
    return getenv("FILABRAIN_SEARCH_URL", "").strip().rstrip("/")


async def config_record(db: DBSession) -> tuple[FilamentResearchSettings | None, dict]:
    row = await db.get(FilamentResearchSettings, 1)
    fallback = {
        "model_url": _deployment_model_url(),
        "model_id": getenv("FILABRAIN_MODEL_ID", "").strip(),
        "search_url": _deployment_search_url(),
        "supplier_sites": [],
        "supplier_download_mib": DEFAULT_SUPPLIER_DOWNLOAD_MIB,
    }
    config = fallback | (row.config or {} if row else {})
    config["model_url"] = _model_base_url(config["model_url"])
    config["search_url"] = config["search_url"] or fallback["search_url"]
    if "model_endpoints" in config:
        default = next((item for item in config["model_endpoints"] if item["id"] == config["default_endpoint_id"]), None)
        config.update({key: (default or {}).get(key, "") for key in ("model_url", "model_id", "encrypted_key")})
        config["id"] = config["default_endpoint_id"]
    return row, config


def endpoints(config: dict) -> list[dict]:
    if "model_endpoints" in config:
        return config["model_endpoints"]
    if not config["model_url"]:
        return []
    return [{
        "id": LEGACY_ENDPOINT_ID, "name": "Default endpoint", "model_url": config["model_url"],
        "model_id": config["model_id"], "models": None, "hidden_models": [],
        "encrypted_key": config.get("encrypted_key"),
    }]


def deployment_key(config: dict) -> str:
    if (config.get("id", LEGACY_ENDPOINT_ID) == LEGACY_ENDPOINT_ID
        and config["model_url"] and config["model_url"] == _deployment_model_url()):
        return getenv("FILABRAIN_API_KEY", "")
    return ""


def model_key(config: dict) -> str:
    if config.get("encrypted_key"):
        return decrypt_secret(config["encrypted_key"])
    return deployment_key(config)


def public_config(config: dict, mcp_enabled: bool) -> dict:
    model_endpoints = endpoints(config)
    return {
        "model_url": config["model_url"],
        "model_id": config["model_id"],
        "search_url": config["search_url"],
        "supplier_sites": config["supplier_sites"],
        "supplier_download_mib": config["supplier_download_mib"],
        "mcp_enabled": mcp_enabled,
        "has_key": bool(config.get("encrypted_key") or deployment_key(config)),
        "default_endpoint_id": config.get("default_endpoint_id", model_endpoints[0]["id"] if model_endpoints else None),
        "model_endpoints": [
            {key: item[key] for key in ("id", "name", "model_url", "model_id", "models", "hidden_models")}
            | {"has_key": bool(item.get("encrypted_key") or deployment_key(item))}
            for item in model_endpoints
        ],
    }


@router.get("/settings")
async def read_settings(db: DBSession, _principal=admin):
    _, config = await config_record(db)
    return public_config(config, await mcp_access_enabled(db))


@router.put("/settings")
async def save_settings(data: SettingsInput, db: DBSession, _principal=admin):
    if data.model_fields_set == {"mcp_enabled"}:
        raise HTTPException(422, "Use the MCP settings endpoint to change MCP access")
    await lock_research_plugin(db)
    row, old = await config_record(db)
    config = dict(row.config or {}) if row else {}
    for key in ("search_url", "supplier_sites", "supplier_download_mib"):
        if key in data.model_fields_set:
            config[key] = data.model_dump(include={key})[key]
    if data.model_endpoints is not None:
        previous = {item["id"]: item for item in endpoints(old)}
        entries = {}
        for item in data.model_endpoints:
            if item.id in entries or not item.model_url:
                raise HTTPException(422, "Each endpoint needs a unique ID and a model server URL")
            entries[item.id] = item.model_dump(exclude={"api_key", "clear_key", "endpoint_id"}) | {
                "models": None if item.id == LEGACY_ENDPOINT_ID and item.models is None else sorted(set(item.models or [])),
                "hidden_models": sorted(set(item.hidden_models)),
                "encrypted_key": saved_key(item, previous.get(item.id, {})),
            }
        default = entries.get(data.default_endpoint_id)
        if (entries and not default) or (not entries and data.default_endpoint_id):
            raise HTTPException(422, "Choose a default endpoint")
        if default and default["model_id"] in default["hidden_models"]:
            raise HTTPException(422, "The default model must be visible")
        config["model_endpoints"] = list(entries.values())
        config["default_endpoint_id"] = data.default_endpoint_id
        # Named endpoints are the source of truth once saved.
        for key in ("model_url", "model_id", "encrypted_key", "id"):
            config.pop(key, None)
    elif "model_endpoints" not in old and data.model_fields_set.intersection({"model_url", "model_id", "api_key", "clear_key"}):
        if bool(data.model_id) != bool(data.model_url):
            raise HTTPException(422, "Enter both a model URL and a model ID")
        config.update(model_url=data.model_url, model_id=data.model_id, encrypted_key=saved_key(data, old))
    if row is None:
        row = FilamentResearchSettings(id=1)
        db.add(row)
    # Keep the deployment default out of storage so changing Compose takes effect.
    if config.get("search_url") == _deployment_search_url():
        config["search_url"] = ""
    row.config = config
    await db.commit()
    _, config = await config_record(db)
    return public_config(config, await mcp_access_enabled(db))


def saved_key(data: ModelConnection, old: dict) -> str | None:
    if data.api_key and data.api_key.get_secret_value() and not data.clear_key:
        if not settings.oidc_enc_key:
            raise HTTPException(
                422, "Configure OIDC_ENC_KEY or use the environment-backed model key"
            )
        if not data.model_url:
            raise HTTPException(422, "Enter the model URL before saving a key")
        return encrypt_secret(data.api_key.get_secret_value())
    if not data.clear_key and data.model_url == old.get("model_url"):
        return old.get("encrypted_key")
    return None


@router.put("/settings/mcp")
async def save_mcp_settings(data: McpSettingsInput, db: DBSession, _principal=admin):
    await lock_research_plugin(db)
    row = await db.get(FilamentResearchSettings, 1, with_for_update=True)
    if row is None:
        row = FilamentResearchSettings(id=1)
        db.add(row)
    row.mcp_enabled = data.mcp_enabled
    await db.commit()
    return data


def _connection_key(data: ModelConnection, old: dict) -> str:
    key = data.api_key.get_secret_value() if data.api_key else ""
    if key or data.clear_key:
        return key
    if data.endpoint_id:
        old = next((item for item in endpoints(old) if item["id"] == data.endpoint_id), {})
    if data.model_url == old.get("model_url"):
        return model_key(old)
    return deployment_key({"model_url": data.model_url, "id": data.endpoint_id or LEGACY_ENDPOINT_ID})


async def model_ids(url: str, key: str) -> list[str]:
    try:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with (
            httpx.AsyncClient(timeout=5, trust_env=False) as client,
            client.stream("GET", f"{url}/models", headers=headers) as response,
        ):
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 512 * 1024:
                    raise ValueError("Model list too large")
        payload = json.loads(raw)
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise TypeError("Invalid model list")
    except (httpx.HTTPError, ValueError, RuntimeError, TypeError):
        raise HTTPException(
            502, "Could not load models from this server. Check the URL or enter the model ID manually."
        ) from None
    return sorted({
        entry["id"] for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        and 0 < len(entry["id"]) <= 150 and not any(ch.isspace() for ch in entry["id"])
    })


async def visible_models(endpoint: dict) -> list[str]:
    models = endpoint["models"]
    if models is None:
        models = await model_ids(endpoint["model_url"], model_key(endpoint))
    hidden = set(endpoint["hidden_models"])
    return [model for model in models if model not in hidden]


async def model_group(endpoint: dict) -> dict:
    unavailable = False
    try:
        async with asyncio.timeout(6):
            models = await visible_models(endpoint)
    except (HTTPException, TimeoutError, ValueError, RuntimeError):
        unavailable = True
        models = []
    preferred = endpoint["model_id"]
    return {"id": endpoint["id"], "name": endpoint["name"], "models": list(dict.fromkeys([
        *([preferred] if preferred and preferred not in endpoint["hidden_models"] else []), *models,
    ])), "unavailable": unavailable}


async def resolve_model(config: dict, requested: str | None, endpoint_id: str | None = None) -> tuple[dict, str]:
    if endpoint_id is None and requested in (None, config["model_id"]):
        return config, config["model_id"]
    candidates = endpoints(config)
    selected = endpoint_id or config.get("default_endpoint_id", LEGACY_ENDPOINT_ID)
    endpoint = next((item for item in candidates if item["id"] == selected), None)
    if not endpoint or not requested or requested in endpoint["hidden_models"]:
        raise HTTPException(422, "Select a visible model on a configured endpoint")
    if requested != endpoint["model_id"] and requested not in await visible_models(endpoint):
        raise HTTPException(422, "Select a model available on the configured endpoint")
    return endpoint, requested


@router.post("/settings/models")
async def list_models(data: ModelConnection, db: DBSession, _principal=admin):
    if not data.model_url:
        raise HTTPException(422, "Enter the model server URL")
    _, old = await config_record(db)
    try:
        key = _connection_key(data, old)
    except (ValueError, RuntimeError):
        raise HTTPException(502, "Could not read the model server key") from None
    return {"models": await model_ids(data.model_url, key)}


@router.post("/settings/test")
async def test_model(data: ModelConnection, db: DBSession, _principal=admin):
    _, old = await config_record(db)
    if not data.model_url or not data.model_id:
        raise HTTPException(422, "Enter both a model URL and a model ID")
    try:
        key = _connection_key(data, old)
        suggestions, _ = await request_extraction(
            data.model_url,
            data.model_id,
            key,
            {"designation": "Fixture PLA"},
            [
                Evidence(
                    "https://example.invalid/fixture",
                    "Fixture PLA",
                    "Fixture PLA density: 1.24 g/cm³",
                )
            ],
        )
        if not any(
            item["field"] == "density_g_cm3" and item["value"] == 1.24
            for item in suggestions
        ):
            raise ValueError("Model did not return a supported value")
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, RuntimeError):
        raise HTTPException(
            502,
            "The model did not return a usable sample. Check the connection and model ID.",
        ) from None
    return {"ok": True}


@router.post("/settings/test-search")
async def test_search(data: SearchTestInput, _principal=admin):
    if not data.search_url:
        raise HTTPException(422, "Enter the SearXNG URL")
    try:
        if not await search_web(data.search_url, "3D printing filament"):
            raise ValueError("No results returned")
    except (httpx.HTTPError, TimeoutError, ValueError, AttributeError, TypeError):
        raise HTTPException(502, "SearXNG returned no usable results. Check the URL, JSON output, and search engines.") from None
    return {"ok": True}
