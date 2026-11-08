import asyncio
import re
from typing import Annotated, Literal
from urllib.parse import urlencode

import httpx
from app.api.deps import DBSession, PrincipalDep, ensure_any_permission
from app.models import Filament, InstalledPlugin
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .model import request_completion
from .proposals import (
    Identity,
    Text,
    batch_proposal,
    filament_identity,
    filament_snapshot,
    identity_matches,
    research_products,
)
from .settings import (
    config_record,
    mcp_access_enabled,
    model_key,
)

router = APIRouter(tags=["FilaBrain"])
PLUGIN_KEY = "filabrain"
CHAT_PAGES = {
    "new_filament": ("/filaments/new", "filaments:create"),
    "new_spool": ("/filaments?select_for_spool=1", "spools:create"),
    "filaments": ("/filaments", "filaments:read"),
    "research_batches": ("/filaments/research", "filaments:update"),
    "model_settings": ("/admin/system/filabrain", "admin:plugins_manage"),
}
PAGE_MARKER = re.compile(r"(?m)^\[\[open:([a-z_]+)(?:\|([^\]\r\n]{1,255}))?\]\][ \t]*$")


class ProposalRequest(BaseModel):
    mode: Literal["create", "edit"]
    filament_id: Annotated[int, Field(gt=0)] | None = None
    identity: Identity | None = None
    product_url: Annotated[str, Field(max_length=2048)] | None = None
    sku_hint: Text = ""
    guidance: Annotated[str, Field(max_length=400)] = ""
    action: Literal["fill", "standardize"] = "fill"


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class ChatRequest(BaseModel):
    messages: Annotated[list[ChatMessage], Field(min_length=1, max_length=11)]


async def is_enabled(db: DBSession) -> bool:
    return bool(
        await db.scalar(
            select(InstalledPlugin.is_active).where(
                InstalledPlugin.plugin_key == PLUGIN_KEY
            )
        )
    )


@router.get("/status")
async def research_status(db: DBSession, principal: PrincipalDep):
    enabled = await is_enabled(db)
    permissions = set()
    if enabled or principal.auth_type == "session":
        try:
            permissions = await ensure_any_permission(
                db,
                principal,
                "filaments:create",
                "filaments:update",
                "filaments:read",
                "admin:plugins_manage",
            )
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
    can_chat = enabled and principal.auth_type == "session" and "filaments:read" in permissions
    chat_model = None
    if enabled and principal.auth_type == "session" and (
        can_chat or permissions.intersection({"filaments:create", "filaments:update"})
    ):
        _, config = await config_record(db)
        chat_model = (config["model_id"] or None) if config["model_url"] else None
    return {
        "enabled": enabled,
        "mcp_enabled": enabled and await mcp_access_enabled(db),
        "can_create": enabled and "filaments:create" in permissions,
        "can_update": "filaments:update" in permissions,
        "can_configure": enabled and "admin:plugins_manage" in permissions,
        "can_chat": can_chat,
        "chat_model": chat_model,
    }


@router.post("/chat")
async def research_chat(data: ChatRequest, db: DBSession, principal: PrincipalDep):
    if not await is_enabled(db):
        raise HTTPException(404, "FilaBrain is disabled")
    if principal.auth_type != "session":
        raise HTTPException(403, "A browser session is required")
    permissions = await ensure_any_permission(db, principal, *(permission for _, permission in CHAT_PAGES.values()))
    if "filaments:read" not in permissions:
        raise HTTPException(403, "Filament read access is required")
    allowed_pages = {name: url for name, (url, permission) in CHAT_PAGES.items() if permission in permissions}
    if len(data.messages) % 2 == 0 or any(
        message.role != ("assistant" if index % 2 else "user")
        or (message.role == "user" and len(message.content) > 2000)
        for index, message in enumerate(data.messages)
    ):
        raise HTTPException(422, "Expected a conversation ending in a user question")
    _, config = await config_record(db)
    if not (config["model_url"] and config["model_id"]):
        raise HTTPException(503, "Configure a model in FilaBrain settings")
    await db.rollback()
    messages = [
        {"role": "system", "content": (
            "You are the FilaMan assistant. Help with general questions, especially 3D printing, "
            "filament and FilaMan workflows. You can only see this conversation. "
            "Do not claim to have searched the web, inspected inventory, opened a page, or changed records. "
            "When the latest user asks to open a FilaMan page or start adding a filament or spool, "
            "offer the page in your reply and append one marker on its own final line, such as [[open:new_filament]]. "
            f"Allowed page names: {', '.join(allowed_pages)}. "
            "If opening new_filament for a product the user named, use [[open:new_filament|product name]] "
            "with the supplied name. The marker gives the user a link to open the page. "
            "Leave out the marker for ordinary questions."
        )},
        *(message.model_dump() for message in data.messages),
    ]
    try:
        async with asyncio.timeout(50):
            answer = await request_completion(
                config["model_url"], config["model_id"], model_key(config), messages, timeout=45,
            )
        if not answer.strip():
            raise ValueError("Empty model response")
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError, RuntimeError):
        raise HTTPException(502, "The configured model could not answer. Try again.") from None
    action = None
    if marker := PAGE_MARKER.search(answer):
        page, name = marker.groups()
        if url := allowed_pages.get(page):
            if page == "new_filament" and name and name.strip():
                url += "?" + urlencode({"designation": name.strip()})
            action = {"page": page, "url": url}
        answer = PAGE_MARKER.sub("", answer).strip()
    if not answer.strip():
        answer = "Select the page to continue." if action else "That page is unavailable."
    return {"reply": answer.strip()[:8000], "action": action}


async def get_edit_filament(db: DBSession, filament_id: int) -> Filament:
    filament = await db.scalar(
        select(Filament)
        .where(Filament.id == filament_id)
        .options(selectinload(Filament.manufacturer))
    )
    if filament is None:
        raise HTTPException(404, "Filament not found")
    return filament


async def research(db: DBSession, data: ProposalRequest) -> dict:
    filament = None
    if data.mode == "edit":
        if data.filament_id is None:
            raise HTTPException(422, "An existing filament is required")
        filament = await get_edit_filament(db, data.filament_id)
        identity = filament_identity(filament)
        if data.identity is not None and not identity_matches(data.identity, identity):
            raise HTTPException(
                409, "The filament identity changed. Save or reload before researching."
            )
    else:
        if data.filament_id is not None:
            raise HTTPException(422, "A new draft cannot target an existing filament")
        identity = data.identity or Identity()
        if not (
            identity.designation.strip()
            or identity.sku.strip()
            or data.product_url
            or data.sku_hint.strip()
        ):
            raise HTTPException(422, "Enter a product name, SKU or URL")

    if data.sku_hint.strip() and identity.sku.strip() and (
        identity.sku.strip().casefold() != data.sku_hint.strip().casefold()
    ):
        raise HTTPException(409, "The SKU differs from the filament in the editor")
    target = {"mode": data.mode, "filament_id": data.filament_id, "identity": identity}
    if filament is None:
        if data.action != "fill":
            raise HTTPException(422, "Standardization requires an existing filament")
        return {
            "target": target,
            **await research_products(
                db, identity, product_url=data.product_url, sku_hint=data.sku_hint, guidance=data.guidance,
            ),
        }
    snapshot = filament_snapshot(filament)
    result = await batch_proposal(
        db, snapshot, data.action,
        sku_hint=data.sku_hint, product_url=data.product_url, guidance=data.guidance, release_read=False,
    )
    if data.action == "standardize":
        return {
            "target": target, "suggestions": [], **result,
            "standardization_context": {
                field: snapshot[field] for field in ("material_subgroup", "finish_type", "color_mode")
            },
        }
    return {
        "target": target,
        "suggestions": [
            {
                "field": field, "value": change["value"],
                "sources": [
                    {"url": source.get("url"), "title": source["title"], "evidence": source["quote"]}
                    for source in unit["sources"]
                ],
            }
            for unit in result["proposals"] for field, change in unit["fields"].items()
        ],
        "issues": [
            {**issue, "code": "no_match" if issue["code"] == "needs_match" else issue["code"]}
            for issue in result["issues"]
        ],
    }


@router.post("/proposals")
async def research_proposal(
    data: ProposalRequest, db: DBSession, principal: PrincipalDep
):
    if not await is_enabled(db):
        raise HTTPException(404, "FilaBrain is disabled")
    await ensure_any_permission(
        db, principal, "filaments:update" if data.mode == "edit" else "filaments:create"
    )
    return await research(db, data)
