import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from app.api.deps import PrincipalDep
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from . import audio, batch_api, research, settings
from .batch_worker import start_worker
from .db import ensure_schema


@asynccontextmanager
async def lifespan(app):
    start_worker(app)
    try:
        yield
    finally:
        task = app.state.filabrain_worker
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


router = APIRouter(
    prefix="/filabrain",
    dependencies=[Depends(ensure_schema)],
    lifespan=lifespan,
)
for routes in (research.router, settings.router, batch_api.router, audio.router):
    router.include_router(routes)


class ResearchArguments(BaseModel):
    query: Annotated[str, Field(max_length=400)] = ""
    model_config = ConfigDict(extra="forbid")
    filament_id: Annotated[int, Field(gt=0, le=2_147_483_647)] | None = None
    identity: research.Identity | None = None
    product_url: Annotated[str, Field(max_length=2048)] | None = None
    sku_hint: str = Field("", max_length=255)


async def _research_tool(args, db, _principal):
    # Leave time for schema checks, local database work and serialization before the host's 60-second limit.
    deadline = asyncio.get_running_loop().time() + 50
    await ensure_schema(db)
    data = research.ProposalRequest(
        mode="edit" if args.filament_id is not None else "create",
        **args.model_dump(),
    )
    result = {"proposal": jsonable_encoder(await research.research(db, data, deadline=deadline))}
    # The host truncates text past 60,000 characters, which would break the proposal JSON.
    if len(json.dumps(result, ensure_ascii=False)) > 60_000:
        raise HTTPException(422, "The research proposal is too large for FilaMan MCP. Review it in FilaBrain.")
    return result


mcp_read_tools = {
    "research_filament": {
        "description": (
            "Research filament specifications by existing filament ID, product clues, product identity, SKU, or public product URL. "
            "Returns source-backed suggestions for review."
        ),
        "permission": "filaments:read",
        "arguments": ResearchArguments,
        "handler": _research_tool,
    },
}


@router.get("/guide-art", include_in_schema=False)
async def guide_art(_principal: PrincipalDep):
    return FileResponse(Path(__file__).with_name("guide-art.webp"), media_type="image/webp")
