"""Shared validation, access checks and bounded read results for agent tools."""
import json

from app.api.deps import ensure_any_permission
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra='forbid')



async def read_access(db, principal, permission):
    from .research import is_enabled
    if not await is_enabled(db):
        raise HTTPException(404, 'FilaBrain is disabled')
    await ensure_any_permission(db, principal, permission)


def bounded_result(result):
    result = jsonable_encoder(result)
    if len(json.dumps(result, ensure_ascii=False)) > 60_000:
        raise HTTPException(422, 'This result is too large for MCP. Narrow the selection or review it in FilaBrain.')
    return result
