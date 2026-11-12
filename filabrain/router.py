import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from app.api.deps import PrincipalDep
from fastapi import APIRouter, Depends
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
    await ensure_schema(db)
    return await mcp.research_read_tool(args, db, principal)


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
