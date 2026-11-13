from datetime import datetime
from weakref import WeakSet

from app.api.deps import DBSession
from app.models import Filament, User, UserSession
from app.models.base import TZDateTime
from sqlalchemy import JSON, Boolean, ForeignKey, String, text, true
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class FilamentResearchSettings(Base):
    __tablename__ = "filabrain_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    config: Mapped[dict | None] = mapped_column(JSON)
    mcp_enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())


class FilamentResearchSave(Base):
    __tablename__ = "filabrain_saves"

    owner_id: Mapped[int] = mapped_column(ForeignKey(User.__table__.c.id, ondelete="CASCADE"), primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    filament_id: Mapped[int | None] = mapped_column(
        ForeignKey(Filament.__table__.c.id, ondelete="SET NULL"), index=True
    )


class FilamentResearchPairing(Base):
    __tablename__ = "filabrain_pairings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey(UserSession.__table__.c.id, ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)
    target: Mapped[dict] = mapped_column(JSON)
    candidate: Mapped[dict | None] = mapped_column(JSON)
    proposal: Mapped[dict | None] = mapped_column(JSON)


class FilamentResearchBatch(Base):
    __tablename__ = "filabrain_batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey(User.__table__.c.id, ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime(), index=True)
    canceled_at: Mapped[datetime | None] = mapped_column(TZDateTime())


class FilamentResearchBatchItem(Base):
    __tablename__ = "filabrain_batch_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("filabrain_batches.id", ondelete="CASCADE"), index=True
    )
    filament_id: Mapped[int] = mapped_column()
    live_filament_id: Mapped[int | None] = mapped_column(
        ForeignKey(Filament.__table__.c.id, ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column()
    decision_revision: Mapped[int] = mapped_column()
    attempts: Mapped[int] = mapped_column()
    claim_token: Mapped[str | None] = mapped_column(String(32))
    claim_until: Mapped[datetime | None] = mapped_column(TZDateTime(), index=True)
    identity: Mapped[dict] = mapped_column(JSON)
    proposals: Mapped[list[dict]] = mapped_column(JSON)
    issues: Mapped[list[dict]] = mapped_column(JSON)
    approved_ids: Mapped[list[str]] = mapped_column(JSON)
    changed_fields: Mapped[list[str]] = mapped_column(JSON)
    sku_hint: Mapped[str] = mapped_column(String(255))
    product_url: Mapped[str | None] = mapped_column(String(2048))
    applied_at: Mapped[datetime | None] = mapped_column(TZDateTime())


_initialized = WeakSet()


async def ensure_schema(db: DBSession) -> None:
    bind = db.bind
    if bind in _initialized:
        return

    # A fresh install can be seen by several workers at once.
    async with bind.begin() as connection:
        dialect = connection.dialect.name
        if dialect == "sqlite":
            await connection.execute(text("BEGIN IMMEDIATE"))
        elif dialect == "postgresql":
            await connection.execute(text("SELECT pg_advisory_xact_lock(2743261400)"))
        elif dialect in ("mysql", "mariadb"):
            locked = await connection.scalar(
                text("SELECT GET_LOCK('filaman_filabrain_schema', 30)")
            )
            if locked != 1:
                raise RuntimeError("Could not lock the FilaBrain schema")
        try:
            await connection.run_sync(Base.metadata.create_all)
        finally:
            if dialect in ("mysql", "mariadb"):
                await connection.execute(
                    text("SELECT RELEASE_LOCK('filaman_filabrain_schema')")
                )
    _initialized.add(bind)
