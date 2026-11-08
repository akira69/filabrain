import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends

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
