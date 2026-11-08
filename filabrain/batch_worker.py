import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from uuid import uuid4

from app.api.deps import resolve_user_permissions
from app.core.database import async_session_maker
from app.models import (
    Filament,
    User,
)
from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from .db import FilamentResearchBatch as Batch
from .db import FilamentResearchBatchItem as Item
from .db import ensure_schema
from .proposals import (
    IDENTITY_FIELDS,
    batch_proposal,
    filament_snapshot,
)
from .settings import lock_research_plugin as lock_queue

RETENTION = timedelta(days=30)
LEASE = timedelta(seconds=120)
RESEARCH_TIMEOUT = 90
LEASE_RENEW_INTERVAL = 30
MAX_ATTEMPTS = 3
SETTLED = ("applied", "no_change", "unavailable", "canceled")
logger = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(UTC)


async def owner_can_update(db: AsyncSession, owner_id: int) -> bool:
    account = (
        await db.execute(select(User.is_active, User.is_superadmin).where(
            User.id == owner_id, User.deleted_at.is_(None)
        ))
    ).one_or_none()
    return bool(account and account.is_active and (
        account.is_superadmin
        or "filaments:update" in await resolve_user_permissions(db, owner_id)
    ))


def record_identity(snapshot: dict) -> dict:
    return {field: snapshot[field] for field in IDENTITY_FIELDS}


async def load_filament(db: AsyncSession, item: Item) -> Filament | None:
    if item.live_filament_id is None:
        return None
    # Lock the filament and its manufacturer before locking the item, in the
    # same order as a native delete that clears the item's foreign key.
    statement = (
        select(Filament)
        .join(Filament.manufacturer)
        .options(contains_eager(Filament.manufacturer))
        .where(Filament.id == item.live_filament_id)
        .execution_options(populate_existing=True)
    )
    if db.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    filament = await db.scalar(statement)
    # The numeric ID can have been reused since the item's link was first read.
    await db.refresh(item, attribute_names=["live_filament_id"], with_for_update=True)
    return filament if filament is not None and item.live_filament_id == filament.id else None


async def settle_batch(db: AsyncSession, batch: Batch) -> None:
    await db.flush()
    pending = await db.scalar(select(exists().where(
        Item.batch_id == batch.id,
        Item.status.not_in(SETTLED),
        Item.live_filament_id.is_not(None),
        Item.decision != "skipped",
    )))
    if pending:
        batch.finished_at = None
    elif batch.finished_at is None:
        batch.finished_at = now()


async def cancel_batches(db: AsyncSession, batch_filter) -> None:
    """Cancel the selected jobs in the caller's queue transaction."""
    active = select(Batch.id).where(Batch.canceled_at.is_(None), batch_filter)
    if db.get_bind().dialect.name != "sqlite":
        active = active.with_for_update()
    active_ids = list(await db.scalars(active))
    if not active_ids:
        return
    at = now()
    await db.execute(update(Batch).where(Batch.id.in_(active_ids)).values(
        canceled_at=at, finished_at=at,
    ))
    await db.execute(update(Item).where(
        Item.batch_id.in_(active_ids), Item.status != "applied",
    ).values(
        status="canceled",
        approved_ids=[],
        decision="pending",
        decision_revision=Item.decision_revision + 1,
    ))


class Claim(NamedTuple):
    item_id: int
    token: str
    snapshot: dict
    mode: str
    sku_hint: str
    product_url: str | None


async def _claim_next(db: AsyncSession) -> Claim | None:
    enabled = await lock_queue(db)
    if not enabled:
        await cancel_batches(db, Batch.finished_at.is_(None))
        await db.commit()
        return None

    owner_ids = await db.scalars(select(Batch.owner_id).where(
        Batch.finished_at.is_(None)
    ).distinct())
    for owner_id in owner_ids:
        if not await owner_can_update(db, owner_id):
            await cancel_batches(db, (Batch.owner_id == owner_id) & Batch.finished_at.is_(None))

    missing = list(await db.scalars(select(Item).join(Batch, Batch.id == Item.batch_id).where(
        Batch.finished_at.is_(None),
        Item.live_filament_id.is_(None),
        Item.status.not_in(SETTLED),
    )))
    for item in missing:
        item.status = "unavailable"
        item.approved_ids = []
        item.decision = "pending"
        item.decision_revision += 1
    for batch_id in {item.batch_id for item in missing}:
        batch = await db.get(Batch, batch_id, populate_existing=True)
        await settle_batch(db, batch)

    at = now()
    if await db.scalar(select(exists().where(Item.claim_until > at))):
        await db.commit()
        return None
    row = (await db.execute(
        select(Item, Batch)
        .join(Batch, Batch.id == Item.batch_id)
        .where(
            Batch.finished_at.is_(None),
            or_(Item.status == "queued", Item.status == "running"),
        )
        .order_by(Batch.created_at, Item.id)
        .limit(1)
        .execution_options(populate_existing=True)
    )).one_or_none()
    if row is None:
        await db.commit()
        return None
    item, batch = row
    filament = await load_filament(db, item)
    if filament is None:
        item.status = "unavailable"
    elif item.attempts >= MAX_ATTEMPTS:
        item.status = "failed"
        item.issues = [{"code": "attempts_exhausted"}]
    else:
        snapshot = filament_snapshot(filament)
        item.status = "running"
        item.claim_token = uuid4().hex
        item.claim_until = at + LEASE
        item.attempts += 1
        item.identity = record_identity(snapshot)
        claim = Claim(item.id, item.claim_token, snapshot, batch.mode, item.sku_hint, item.product_url)
        await db.commit()
        return claim
    item.claim_token = None
    item.claim_until = None
    await settle_batch(db, batch)
    await db.commit()
    return None


async def _store_result(db: AsyncSession, claim: Claim, result: dict) -> None:
    enabled = await lock_queue(db)
    item = await db.get(Item, claim.item_id, populate_existing=True)
    if item is None or item.claim_token != claim.token or item.claim_until <= now():
        return
    batch = await db.get(Batch, item.batch_id, populate_existing=True)
    item.claim_token = None
    item.claim_until = None
    if not enabled or not await owner_can_update(db, batch.owner_id):
        await db.flush()
        await cancel_batches(db, Batch.id == batch.id)
    elif item.status == "running":
        if item.live_filament_id is None:
            item.status = "unavailable"
        else:
            item.proposals = result["proposals"]
            item.issues = result["issues"]
            codes = {issue["code"] for issue in item.issues}
            if item.proposals:
                item.status = "ready"
            elif "unavailable_source" in codes:
                item.status = "failed"
            elif "needs_match" in codes:
                item.status = "needs_match"
            else:
                item.status = "no_change"
        await settle_batch(db, batch)
    await db.commit()


async def _renew_claim(db: AsyncSession, claim: Claim) -> bool:
    await lock_queue(db)
    at = now()
    updated = await db.execute(update(Item).where(
        Item.id == claim.item_id,
        Item.claim_token == claim.token,
        Item.claim_until > at,
    ).values(claim_until=at + LEASE))
    await db.commit()
    return bool(updated.rowcount)


async def _research_claim(sessions, claim: Claim) -> dict | None:
    async def propose():
        async with sessions() as db, asyncio.timeout(RESEARCH_TIMEOUT):
            return await batch_proposal(
                db, claim.snapshot, claim.mode, sku_hint=claim.sku_hint, product_url=claim.product_url
            )

    task = asyncio.create_task(propose())
    try:
        while not (await asyncio.wait({task}, timeout=LEASE_RENEW_INTERVAL))[0]:
            async with sessions() as db:
                if not await _renew_claim(db, claim):
                    return None
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def run_next_batch_item(sessions=async_session_maker) -> bool:
    async with sessions() as db:
        claim = await _claim_next(db)
    if claim is None:
        return False
    try:
        result = await _research_claim(sessions, claim)
    except Exception:  # noqa: BLE001 - Keep unexpected source errors scoped to one item.
        logger.warning("Filament batch research failed for item %s", claim.item_id)
        result = {"proposals": [], "issues": [{"code": "unavailable_source", "source": "Research"}]}
    if result is not None:
        async with sessions() as db:
            await _store_result(db, claim, result)
    return True


def start_worker(app) -> None:
    task = getattr(app.state, "filabrain_worker", None)
    if task is None or task.done():
        app.state.filabrain_worker = asyncio.create_task(run_batch_worker())


async def run_batch_worker() -> None:
    next_cleanup = now()
    while True:
        try:
            # Skip taking a write lock on installations with no active jobs.
            async with async_session_maker() as db:
                await ensure_schema(db)
                has_work = await db.scalar(select(exists().where(Batch.finished_at.is_(None))))
            if has_work and await run_next_batch_item():
                continue
            if now() >= next_cleanup:
                async with async_session_maker() as db:
                    await db.execute(delete(Batch).where(Batch.finished_at < now() - RETENTION))
                    await db.commit()
                next_cleanup = now() + timedelta(hours=1)
        except Exception:
            logger.exception("FilaBrain worker failed, will retry")
        await asyncio.sleep(2)
