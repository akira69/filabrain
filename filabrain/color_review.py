"""Read-only publisher color previews for unsaved filament selections."""
import asyncio
import ipaddress
from typing import Annotated
from urllib.parse import urlsplit

from app.api.deps import DBSession, PrincipalDep, ensure_any_permission
from app.models import Color
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .batch_api import ID, matching_swatches, swatch_data
from .color_standards import PANTONE_CODE, RAL_CODE, normalize_reference, resolve_preview
from .research import is_enabled
from .sources import public_link

router = APIRouter(tags=["FilaBrain"])


def manufacturer_link(value: str | None) -> str | None:
    link = public_link(value)
    if not link:
        return None
    host = urlsplit(link).hostname.rstrip('.').lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ('.' not in host or all(ch in '0123456789.' for ch in host)
                or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home', '.test'))):
            return None
    else:
        if not address.is_global or address.is_multicast:
            return None
    return link


class ColorReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Annotated[str, Field(min_length=1, max_length=100)]
    manufacturer_source: Annotated[str, Field(max_length=2048)] | None = None
    from_product_url: bool = False

    @field_validator("code")
    @classmethod
    def canonical_code(cls, value: str) -> str:
        value = value.strip()
        if not value.isascii() or not (RAL_CODE.fullmatch(value) or PANTONE_CODE.fullmatch(value)):
            raise ValueError("Supply an exact RAL or Pantone reference")
        return normalize_reference(value)

    @field_validator("manufacturer_source")
    @classmethod
    def public_manufacturer_source(cls, value: str | None) -> str | None:
        return manufacturer_link(value)


class ColorPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    references: Annotated[list[ColorReference], Field(min_length=1, max_length=4)]
    color_id: ID | None = None


async def review_permissions(db: DBSession, principal) -> bool:
    if not await is_enabled(db):
        raise HTTPException(404, "FilaBrain is disabled")
    await ensure_any_permission(db, principal, "filaments:create", "filaments:update")
    try:
        await ensure_any_permission(db, principal, "colors:create")
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        return False
    return True


@router.post("/color-previews")
async def preview_colors(data: ColorPreviewRequest, db: DBSession, principal: PrincipalDep):
    await review_permissions(db, principal)
    # Publisher requests only use strictly normalized codes, never manufacturer links.
    await db.rollback()
    previews = await asyncio.gather(*(resolve_preview(reference.code) for reference in data.references))
    can_create = await review_permissions(db, principal)
    current = []
    if data.color_id is not None:
        color = await db.get(Color, data.color_id, populate_existing=True)
        if color is None:
            raise HTTPException(404, "The selected color no longer exists")
        current = [swatch_data(color)]
    matching = await matching_swatches(db, previews)
    return {
        "current": current, "can_save": True, "can_assign": True, "can_create": can_create,
        "references": [
            {**reference.model_dump(), **preview, "saved": False, "token": "",
             "matches": [swatch_data(color) for color in matching
                         if color.hex_code.upper() == preview["hex_code"] and color.id != data.color_id]}
            for reference, preview in zip(data.references, previews)
        ],
    }
