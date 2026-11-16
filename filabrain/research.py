import asyncio
import re
from typing import Annotated, Literal
from urllib.parse import urlencode

import httpx
from app.api.deps import DBSession, PrincipalDep, ensure_any_permission
from app.api.v1.schemas_filament import FilamentCreate
from app.core.cache import response_cache
from app.core.db_utils import get_next_available_id
from app.core.event_bus import event_bus
from app.models import Filament, FilamentColor, InstalledPlugin, Manufacturer
from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, StrictBool, StringConstraints, ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from .catalog import linked_spool_card
from .chat_context import NAVIGATION, collect_context, lookup_live
from .db import FilamentResearchSave
from .model import request_completion
from .proposals import (
    Identity,
    Text,
    batch_proposal,
    duplicate_ids,
    filament_identity,
    filament_snapshot,
    identity_matches,
    manufacturer_site_url,
    research_products,
    snapshot_identity,
)
from .settings import (
    EndpointId,
    ModelId,
    check_model_catalog,
    config_record,
    default_endpoint_id,
    endpoints,
    lock_research_plugin,
    model_group,
    model_key,
    select_model,
)
from .spool_profiles import (
    DIMENSION_FIELDS, LOOKUP_WORDS, PACKAGE_WEIGHT, STYLE_CUES, find_spool_profiles, spool_follow_up, spool_intent,
    spool_profile_result,
)
from .weights import grams_from_match

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
SELECTED_SPOOL = re.compile(r"\b(?:my|this|(?:the\s+)?selected)\s+(?:spool|reel|bobbin|filament)\b", re.I)
SELECTED_SPOOL_WORDS = LOOKUP_WORDS | {"chose", "chosen", "its", "picked", "selected"}
SpoolProfileId = Annotated[str, StringConstraints(min_length=1, max_length=256)]
RecordToken = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
DraftId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9-]+$")]


class ProposalRequest(BaseModel):
    mode: Literal["create", "edit"]
    filament_id: Annotated[int, Field(gt=0)] | None = None
    identity: Identity | None = None
    palette_color_id: Annotated[int, Field(gt=0, le=2_147_483_647, strict=True)] | None = None
    record_token: RecordToken | None = None
    product_url: Annotated[str, Field(max_length=2048)] | None = None
    reference_url: Annotated[str, Field(max_length=2048)] | None = None
    sku_hint: Text = ""
    guidance: Annotated[str, Field(max_length=400)] = ""
    action: Literal["fill", "standardize"] = "fill"
    spool_lookup: StrictBool = False
    spool_package_weight_g: Annotated[float, Field(ge=1, le=50_000)] | None = None
    model_id: ModelId | None = None
    endpoint_id: EndpointId | None = None


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    confirmed_spool_profile_id: SpoolProfileId | None = None
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class ManufacturerWebsite(BaseModel):
    manufacturer_id: Annotated[int, Field(gt=0)]
    name: Text
    url: Annotated[str, Field(max_length=500)]
    record_token: RecordToken | None = None


class ChatRequest(BaseModel):
    messages: Annotated[list[ChatMessage], Field(min_length=1, max_length=11)]
    selected_spool_profile_id: SpoolProfileId | None = None
    spool_lookup_message_index: Annotated[int, Field(ge=1, le=9, strict=True)] | None = None
    spool_lookup_filament_id: Annotated[int, Field(gt=0, le=2_147_483_647, strict=True)] | None = None
    model_id: ModelId | None = None
    endpoint_id: EndpointId | None = None
    filament_id: Annotated[int, Field(gt=0, le=2_147_483_647)] | None = None
    printer_id: Annotated[int, Field(gt=0, le=2_147_483_647)] | None = None
    nozzle_mm: Annotated[float, Field(gt=0, le=2, allow_inf_nan=False)] | None = None
    web_search: StrictBool = False


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
                "manufacturers:update",
                "spools:create",
                "spools:read",
                "printers:read",
                "admin:plugins_manage",
            )
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
    can_chat = enabled and principal.auth_type == "session" and "filaments:read" in permissions
    chat_model = None
    endpoint_id = None
    if enabled and principal.auth_type == "session" and (
        can_chat or permissions.intersection({"filaments:create", "filaments:update"})
    ):
        _, config = await config_record(db)
        chat_model = (config["model_id"] or None) if config["model_url"] else None
        endpoint_id = default_endpoint_id(config)
    return {
        "enabled": enabled,
        "can_create": enabled and "filaments:create" in permissions,
        "can_add_spool": enabled and principal.auth_type == "session"
        and {"filaments:read", "spools:create"} <= permissions,
        "can_read_spools": enabled and principal.auth_type == "session" and "spools:read" in permissions,
        "can_read_printers": enabled and principal.auth_type == "session" and "printers:read" in permissions,
        "can_update": "filaments:update" in permissions,
        "can_update_manufacturer": enabled and principal.auth_type == "session"
        and {"filaments:update", "manufacturers:update"} <= permissions,
        "can_configure": enabled and "admin:plugins_manage" in permissions,
        "can_chat": can_chat,
        "chat_model": chat_model,
        "chat_endpoint_id": endpoint_id,
    }


def selected_spool_intent(question: str) -> str | None:
    if set(re.findall(r"\w+", question.lower())) <= SELECTED_SPOOL_WORDS:
        return spool_intent(question + " spool")
    return None


@router.post("/chat")
async def research_chat(data: ChatRequest, db: DBSession, principal: PrincipalDep):
    if not await is_enabled(db):
        raise HTTPException(404, "FilaBrain is disabled")
    if principal.auth_type != "session":
        raise HTTPException(403, "A browser session is required")
    permissions = await ensure_any_permission(
        db, principal, "spools:read", "printers:read", *(permission for _, permission in CHAT_PAGES.values()),
    )
    if "filaments:read" not in permissions:
        raise HTTPException(403, "Filament read access is required")
    allowed_pages = {name: url for name, (url, permission) in CHAT_PAGES.items() if permission in permissions}
    if len(data.messages) % 2 == 0 or any(
        message.role != ("assistant" if index % 2 else "user")
        or (message.role == "user" and len(message.content) > 2000)
        for index, message in enumerate(data.messages)
    ):
        raise HTTPException(422, "Expected a conversation ending in a user question")
    lookup_index = data.spool_lookup_message_index
    if lookup_index is not None and (lookup_index >= len(data.messages) - 1 or lookup_index % 2 == 0):
        raise HTTPException(422, "The spool selection must reference an assistant message")
    _, config = await config_record(db)
    if not data.endpoint_id and not (config["model_url"] and config["model_id"]):
        raise HTTPException(503, "Configure a model in FilaBrain settings")
    question = data.messages[-1].content
    if not NAVIGATION.search(question):
        selected_id = data.selected_spool_profile_id
        selected_intent = selected_spool_intent(question) if selected_id and len(data.messages) >= 3 else None
        lookup_messages = data.messages
        filament_id = data.filament_id
        if selected_intent:
            if "spool_lookup_filament_id" in data.model_fields_set:
                filament_id = data.spool_lookup_filament_id
            lookup_end = lookup_index if lookup_index is not None else len(data.messages) - 2
            while (
                lookup_end >= 3
                and data.messages[lookup_end].confirmed_spool_profile_id == selected_id
                and selected_spool_intent(data.messages[lookup_end - 1].content)
            ):
                lookup_end -= 2
            lookup_messages = data.messages[:lookup_end]
            question = lookup_messages[-1].content
        intent = spool_intent(question)
        refinements = []
        for previous in (-3, -5):
            if intent or NAVIGATION.search(question) or len(lookup_messages) < -previous or not spool_follow_up(
                question, lookup_messages[previous + 1].content,
            ):
                break
            refinements.insert(0, question)
            question = lookup_messages[previous].content
            intent = spool_intent(question)
        if intent and not NAVIGATION.search(question):
            refinement = " ".join(refinements)
            profile_id = None
            package_weight_g = None
            search_identity = {}
            if filament_id is not None:
                filament = await db.get(Filament, filament_id, options=(selectinload(Filament.manufacturer),))
                if filament and linked_spool_question(question, filament):
                    saved = filament_identity(filament)
                    search_identity = {
                        "manufacturer_name": saved.manufacturer, "product_name": saved.designation,
                        "material_name": saved.material, "sku": saved.sku,
                    }
                    question = f"{saved.manufacturer} {saved.designation} {question}"
                    if not PACKAGE_WEIGHT.search(refinement):
                        profile_id = getattr(filament, "filamentdb_spool_profile_id", None)
                        package_weight_g = saved.weight_g
            await db.rollback()
            result = await linked_spool_result(profile_id, intent) or await find_spool_profiles(
                question, intent, refinement, package_weight_g=package_weight_g, **search_identity,
            )
            if not selected_intent:
                return result
            for profile in result["spool_profiles"]:
                if profile["id"] == selected_id:
                    return {**spool_profile_result(profile, selected_intent), "selected_spool_profile_id": selected_id}
        if selected_intent:
            return {"reply": "I couldn’t verify the selected spool for this question.", "action": None}
    context = await collect_context(
        db, data.messages[-1].content, permissions, filament_id=data.filament_id,
        printer_id=data.printer_id, nozzle_mm=data.nozzle_mm, web_search=data.web_search,
        previous_questions=[message.content for message in reversed(data.messages[:-1:2])],
    )
    await db.rollback()
    if context and "ambiguous_weight" in context.notices:
        return context.response("")
    endpoint, model = select_model(config, data.model_id, data.endpoint_id)
    await check_model_catalog(endpoint, model)
    if context:
        await lookup_live(context, config["search_url"], config["supplier_download_mib"])
    messages = [
        {"role": "system", "content": context.prompt() if context else (
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
        *({"role": message.role, "content": message.content} for message in data.messages),
    ]
    try:
        async with asyncio.timeout(50):
            answer = await request_completion(
                endpoint["model_url"], model, model_key(endpoint), messages, timeout=45,
            )
        if not answer.strip():
            raise ValueError("Empty model response")
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError, RuntimeError):
        if context:
            return context.response("")
        raise HTTPException(502, "The configured model could not answer. Try again.") from None
    if context:
        return context.response(answer)
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


def linked_spool_question(question: str, filament: Filament) -> bool:
    for match in PACKAGE_WEIGHT.finditer(question):
        if (grams := grams_from_match(match)) is None or grams != filament.raw_material_weight_g:
            return False
    question = PACKAGE_WEIGHT.sub(" ", question)
    if SELECTED_SPOOL.search(question):
        question = SELECTED_SPOOL.sub(" ", question)
    else:
        names = [filament.manufacturer.name.strip() if filament.manufacturer else "", filament.designation.strip()]
        if not all(names):
            return False
        labels = [rf"(?<!\w){re.escape(name)}(?!\w)" for name in sorted(names, key=len, reverse=True)]
        if not all(re.search(label, question, re.I) for label in labels):
            return False
        question = re.sub("|".join(labels), " ", question, flags=re.I)
    return set(re.findall(r"\w+", question.lower())) <= LOOKUP_WORDS


async def linked_spool_result(profile_id: int | None, intent: str) -> dict | None:
    if not (profile := await linked_spool_card(profile_id)):
        return None
    dimensions = [f"{profile[key]:g} mm {label}" for key, label in DIMENSION_FIELDS if key in profile]
    specs = [f'{profile["empty_weight_g"]:g} g empty'] if "empty_weight_g" in profile else []
    specs.extend(dimensions)
    reply = f'The selected filament links to FilaManDB’s {profile["title"]} profile.'
    if specs:
        reply += f' It lists {", ".join(specs)}.'
    if intent == "visual" and "photo" not in profile:
        reply += " A photo of this design is unavailable."
    if intent == "dimensions" and not dimensions:
        reply += " The listing gives no dimensions."
    return {"reply": reply, "action": None, "spool_profiles": [profile]}


@router.get("/models")
async def research_models(db: DBSession, principal: PrincipalDep):
    if not await is_enabled(db):
        raise HTTPException(404, "FilaBrain is disabled")
    if principal.auth_type != "session":
        raise HTTPException(403, "A browser session is required")
    await ensure_any_permission(db, principal, "filaments:read", "filaments:create", "filaments:update")
    _, config = await config_record(db)
    await db.rollback()
    saved = endpoints(config)
    # Saved catalogs are local data. Legacy and manual-only endpoints may need discovery.
    groups = await asyncio.gather(*(model_group(endpoint) for endpoint in saved))
    default_id = config.get("default_endpoint_id", saved[0]["id"] if saved else None)
    default_group = next((group for group in groups if group["id"] == default_id), None)
    return {"groups": groups, "default_endpoint_id": default_id,
            "default_model_id": (config["model_id"] or None) if config["model_url"] else None,
            "models": (default_group or {}).get("models", [])}


async def get_edit_filament(db: DBSession, filament_id: int) -> Filament:
    filament = await db.scalar(
        select(Filament)
        .where(Filament.id == filament_id)
        .options(selectinload(Filament.manufacturer))
    )
    if filament is None:
        raise HTTPException(404, "Filament not found")
    return filament


async def research(db: DBSession, data: ProposalRequest, *, deadline: float | None = None) -> dict:
    filament = None
    if data.mode == "edit":
        if data.filament_id is None:
            raise HTTPException(422, "An existing filament is required")
        filament = await get_edit_filament(db, data.filament_id)
        if data.record_token is not None and data.record_token != getattr(filament, "record_token", None):
            raise HTTPException(409, "The filament record changed. Reload the editor.")
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

    sku_hint = data.sku_hint.strip()
    sku = identity.sku.strip()
    if sku_hint and sku and sku.casefold() != sku_hint.casefold():
        raise HTTPException(409, "The SKU differs from the filament in the editor")
    palette = [(1, data.palette_color_id)] if data.palette_color_id is not None else None
    target = {"mode": data.mode, "filament_id": data.filament_id, "identity": identity}
    if data.spool_lookup:
        if data.action != "fill" or not identity.designation.strip():
            raise HTTPException(422, "Enter the filament product name before looking up spool designs")
        issues = []
        package_weight = identity.weight_g or data.spool_package_weight_g
        if filament is None:
            duplicate_identity = identity.model_copy(update={"weight_g": package_weight})
            issues = [
                {"code": "duplicate", "filament_id": duplicate_id}
                for duplicate_id in await duplicate_ids(db, duplicate_identity, palette=palette, sku_hint=sku_hint)
            ]
        profile_id = getattr(filament, "filamentdb_spool_profile_id", None)
        await db.rollback()
        lookup = await linked_spool_result(profile_id, "visual")
        if not lookup or STYLE_CUES.search(data.guidance):
            candidates = await find_spool_profiles(
                f"{identity.manufacturer} {identity.designation}", "visual",
                style_hint=data.guidance, package_weight_g=package_weight,
                manufacturer_name=identity.manufacturer, product_name=identity.designation,
                material_name=identity.material, sku=sku or sku_hint, strict_package=True,
                linked_profile_id=lookup["spool_profiles"][0]["id"] if lookup else None,
            )
            if not lookup:
                lookup = candidates
            elif candidates["spool_profiles"]:
                lookup["spool_profiles"].extend(candidates["spool_profiles"][:3])
                lookup["reply"] += " Compare it with the listed alternatives that match your description."
            else:
                lookup["reply"] += " I couldn’t find another listed design matching your description."
        return {
            "target": target, "suggestions": [], "issues": issues,
            "spool_profiles": lookup["spool_profiles"], "spool_reply": lookup["reply"],
        }
    if filament is None:
        if data.action != "fill":
            raise HTTPException(422, "Standardization requires an existing filament")
        return {
            "target": target,
            **await research_products(
                db, identity, product_url=data.product_url, reference_url=data.reference_url, sku_hint=sku_hint,
                palette=palette, guidance=data.guidance, model_id=data.model_id, endpoint_id=data.endpoint_id,
                deadline=deadline,
                release_read=deadline is not None,
            ),
        }
    snapshot = filament_snapshot(filament)
    result = await batch_proposal(
        db, snapshot, data.action,
        sku_hint=data.sku_hint, product_url=data.product_url, reference_url=data.reference_url, guidance=data.guidance,
        release_read=deadline is not None, model_id=data.model_id, endpoint_id=data.endpoint_id, deadline=deadline,
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


@router.post("/saved-filaments/{draft_id}")
async def save_research_filament(draft_id: DraftId, data: FilamentCreate, db: DBSession, principal: PrincipalDep):
    owner = principal.user_id
    if principal.auth_type != "session" or owner is None:
        raise HTTPException(403, "A browser session is required")
    if not await lock_research_plugin(db):
        raise HTTPException(404, "FilaBrain is disabled")
    await ensure_any_permission(db, principal, "filaments:create")

    receipt = await db.get(FilamentResearchSave, (owner, draft_id), with_for_update=True)
    if receipt is not None:
        if receipt.filament_id is None:
            raise HTTPException(409, "The filament saved from this draft was deleted. Start a new draft.")
        return {"id": receipt.filament_id, "already_saved": True}

    manufacturer = await db.get(Manufacturer, data.manufacturer_id)
    if manufacturer is None:
        raise HTTPException(404, "Manufacturer not found")
    values = data.model_dump(exclude={"colors"})
    try:
        identity = snapshot_identity({**values, "manufacturer_name": manufacturer.name})
    except ValidationError as exc:
        raise HTTPException(422, "The filament identity contains an invalid value.") from exc
    palette = [(color.position, color.color_id) for color in data.colors or ()]
    if await duplicate_ids(db, identity, palette=palette):
        raise HTTPException(409, "A matching filament already exists. Review this draft before saving.")
    for field, default in (
        ("default_spool_weight_g", manufacturer.empty_spool_weight_g),
        ("spool_outer_diameter_mm", manufacturer.spool_outer_diameter_mm),
        ("spool_width_mm", manufacturer.spool_width_mm),
        ("spool_material", manufacturer.spool_material),
    ):
        if values[field] is None:
            values[field] = default

    try:
        filament_id = await get_next_available_id(db, Filament)
        db.add(Filament(id=filament_id, **values))
        # Persist the filament and its receipt together so retries can recover a committed save.
        await db.flush()
        for color in data.colors or []:
            db.add(FilamentColor(filament_id=filament_id, **color.model_dump()))
        db.add(FilamentResearchSave(owner_id=owner, draft_id=draft_id, filament_id=filament_id))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "The filament library changed during the save. Review the draft and try again.") from None
    response_cache.delete("filament_types")
    response_cache.delete("filter_options:filaments")
    await event_bus.publish({"event": "filaments_changed"})
    return {"id": filament_id, "already_saved": False}


@router.get("/saved-filaments/{draft_id}", response_class=RedirectResponse)
async def open_saved_filament(draft_id: DraftId, db: DBSession, principal: PrincipalDep, add_spool: bool = False):
    if principal.auth_type != "session" or principal.user_id is None:
        raise HTTPException(403, "A browser session is required")
    await ensure_any_permission(db, principal, "filaments:read", "filaments:create")
    if add_spool:
        await ensure_any_permission(db, principal, "spools:create")
    if not await is_enabled(db):
        raise HTTPException(404, "FilaBrain is disabled")
    receipt = await db.get(FilamentResearchSave, (principal.user_id, draft_id))
    if receipt is None or receipt.filament_id is None:
        raise HTTPException(404, "Saved filament not found")
    url = f"/spools/new?filament_id={receipt.filament_id}" if add_spool else f"/filaments/{receipt.filament_id}"
    return RedirectResponse(url, headers={"Cache-Control": "no-store"})


@router.get("/spool-filaments")
async def lookup_spool_filament(
    db: DBSession,
    principal: PrincipalDep,
    exact: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
    search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
):
    if principal.auth_type != "session":
        raise HTTPException(403, "A browser session is required")
    await ensure_any_permission(db, principal, "filaments:read")
    await ensure_any_permission(db, principal, "spools:create")
    if not await is_enabled(db):
        raise HTTPException(404, "FilaBrain is disabled")
    if (exact is None) == (search is None):
        raise HTTPException(422, "Provide an exact SKU or URL, or search by product name.")
    query = select(Filament).options(selectinload(Filament.manufacturer))
    if exact is not None:
        term = exact.strip()
        match = Filament.shop_url == term if term.lower().startswith(("https://", "http://")) else (
            func.lower(Filament.manufacturer_sku) == term.lower()
        )
        query = query.where(match)
    else:
        terms = (search or "").split()
        if not 1 <= len(terms) <= 12:
            raise HTTPException(422, "Use 1 to 12 search terms.")
        fields = (Manufacturer.name, Filament.designation, Filament.material_type, Filament.manufacturer_color_name)
        query = query.outerjoin(Manufacturer, Filament.manufacturer_id == Manufacturer.id).where(
            *(or_(*(field.icontains(term, autoescape=True) for field in fields)) for term in terms)
        )
    result = await db.scalars(query.order_by(Filament.designation, Filament.id).limit(13))
    items = [
        {
            "id": row.id,
            "designation": row.designation,
            "manufacturer": {"name": row.manufacturer.name} if row.manufacturer else None,
            "material_type": row.material_type,
            "material_subgroup": row.material_subgroup,
            "finish_type": row.finish_type,
            "manufacturer_color_name": row.manufacturer_color_name,
            "diameter_mm": row.diameter_mm,
            "raw_material_weight_g": row.raw_material_weight_g,
        }
        for row in result
    ]
    return {"items": items, "total": len(items)}


@router.get("/filaments/{filament_id}/research-record")
async def research_record(filament_id: int, db: DBSession, principal: PrincipalDep):
    if not await is_enabled(db):
        raise HTTPException(404, "FilaBrain is disabled")
    if principal.auth_type != "session":
        raise HTTPException(403, "A browser session is required")
    await ensure_any_permission(db, principal, "filaments:update")
    filament = await db.get(Filament, filament_id)
    if filament is None:
        raise HTTPException(404, "Filament not found")
    return {"record_token": getattr(filament, "record_token", None)}


@router.put("/filaments/{filament_id}/manufacturer-website", status_code=204)
async def save_manufacturer_website(
    filament_id: int, data: ManufacturerWebsite, db: DBSession, principal: PrincipalDep,
):
    if not await is_enabled(db):
        raise HTTPException(404, "FilaBrain is disabled")
    if principal.auth_type != "session":
        raise HTTPException(403, "A browser session is required")
    required = {"filaments:update", "manufacturers:update"}
    if not required <= await ensure_any_permission(db, principal, *required):
        raise HTTPException(403, "Manufacturer update permission is required")
    if not (website := manufacturer_site_url(data.url)):
        raise HTTPException(422, "Enter a manufacturer website")
    filament = select(Filament.id).where(
        Filament.id == filament_id, Filament.manufacturer_id == data.manufacturer_id,
    )
    token_column = getattr(Filament, "record_token", None)
    if token_column is not None:
        if data.record_token is None:
            raise HTTPException(409, "Reload the filament to verify its record.")
        filament = filament.where(token_column == data.record_token)
    result = await db.execute(
        update(Manufacturer)
        .where(
            Manufacturer.id == data.manufacturer_id,
            Manufacturer.name == data.name,
            or_(Manufacturer.url.is_(None), func.trim(Manufacturer.url) == ""),
            filament.exists(),
        )
        .values(url=website)
    )
    if not result.rowcount:
        raise HTTPException(409, "The manufacturer changed or already has a website. Reload the filament.")
    await db.commit()
    await event_bus.publish({"event": "manufacturers_changed"})
