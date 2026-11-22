import asyncio
import hashlib
import json
import logging
from typing import Annotated, Literal
from uuid import uuid4

from app.api.deps import DBSession, PrincipalDep, resolve_user_permissions
from app.api.v1.schemas_filament import FilamentUpdate
from app.core.cache import response_cache
from app.core.event_bus import event_bus
from app.core.security import Principal
from app.models import Color, Filament, FilamentColor, User
from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import contains_eager

from .batch_worker import (
    RETENTION,
    cancel_batches,
    load_filament,
    lock_queue,
    now,
    owner_can_update,
    record_identity,
    settle_batch,
    start_worker,
)
from .color_standards import references_from, resolve_preview
from .db import FilamentResearchBatch as Batch
from .db import FilamentResearchBatchItem as Item
from .proposals import (
    MODE_FIELDS,
    filament_snapshot,
    is_missing,
)

router = APIRouter(prefix="/batches", tags=["FilaBrain"])
logger = logging.getLogger(__name__)
COLOR_REFERENCES = "filabrain_color_references"
MAX_DATABASE_ID = 2_147_483_647
ID = Annotated[int, Field(gt=0, le=MAX_DATABASE_ID, strict=True)]
Revision = Annotated[int, Field(ge=0, strict=True)]
UnitID = Annotated[str, Field(min_length=1, max_length=100)]


class BatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedFilament(BatchInput):
    id: ID
    manufacturer_id: ID
    designation: str
    material_type: str
    record_token: str | None = Field(None, pattern=r"^[0-9a-f]{32}$")


class CreateBatch(BatchInput):
    filament_ids: Annotated[list[ID], Field(min_length=1, max_length=50)]
    mode: Literal["fill", "standardize"]
    expected_filaments: Annotated[list[ExpectedFilament], Field(min_length=1, max_length=50)] | None = None


class ItemVersion(BatchInput):
    revision: Revision
    decision_revision: Revision


class ItemApproval(ItemVersion):
    item_id: ID
    approved_ids: Annotated[list[UnitID], Field(max_length=20)]


class ItemDecision(ItemApproval):
    decision: Literal["pending", "approved", "skipped"]

    @model_validator(mode="after")
    def valid_selection(self):
        if (self.decision == "approved") != bool(self.approved_ids):
            raise ValueError("Select proposal units only when approving")
        return self


class Decisions(BatchInput):
    items: Annotated[list[ItemDecision], Field(min_length=1, max_length=50)]


class ApplyBatch(BatchInput):
    items: Annotated[list[ItemApproval], Field(min_length=1, max_length=50)]


class RetryItem(ItemVersion):
    sku_hint: Annotated[str, Field(max_length=255)] = ""
    product_url: Annotated[str, Field(max_length=2048)] | None = None


class SwatchChoice(BatchInput):
    code: Annotated[str, Field(max_length=100)]
    token: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    action: Literal["keep", "reuse", "create"]
    color_id: ID | None = None
    color_name: Annotated[str, Field(max_length=100)] | None = None

    @model_validator(mode="after")
    def valid_color_selection(self):
        if self.action == "reuse" and (self.color_id is None or self.color_name is None):
            raise ValueError("Supply the selected swatch ID and name")
        if self.action != "reuse" and (self.color_id is not None or self.color_name is not None):
            raise ValueError("Supply a swatch only when reusing one")
        return self


async def require_editor(db: DBSession, principal: Principal, *, queue_locked: bool = False) -> int:
    owner_id = principal.user_id
    if principal.auth_type != "session" or owner_id is None:
        raise HTTPException(403, "An editor session is required")
    if not await owner_can_update(db, owner_id):
        if not queue_locked:
            await db.rollback()
            await lock_queue(db)
        await cancel_batches(db, (Batch.owner_id == owner_id) & Batch.finished_at.is_(None))
        await db.commit()
        raise HTTPException(403, "An editor session is required")
    return owner_id


async def load_batch(db: DBSession, batch_id: str, owner_id: int) -> Batch:
    batch = await db.scalar(select(Batch).where(
        Batch.id == batch_id,
        Batch.owner_id == owner_id,
    ).execution_options(populate_existing=True))
    if batch is None or (batch.finished_at is not None and batch.finished_at < now() - RETENTION):
        raise HTTPException(404, "Research batch not found")
    return batch


async def load_items(db: DBSession, batch: Batch) -> list[Item]:
    return list(await db.scalars(select(Item).where(
        Item.batch_id == batch.id
    ).order_by(Item.id).execution_options(populate_existing=True)))


def reviewed_identity(item: Item) -> dict:
    if item.status != "applied":
        return item.identity
    changes = {
        field: change["value"]
        for unit in item.proposals if unit["id"] in item.approved_ids
        for field, change in unit["fields"].items()
        if field in item.identity and field in item.changed_fields
    }
    return item.identity | changes


def summary(batch: Batch, total: int) -> dict:
    return {
        "id": batch.id,
        "mode": batch.mode,
        "status": "canceled" if batch.canceled_at else "finished" if batch.finished_at else "active",
        "created_at": batch.created_at,
        "finished_at": batch.finished_at,
        "total": total,
    }


async def detail(db: DBSession, batch: Batch) -> dict:
    items = await load_items(db, batch)
    return {
        **summary(batch, len(items)),
        "items": [{
            "id": item.id,
            "filament_id": item.filament_id,
            "live_filament_id": item.live_filament_id,
            "status": (
                "unavailable"
                if item.live_filament_id is None and item.status not in ("applied", "canceled")
                else item.status
            ),
            "decision": item.decision,
            "revision": item.revision,
            "decision_revision": item.decision_revision,
            "identity": item.identity,
            "color_references": list(references_from(item.issues, reviewed_identity(item).get("manufacturer_color_name"))),
            "proposals": item.proposals,
            "issues": item.issues,
            "approved_ids": item.approved_ids,
            "changed_fields": item.changed_fields,
            "applied_at": item.applied_at,
        } for item in items],
    }


def require_active(enabled: bool, batch: Batch | None = None) -> None:
    if not enabled:
        raise HTTPException(409, "FilaBrain is disabled")
    if batch is not None and batch.canceled_at is not None:
        raise HTTPException(409, "The batch was canceled")


def distinct_ids(ids: list[int]) -> None:
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Each ID may appear only once")


def matches_version(item: Item, version: ItemVersion) -> bool:
    return (item.revision, item.decision_revision) == (version.revision, version.decision_revision)


def selected_units(item: Item, approval: ItemApproval) -> list[dict]:
    units = [unit for unit in item.proposals if unit["id"] in approval.approved_ids]
    if len(units) != len(approval.approved_ids):
        raise HTTPException(422, "Select existing proposal units")
    return units


@router.post("")
async def create_batch(data: CreateBatch, request: Request, db: DBSession, principal: PrincipalDep):
    distinct_ids(data.filament_ids)
    enabled = await lock_queue(db)
    owner_id = await require_editor(db, principal, queue_locked=True)
    require_active(enabled)
    selection = (
        select(Filament)
        .join(Filament.manufacturer)
        .options(contains_eager(Filament.manufacturer))
        .where(Filament.id.in_(data.filament_ids))
        .order_by(Filament.id)
        .execution_options(populate_existing=True)
    )
    # Prevent a delete and ID reuse between the selection and FK insertion.
    if db.get_bind().dialect.name != "sqlite":
        selection = selection.with_for_update()
    records = {filament.id: filament for filament in await db.scalars(selection)}
    if len(records) != len(data.filament_ids):
        if data.expected_filaments is None:
            raise HTTPException(404, "A selected filament no longer exists")
        raise HTTPException(404, {
            "code": "filament_selection_changed",
            "message": "A selected filament no longer exists",
        })
    if data.expected_filaments is not None:
        expected_ids = {filament.id for filament in data.expected_filaments}
        if expected_ids != records.keys() or len(expected_ids) != len(data.expected_filaments):
            raise HTTPException(422, "Provide one identity per selected filament")
        for expected in data.expected_filaments:
            filament = records[expected.id]
            token_changed = expected.record_token is not None and expected.record_token != getattr(filament, "record_token", None)
            details_changed = (expected.manufacturer_id, expected.designation, expected.material_type) != (
                filament.manufacturer_id, filament.designation, filament.material_type,
            )
            if token_changed or details_changed:
                raise HTTPException(409, {
                    "code": "filament_selection_changed",
                    "message": "A selected filament changed. Reload the library and choose again.",
                })
    batch = Batch(id=uuid4().hex, owner_id=owner_id, mode=data.mode, created_at=now())
    db.add(batch)
    await db.flush()
    db.add_all([
        Item(
            batch_id=batch.id,
            filament_id=filament_id,
            live_filament_id=filament_id,
            status="queued",
            decision="pending",
            revision=0,
            decision_revision=0,
            attempts=0,
            identity=record_identity(filament_snapshot(records[filament_id])),
            proposals=[],
            issues=[],
            approved_ids=[],
            changed_fields=[],
            sku_hint="",
        ) for filament_id in data.filament_ids
    ])
    await db.commit()
    result = await detail(db, batch)
    # The host may hot-mount the router after its lifespan has started.
    start_worker(request.app)
    return result


@router.get("")
async def list_batches(db: DBSession, principal: PrincipalDep):
    owner_id = await require_editor(db, principal)
    counts = select(Item.batch_id, func.count().label("total")).group_by(Item.batch_id).subquery()
    rows = await db.execute(
        select(Batch, counts.c.total)
        .join(counts, counts.c.batch_id == Batch.id)
        .where(
            Batch.owner_id == owner_id,
            (Batch.finished_at.is_(None)) | (Batch.finished_at >= now() - RETENTION),
        )
        .order_by(Batch.created_at.desc())
    )
    return [summary(batch, total) for batch, total in rows]


@router.get("/{batch_id}")
async def get_batch(batch_id: str, db: DBSession, principal: PrincipalDep):
    owner_id = await require_editor(db, principal)
    return await detail(db, await load_batch(db, batch_id, owner_id))


async def swatch_item(db: DBSession, batch_id: str, item_id: int, principal: Principal, *, locked: bool = False):
    owner_id = await require_editor(db, principal, queue_locked=locked)
    batch = await load_batch(db, batch_id, owner_id)
    item = await db.scalar(select(Item).where(Item.batch_id == batch.id, Item.id == item_id)
                           .execution_options(populate_existing=True))
    if not item or item.live_filament_id is None:
        raise HTTPException(404, "Filament is unavailable")
    filament = await load_filament(db, item) if locked else await db.get(Filament, item.live_filament_id)
    if filament is None:
        raise HTTPException(404, "Filament is unavailable")
    identity_fields = ("manufacturer_id", "designation", "material_type", "material_subgroup",
                       "diameter_mm", "manufacturer_color_name", "finish_type", "color_mode", "manufacturer_sku")
    identity = reviewed_identity(item)
    if any(field in identity and identity[field] != getattr(filament, field) for field in identity_fields):
        raise HTTPException(409, "The filament changed after research. Start a new run to review its color.")
    return batch, item, filament


def swatch_data(color: Color) -> dict:
    return {"id": color.id, "name": color.name, "hex_code": color.hex_code}


async def matching_swatches(db: DBSession, previews: list[dict]) -> list[Color]:
    hex_codes = {preview["hex_code"] for preview in previews} - {None}
    return list(await db.scalars(select(Color).where(func.upper(Color.hex_code).in_(hex_codes))
                                 .order_by(Color.name, Color.id))) if hex_codes else []


async def attached_swatches(db: DBSession, filament_id: int, *, locked: bool = False):
    query = (select(FilamentColor, Color).join(Color, Color.id == FilamentColor.color_id)
             .where(FilamentColor.filament_id == filament_id).order_by(FilamentColor.position))
    if locked and db.get_bind().dialect.name != "sqlite":
        query = query.with_for_update()
    return (await db.execute(query)).all()


def saved_color_references(filament: Filament) -> list[dict]:
    saved = (filament.custom_fields or {}).get(COLOR_REFERENCES)
    return [item for item in saved if isinstance(item, dict)] if isinstance(saved, list) else []


def color_preview_token(item: Item, filament: Filament, assignments, code: str, source: dict, preview: dict) -> str:
    state = [item.revision, filament.id, getattr(filament, "record_token", None), filament.manufacturer_id,
             filament.designation, filament.manufacturer_color_name, filament.color_mode,
             [(assignment.id, assignment.display_name_override, swatch_data(color)) for assignment, color in assignments],
             saved_color_references(filament), code, source, preview]
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


async def may_create_color(db: DBSession, owner_id: int) -> bool:
    return bool(await db.scalar(select(User.is_superadmin).where(User.id == owner_id)) or
                "colors:create" in await resolve_user_permissions(db, owner_id))


@router.get("/{batch_id}/items/{item_id}/swatches")
async def preview_swatches(batch_id: str, item_id: int, db: DBSession, principal: PrincipalDep):
    _, item, filament = await swatch_item(db, batch_id, item_id, principal)
    references = list(references_from(item.issues, filament.manufacturer_color_name).items())[:4]
    await db.rollback()
    previews = await asyncio.gather(*(resolve_preview(code) for code, _ in references))

    batch, item, filament = await swatch_item(db, batch_id, item_id, principal)
    if references != list(references_from(item.issues, filament.manufacturer_color_name).items())[:4]:
        raise HTTPException(409, "The research references changed. Reopen the run and review them again.")
    assignments = await attached_swatches(db, filament.id)
    current = [swatch_data(color) for _, color in assignments]
    current_ids = {color["id"] for color in current}
    saved = saved_color_references(filament)
    matching = await matching_swatches(db, previews)
    return {
        "current": current,
        "can_assign": not batch.canceled_at and filament.color_mode != "multi" and len(current) <= 1,
        "can_save": not batch.canceled_at,
        "can_create": await may_create_color(db, principal.user_id),
        "references": [
            {"code": code, **preview, **source,
             "saved": any(reference.get("code") == code for reference in saved),
             "matches": [swatch_data(color) for color in matching if color.hex_code.upper() == preview["hex_code"]
                         and color.id not in current_ids],
             "token": color_preview_token(item, filament, assignments, code, source, preview)}
            for (code, source), preview in zip(references, previews)
        ],
    }


@router.post("/{batch_id}/items/{item_id}/swatches")
async def apply_swatch(batch_id: str, item_id: int, data: SwatchChoice, db: DBSession, principal: PrincipalDep):
    batch, item, filament = await swatch_item(db, batch_id, item_id, principal)
    require_active(True, batch)
    if data.code not in references_from(item.issues, filament.manufacturer_color_name):
        raise HTTPException(409, "This color reference is no longer available")
    await db.rollback()
    preview = await resolve_preview(data.code)

    enabled = await lock_queue(db)
    batch, item, filament = await swatch_item(db, batch_id, item_id, principal, locked=True)
    require_active(enabled, batch)
    references = references_from(item.issues, filament.manufacturer_color_name)
    assignments = await attached_swatches(db, filament.id, locked=True)
    source = references.get(data.code)
    saved = saved_color_references(filament)
    if source is None or data.token != color_preview_token(item, filament, assignments, data.code, source, preview):
        raise HTTPException(409, "The color preview changed. Reload the swatches and review again.")

    existing_reference = next((entry for entry in saved if entry.get("code") == data.code), None)
    reference = {"code": data.code, **preview, **source}
    if preview["hex_code"] is None and existing_reference and existing_reference.get("hex_code"):
        reference = existing_reference
    color = None
    made_color = False
    if data.action != "keep":
        if not preview["hex_code"] or filament.color_mode == "multi" or len(assignments) > 1:
            raise HTTPException(409, "A single-color swatch is required to change the assignment")
        if data.action == "reuse":
            color = await db.get(Color, data.color_id, with_for_update=True)
            if not color or color.name != data.color_name or color.hex_code.upper() != preview["hex_code"]:
                raise HTTPException(409, "The selected swatch changed. Review it again.")
        else:
            display_name = preview["name"] or data.code
            suffix = f" ({data.code})"
            name = data.code if display_name == data.code else display_name[:100 - len(suffix)].rstrip() + suffix
            color = await db.scalar(select(Color).where(Color.name == name, Color.hex_code == preview["hex_code"]))
            if color is None:
                if not await may_create_color(db, principal.user_id):
                    raise HTTPException(403, "You need permission to create colors")
                color = Color(name=name, hex_code=preview["hex_code"], custom_fields={
                    COLOR_REFERENCES: [{"code": data.code, **preview}],
                })
                db.add(color)
                made_color = True
                try:
                    await db.flush()
                except IntegrityError as exc:
                    await db.rollback()
                    raise HTTPException(409, "This swatch was just created. Reload the swatches to reuse it.") from exc

        if assignments:
            assignments[0][0].color_id = color.id
        else:
            db.add(FilamentColor(filament_id=filament.id, color_id=color.id, position=1))

    saved = [entry for entry in saved if entry.get("code") != data.code]
    filament.custom_fields = {**(filament.custom_fields or {}), COLOR_REFERENCES: [*saved, reference]}
    await db.commit()
    response_cache.delete("filter_options:filaments")
    await event_bus.publish({"event": "filaments_changed"})
    if made_color:
        await event_bus.publish({"event": "colors_changed"})
    return {"saved": True}


@router.post("/{batch_id}/decisions")
async def decide(batch_id: str, data: Decisions, db: DBSession, principal: PrincipalDep):
    distinct_ids([entry.item_id for entry in data.items])
    enabled = await lock_queue(db)
    owner_id = await require_editor(db, principal, queue_locked=True)
    batch = await load_batch(db, batch_id, owner_id)
    require_active(enabled, batch)
    items = {item.id: item for item in await load_items(db, batch)}
    group_keys = set()
    grouped = len(data.items) > 1
    for entry in data.items:
        item = items.get(entry.item_id)
        if (
            item is None
            or not matches_version(item, entry)
            or item.status in ("queued", "running", "applied", "unavailable", "canceled")
            or item.live_filament_id is None
        ):
            raise HTTPException(409, "A proposal changed since it was displayed")
        units = selected_units(item, entry)
        if entry.decision == "approved" and item.status != "ready":
            raise HTTPException(409, "The proposal is no longer ready")
        if grouped:
            if item.decision != "pending" or len(units) != 1 or not units[0].get("group_key"):
                raise HTTPException(409, "The approval group changed")
            group_keys.add(units[0]["group_key"])
        item.decision = entry.decision
        item.approved_ids = entry.approved_ids
        item.decision_revision += 1
    if grouped and len(group_keys) != 1:
        raise HTTPException(422, "Group members must share the same standardization rule")
    await settle_batch(db, batch)
    await db.commit()
    return await detail(db, batch)


@router.post("/{batch_id}/items/{item_id}/retry")
async def retry(
    batch_id: str, item_id: Annotated[int, Path(gt=0, le=MAX_DATABASE_ID)],
    data: RetryItem, db: DBSession, principal: PrincipalDep,
):
    enabled = await lock_queue(db)
    owner_id = await require_editor(db, principal, queue_locked=True)
    batch = await load_batch(db, batch_id, owner_id)
    require_active(enabled, batch)
    item = await db.get(Item, item_id, populate_existing=True)
    if item is None or item.batch_id != batch.id:
        raise HTTPException(404, "Research item not found")
    if (
        not matches_version(item, data)
        or item.live_filament_id is None
        or item.status in ("queued", "running", "applied", "unavailable", "canceled")
    ):
        raise HTTPException(409, "The research item cannot be retried")
    item.revision += 1
    item.decision_revision += 1
    item.decision = "pending"
    item.status = "queued"
    item.attempts = 0
    item.approved_ids = []
    item.proposals = []
    item.issues = []
    item.changed_fields = []
    item.sku_hint = data.sku_hint.strip()
    item.product_url = data.product_url
    batch.finished_at = None
    await db.commit()
    return await detail(db, batch)


@router.post("/{batch_id}/cancel")
async def cancel(batch_id: str, db: DBSession, principal: PrincipalDep):
    await lock_queue(db)
    owner_id = await require_editor(db, principal, queue_locked=True)
    batch = await load_batch(db, batch_id, owner_id)
    await cancel_batches(db, Batch.id == batch.id)
    await db.commit()
    return await detail(db, await load_batch(db, batch_id, owner_id))


async def apply_item(
    db: DBSession, batch_id: str, principal: Principal, entry: ItemApproval
) -> dict:
    enabled = await lock_queue(db)
    owner_id = await require_editor(db, principal, queue_locked=True)
    batch = await load_batch(db, batch_id, owner_id)
    item = await db.get(Item, entry.item_id, populate_existing=True)
    result = {"item_id": entry.item_id, "status": "approval_changed", "changed_fields": []}
    if item is None or item.batch_id != batch.id:
        return result
    current = matches_version(item, entry) and set(item.approved_ids) == set(entry.approved_ids)
    if current and item.status == "applied":
        return {**result, "status": "already_applied", "changed_fields": item.changed_fields}
    if not enabled or batch.canceled_at or not current or item.decision != "approved" or item.status != "ready":
        return result

    fields = {
        field: change
        for unit in selected_units(item, entry)
        for field, change in unit["fields"].items()
    }
    if not fields or not fields.keys() <= set(MODE_FIELDS[batch.mode]):
        return {**result, "status": "error"}
    filament = await load_filament(db, item)
    if filament is None:
        changed = ["filament_id"]
        item.status = "unavailable"
    else:
        snapshot = filament_snapshot(filament)
        changed = sorted(
            {field for field, value in item.identity.items() if snapshot[field] != value}
            | {
                field for field, change in fields.items()
                if snapshot[field] != change["old"]
                or (batch.mode == "fill" and not is_missing(field, snapshot[field]))
            }
        )
        if changed:
            item.status = "stale"
    if changed:
        item.changed_fields = changed
        item.decision = "pending"
        item.approved_ids = []
        item.decision_revision += 1
        await settle_batch(db, batch)
        return {**result, "status": "stale", "changed_fields": changed}
    try:
        patch = FilamentUpdate.model_validate({field: change["value"] for field, change in fields.items()})
    except ValidationError:
        return {**result, "status": "error"}
    for field, value in patch.model_dump(exclude_unset=True).items():
        setattr(filament, field, value)
    item.status = "applied"
    item.applied_at = now()
    item.changed_fields = sorted(fields)
    await settle_batch(db, batch)
    return {**result, "status": "applied", "changed_fields": item.changed_fields}


@router.post("/{batch_id}/apply")
async def apply(batch_id: str, data: ApplyBatch, db: DBSession, principal: PrincipalDep):
    distinct_ids([entry.item_id for entry in data.items])
    for entry in data.items:
        if not entry.approved_ids or len(set(entry.approved_ids)) != len(entry.approved_ids):
            raise HTTPException(422, "Select distinct proposal units")
    results = []
    for entry in data.items:
        try:
            result = await apply_item(db, batch_id, principal, entry)
            await db.commit()
        except HTTPException as exc:
            await db.rollback()
            if exc.status_code != 403 or not results:
                raise
            applied_count = sum(result["status"] == "applied" for result in results)
            raise HTTPException(403, {
                "message": f"Editor access changed during Apply. {applied_count} record(s) saved.",
                "results": results,
            }) from exc
        except SQLAlchemyError:
            await db.rollback()
            logger.exception("Could not apply FilaBrain item %s", entry.item_id)
            results.append({"item_id": entry.item_id, "status": "error", "changed_fields": []})
            break
        results.append(result)
        if result["status"] == "applied":
            response_cache.delete("filament_types")
            response_cache.delete("filter_options:filaments")
            await event_bus.publish({"event": "filaments_changed"})
    try:
        batch = await load_batch(db, batch_id, principal.user_id)
        batch_detail = await detail(db, batch)
    except SQLAlchemyError:
        await db.rollback()
        logger.exception("Could not refresh FilaBrain batch %s after Apply", batch_id)
        batch_detail = None
    return {"results": results, "batch": batch_detail}
