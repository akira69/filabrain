import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from app.api.deps import PrincipalDep
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from . import batch_api, mcp, research, settings
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
for routes in (research.router, settings.router, mcp.router, batch_api.router):
    router.include_router(routes)


async def _research_tool(args, db, principal):
    # Leave time for schema checks, local database work and serialization before the host's 60-second limit.
    deadline = asyncio.get_running_loop().time() + 50
    await ensure_schema(db)
    result = await mcp.research_read_tool(args, db, principal, deadline=deadline)
    # The host truncates text past 60,000 characters, which would break the proposal JSON.
    if len(json.dumps(result, ensure_ascii=False)) > 60_000:
        raise HTTPException(422, "The research proposal is too large for FilaMan MCP. Review it in FilaBrain.")
    return result


mcp_read_tools = {
    "research_filament": {
        "description": (
            "Research filament specifications by existing filament ID, product identity, SKU, or public product URL. "
            "Returns source-backed suggestions for review."
        ),
        "permission": "filaments:read",
        "arguments": mcp.ResearchArguments,
        "handler": _research_tool,
    },
}


@router.get("/guide-art", include_in_schema=False)
async def guide_art(_principal: PrincipalDep):
    return FileResponse(Path(__file__).with_name("guide-art.webp"), media_type="image/webp")
