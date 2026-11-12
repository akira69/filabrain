import hashlib
import json
import secrets
from copy import copy
from datetime import UTC, datetime, timedelta
from os import getenv
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from app.api.deps import DBSession, PrincipalDep, ensure_any_permission
from app.api.v1.filaments import create_filament
from app.api.v1.schemas_filament import FilamentCreate
from app.api.v1.schemas_spool import SpoolCreate
from app.api.v1.spools import create_spool
from app.core.config import settings
from app.core.security import (
    Principal,
    hash_token,
    is_argon2_hash,
    parse_token,
    verify_password_async,
    verify_token,
)
from app.models import (
    Filament,
    Manufacturer,
    User,
    UserApiKey,
    UserSession,
)
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from .db import FilamentResearchPairing
from .research import (
    Identity,
    ProposalRequest,
    filament_identity,
    get_edit_filament,
    identity_matches,
    is_enabled,
    research,
)
from .settings import (
    lock_research_plugin,
    mcp_access_enabled,
)

router = APIRouter(tags=["FilaBrain"])
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_REQUEST = 16_384
PAIR_DURATION = timedelta(minutes=15)
RecordID = Annotated[int, Field(gt=0, le=2_147_483_647)]


class PairTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["create", "edit"]
    filament_id: Annotated[int, Field(gt=0)] | None = None
    identity: Identity | None = None


class FindArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field("", max_length=255)
    manufacturer: str = Field("", max_length=255)
    material: str = Field("", max_length=255)
    color: str = Field("", max_length=255)
    sku: str = Field("", max_length=255)


class ResearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filament_id: RecordID | None = None
    identity: Identity | None = None
    product_url: Annotated[str, Field(max_length=2048)] | None = None
    sku_hint: str = Field("", max_length=255)


async def research_read_tool(args, db, _principal):
    data = ProposalRequest(mode="edit" if args.filament_id is not None else "create", **args.model_dump())
    return {"proposal": jsonable_encoder(await research(db, data))}


class ProposeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class ManufacturerQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field("", max_length=255)


class CreateFilamentArguments(FilamentCreate):
    model_config = ConfigDict(extra="forbid")
    manufacturer_id: RecordID


CreateSpoolArguments = create_model(
    "CreateSpoolArguments",
    __config__=ConfigDict(extra="forbid"),
    filament_id=(RecordID, ...),
    **{
        name: (field.annotation, copy(field))
        for name, field in SpoolCreate.model_fields.items()
        if name not in ("filament_id", "rfid_uid", "rfid_uid_2")
    },
)


def _tool(name, description, arguments, *, read_only=True):
    return {
        "name": name,
        "description": description,
        "inputSchema": arguments.model_json_schema(),
        "annotations": {"readOnlyHint": read_only, "idempotentHint": read_only},
    }


FIND_TOOL = _tool(
    "find_filaments",
    "Search FilaMan filaments by product, manufacturer, material, color or SKU.",
    FindArguments,
)
TOOLS = [
    FIND_TOOL,
    _tool(
        "research_filament",
        "Research the filament in the paired editor, or a new product in a paired create form.",
        ResearchArguments,
    ),
    _tool(
        "propose_filament", "Send a researched proposal ID to the paired editor for review.",
        ProposeArguments, read_only=False,
    ),
]
STANDALONE_TOOLS = [
    _tool("find_manufacturers", "Find FilaMan manufacturer IDs by name for new filaments.", ManufacturerQuery),
    FIND_TOOL,
    _tool(
        "research_filament",
        "Research an existing filament by ID, or look up a new product by identity, SKU or URL.",
        ResearchArguments,
    ),
    _tool(
        "create_filament",
        "Save a new filament to the FilaMan library. Confirm the product and fields with the user before saving.",
        CreateFilamentArguments, read_only=False,
    ),
    _tool(
        "create_spool",
        "Save one physical spool for an existing filament ID. Confirm the filament and spool details with the user before saving.",
        CreateSpoolArguments, read_only=False,
    ),
]


def _now():
    return datetime.now(UTC)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _secure(request: Request):
    headers = request.headers
    if any(header in headers for header in ("X-Forwarded-Proto", "X-Forwarded-For", "X-Real-IP")):
        # Stock FilaMan nginx overwrites X-Real-IP and appends that same
        # adjacent peer to X-Forwarded-For, but reflects the original scheme.
        peer = headers.get("X-Real-IP", "")
        trusted = {
            address.strip()
            for address in getenv("FILABRAIN_MCP_TRUSTED_PROXY_IPS", "").split(",")
            if address.strip()
        }
        if (
            peer not in trusted
            or peer != headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            or headers.get("X-Forwarded-Proto") != "https"
            or request.url.scheme != "https"
        ):
            raise HTTPException(403, "MCP requires HTTPS through a trusted reverse proxy")
    local = (
        request.client is not None
        and request.client.host in ("127.0.0.1", "::1")
        and request.url.hostname in ("127.0.0.1", "::1", "localhost")
    )
    if request.url.scheme != "https" and not local:
        raise HTTPException(403, "MCP requires HTTPS")
    origin = request.headers.get("Origin")
    if origin is not None:
        allowed = {f"{request.url.scheme}://{request.url.netloc}"}
        if settings.public_url:
            public = urlsplit(settings.public_url)
            allowed.add(f"{public.scheme}://{public.netloc}")
        if origin not in allowed:
            raise HTTPException(403, "Origin is not allowed")


async def _require_mcp(db, *, for_write=False):
    enabled = await lock_research_plugin(db) if for_write else await is_enabled(db)
    if not enabled:
        raise HTTPException(404, "FilaBrain is disabled")
    if not await mcp_access_enabled(db, locked=for_write):
        raise HTTPException(404, "External MCP access is disabled")


async def _require_target(db, principal, mode):
    await _require_mcp(db)
    await ensure_any_permission(
        db, principal, "filaments:update" if mode == "edit" else "filaments:create"
    )


@router.post("/pairings", status_code=201)
async def create_pairing(
    data: PairTarget, request: Request, db: DBSession, principal: PrincipalDep
):
    _secure(request)
    if principal.auth_type != "session" or principal.session_id is None:
        raise HTTPException(403, "Pair from a signed-in browser")
    await _require_target(db, principal, data.mode)
    if (data.mode == "edit") != (data.filament_id is not None):
        raise HTTPException(422, "Invalid editor target")
    identity = data.identity or Identity()
    if data.filament_id is not None:
        identity = filament_identity(await get_edit_filament(db, data.filament_id))
        if data.identity is not None and not identity_matches(data.identity, identity):
            raise HTTPException(409, "The filament identity changed")
    token = secrets.token_urlsafe(32)
    now = _now()
    pair = FilamentResearchPairing(
        id=uuid4().hex,
        token_hash=_digest(token),
        session_id=principal.session_id,
        expires_at=now + PAIR_DURATION,
        target={
            "mode": data.mode,
            "filament_id": data.filament_id,
            "identity": identity.model_dump(),
        },
    )
    await _require_mcp(db, for_write=True)
    await db.execute(
        delete(FilamentResearchPairing).where(FilamentResearchPairing.expires_at <= now)
    )
    db.add(pair)
    await db.commit()
    return {
        "id": pair.id,
        "token": token,
        "expires_at": pair.expires_at,
        "url": str(request.url_for("research_mcp")),
    }


async def _browser_pair(pair_id, db, principal):
    pair = await db.get(FilamentResearchPairing, pair_id)
    if not pair or pair.session_id != principal.session_id or pair.expires_at <= _now():
        raise HTTPException(404, "Pairing is no longer active")
    return pair


@router.get("/pairings/{pair_id}")
async def get_pairing(pair_id: str, db: DBSession, principal: PrincipalDep):
    pair = await _browser_pair(pair_id, db, principal)
    await _require_target(db, principal, pair.target["mode"])
    return (
        {"proposal": pair.proposal["proposal"], "proposal_id": pair.proposal["id"]}
        if pair.proposal
        else {"proposal": None, "proposal_id": None}
    )


@router.delete("/pairings/{pair_id}", status_code=204)
async def revoke_pairing(pair_id: str, db: DBSession, principal: PrincipalDep):
    pair = await _browser_pair(pair_id, db, principal)
    await db.delete(pair)
    await db.commit()


async def _authenticate(db, request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 120:
        raise HTTPException(
            401, "FilaMan API key or pairing token required", headers={"WWW-Authenticate": "Bearer"}
        )
    if auth.startswith("Bearer uak."):
        principal = await _api_key_principal(db, auth[7:])
        if principal is None:
            raise HTTPException(401, "Invalid FilaMan API key", headers={"WWW-Authenticate": "Bearer"})
        await _require_mcp(db)
        return None, principal
    result = await db.execute(
        select(FilamentResearchPairing, User)
        .join(UserSession, UserSession.id == FilamentResearchPairing.session_id)
        .join(User, User.id == UserSession.user_id)
        .where(
            FilamentResearchPairing.token_hash == _digest(auth[7:]),
            FilamentResearchPairing.expires_at > _now(),
            UserSession.revoked_at.is_(None),
            or_(UserSession.expires_at.is_(None), UserSession.expires_at > _now()),
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    row = result.first()
    if not row:
        raise HTTPException(
            401, "Pairing expired or revoked", headers={"WWW-Authenticate": "Bearer"}
        )
    pair, user = row
    principal = Principal(
        auth_type="session", user_id=user.id, is_superadmin=user.is_superadmin
    )
    await _require_target(db, principal, pair.target["mode"])
    return pair, principal


async def _api_key_principal(db, token: str) -> Principal | None:
    parsed = parse_token(token)
    if parsed is None or parsed[0] != "uak" or not 0 < parsed[1] <= 2_147_483_647:
        return None
    _, key_id, secret = parsed
    row = (await db.execute(
        select(UserApiKey, User)
        .join(User, User.id == UserApiKey.user_id)
        .where(UserApiKey.id == key_id, User.is_active.is_(True), User.deleted_at.is_(None))
    )).one_or_none()
    if row is None:
        return None
    key, user = row
    principal = Principal(
        auth_type="api_key", user_id=user.id, api_key_id=key_id,
        is_superadmin=user.is_superadmin, scopes=key.scopes,
        user_email=user.email, user_display_name=user.display_name, user_language=user.language,
    )
    now = _now()
    old_hash = key.key_hash
    values = {}
    if is_argon2_hash(old_hash):
        if not await verify_password_async(secret, old_hash):
            return None
        values["key_hash"] = hash_token(secret)
    elif not verify_token(secret, old_hash):
        return None
    if key.last_used_at is None or now - key.last_used_at >= timedelta(minutes=5):
        values["last_used_at"] = now
    if values:
        await db.execute(update(UserApiKey).where(
            UserApiKey.id == key_id, UserApiKey.key_hash == old_hash,
        ).values(**values))
        await db.commit()
    return principal


async def _find(db, principal, arguments):
    await ensure_any_permission(db, principal, "filaments:read")
    args = FindArguments.model_validate(arguments)
    query = (
        select(Filament)
        .join(Filament.manufacturer)
        .options(selectinload(Filament.manufacturer))
    )
    if args.query:
        query = query.where(
            or_(
                Filament.designation.icontains(args.query, autoescape=True),
                Filament.manufacturer_sku.icontains(args.query, autoescape=True),
            )
        )
    for value, column in (
        (args.manufacturer, Manufacturer.name),
        (args.material, Filament.material_type),
        (args.color, Filament.manufacturer_color_name),
        (args.sku, Filament.manufacturer_sku),
    ):
        if value:
            query = query.where(column.icontains(value, autoescape=True))
    rows = await db.scalars(query.order_by(Filament.id).limit(20))
    return {
        "filaments": [
            {"id": row.id, **filament_identity(row).model_dump()} for row in rows
        ]
    }


async def _call(db, pair, principal, name, arguments):
    if name == "find_filaments":
        return await _find(db, principal, arguments)
    if name == "research_filament":
        args = ResearchArguments.model_validate(arguments)
        target = dict(pair.target)
        if args.filament_id is not None and args.filament_id != target["filament_id"]:
            raise HTTPException(403, "This token is bound to a different editor")
        if args.identity:
            for key, value in args.identity.model_dump(exclude_unset=True).items():
                bound = target["identity"].get(key)
                if bound not in ("", None) and bound != value:
                    raise HTTPException(
                        403, "The requested identity conflicts with the paired editor"
                    )
                if target["mode"] == "create":
                    target["identity"] = {**target["identity"], key: value}
        proposal = jsonable_encoder(
            await research(
                db,
                ProposalRequest(
                    **target, product_url=args.product_url, sku_hint=args.sku_hint
                ),
            )
        )
        proposal_id = uuid4().hex
        await _require_mcp(db, for_write=True)
        result = await db.execute(
            update(FilamentResearchPairing)
            .where(
                FilamentResearchPairing.id == pair.id,
                FilamentResearchPairing.expires_at > _now(),
            )
            .values(candidate={"id": proposal_id, "proposal": proposal})
        )
        if not result.rowcount:
            raise HTTPException(401, "Pairing expired or revoked")
        await db.commit()
        return {"proposal_id": proposal_id, "proposal": proposal}
    if name == "propose_filament":
        args = ProposeArguments.model_validate(arguments)
        candidate = pair.candidate
        if not candidate or not secrets.compare_digest(
            candidate["id"], args.proposal_id
        ):
            raise HTTPException(
                404, "Research this proposal in the paired editor first"
            )
        if pair.target["mode"] == "edit":
            filament = await get_edit_filament(db, pair.target["filament_id"])
            if not identity_matches(Identity.model_validate(pair.target["identity"]), filament_identity(filament)):
                raise HTTPException(409, "The filament identity changed")
        await _require_mcp(db, for_write=True)
        result = await db.execute(
            update(FilamentResearchPairing)
            .where(
                FilamentResearchPairing.id == pair.id,
                FilamentResearchPairing.expires_at > _now(),
            )
            .values(proposal=candidate)
        )
        if not result.rowcount:
            raise HTTPException(401, "Pairing expired or revoked")
        await db.commit()
        return {"staged": True}
    raise HTTPException(404, "Unknown tool")


async def _call_standalone(db, principal, name, arguments):
    if name == "find_manufacturers":
        await ensure_any_permission(db, principal, "filaments:read", "filaments:create")
        args = ManufacturerQuery.model_validate(arguments)
        query = select(Manufacturer.id, Manufacturer.name).order_by(Manufacturer.name, Manufacturer.id).limit(20)
        if args.query:
            query = query.where(Manufacturer.name.icontains(args.query, autoescape=True))
        rows = await db.execute(query)
        return {"manufacturers": [{"id": row.id, "name": row.name} for row in rows]}
    if name == "find_filaments":
        return await _find(db, principal, arguments)
    if name == "research_filament":
        await ensure_any_permission(db, principal, "filaments:read")
        return await research_read_tool(ResearchArguments.model_validate(arguments), db, principal)
    if name == "create_filament":
        await ensure_any_permission(db, principal, "filaments:create")
        data = CreateFilamentArguments.model_validate(arguments)
        await _require_mcp(db, for_write=True)
        saved = await create_filament(data, db, principal)
        return {"id": saved.id, "designation": saved.designation, "path": f"/filaments/{saved.id}"}
    if name == "create_spool":
        await ensure_any_permission(db, principal, "spools:create")
        data = CreateSpoolArguments.model_validate(arguments)
        await _require_mcp(db, for_write=True)
        saved = await create_spool(SpoolCreate.model_validate(data.model_dump()), db, principal)
        return {"id": saved.id, "filament_id": saved.filament_id, "path": f"/spools/{saved.id}"}
    raise HTTPException(404, "Unknown tool")


def _rpc(id_, *, result=None, error=None):
    return {
        "jsonrpc": "2.0",
        "id": id_,
        **({"error": error} if error else {"result": result}),
    }


@router.get("/mcp")
async def research_mcp_get(request: Request, db: DBSession):
    _secure(request)
    await _authenticate(db, request)
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp", name="research_mcp")
async def research_mcp(request: Request, db: DBSession):
    _secure(request)
    version = request.headers.get("MCP-Protocol-Version")
    if version and version not in VERSIONS:
        raise HTTPException(400, "Unsupported MCP protocol version")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST:
            raise HTTPException(413, "MCP request is too large")
    pair, principal = await _authenticate(db, request)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        return _rpc(None, error={"code": -32700, "message": "Parse error"})
    if (
        not isinstance(body, dict)
        or body.get("jsonrpc") != "2.0"
        or not isinstance(body.get("method"), str)
    ):
        return _rpc(None, error={"code": -32600, "message": "Invalid request"})
    method = body["method"]
    if "id" not in body:
        return Response(status_code=202)
    request_id = body["id"]
    if type(request_id) not in (int, str):
        return _rpc(None, error={"code": -32600, "message": "Invalid request ID"})
    params = body.get("params", {})
    if not isinstance(params, dict):
        return _rpc(request_id, error={"code": -32602, "message": "Invalid params"})
    if method == "initialize":
        proposed = params.get("protocolVersion")
        return _rpc(
            request_id,
            result={
                "protocolVersion": proposed if proposed in VERSIONS else VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "filabrain", "version": "1.0.0"},
            },
        )
    if method == "ping":
        return _rpc(request_id, result={})
    if method == "tools/list":
        return _rpc(request_id, result={"tools": TOOLS if pair is not None else STANDALONE_TOOLS})
    if method != "tools/call":
        return _rpc(request_id, error={"code": -32601, "message": "Unknown method"})
    try:
        name, arguments = params.get("name"), params.get("arguments", {})
        if pair is None:
            result = await _call_standalone(db, principal, name, arguments)
        else:
            result = await _call(db, pair, principal, name, arguments)
    except (HTTPException, IntegrityError, ValidationError) as exc:
        if isinstance(exc, IntegrityError):
            await db.rollback()
            message = "FilaMan could not save this record. Check the referenced IDs and try again."
        elif isinstance(exc, HTTPException) and isinstance(exc.detail, str):
            message = exc.detail
        elif isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
            message = exc.detail.get("message", "Tool request failed")
        else:
            message = "Tool arguments or permissions are invalid"
        return _rpc(
            request_id,
            result={"content": [{"type": "text", "text": message}], "isError": True},
        )
    return _rpc(
        request_id,
        result={
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
            ],
            "structuredContent": result,
        },
    )
