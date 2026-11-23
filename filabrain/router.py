import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from app.api.deps import PrincipalDep
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from . import agent_tools, audio, batch_api, color_review, research, settings
from .batch_worker import start_worker
from .db import ensure_schema

mcp_read_tools = agent_tools.mcp_read_tools


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
for routes in (research.router, settings.router, batch_api.router, audio.router, color_review.router, agent_tools.router):
    router.include_router(routes)


@router.get("/guide-art", include_in_schema=False)
async def guide_art(_principal: PrincipalDep):
    return FileResponse(Path(__file__).with_name("guide-art.webp"), media_type="image/webp")
