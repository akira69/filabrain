"""Shared, read-only agent services for HTTP and the host MCP endpoint."""
import asyncio
from types import SimpleNamespace
from typing import Annotated, Literal

from app.api.deps import DBSession, PrincipalDep
from app.models import Filament
from fastapi import APIRouter, HTTPException
from pydantic import Field, FiniteFloat, field_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .agent_access import ToolArguments, bounded_result, read_access
from .batch_api import ID, attached_swatches, matching_swatches, swatch_data
from .color_audit import (
    rename_allowed,
    saved_references,
    shared_color_links,
    shared_reference_conflicts,
)
from .color_review import ColorReference
from .color_standards import canonical_name, references_from, resolve_preview
from .proposals import Identity, batch_proposal, filament_identity, filament_snapshot
from .spool_profiles import find_spool_profiles

router = APIRouter(prefix='/agent', tags=['FilaBrain'])




class ResearchArguments(ToolArguments):
    query: Annotated[str, Field(max_length=400)] = ''
    filament_id: Annotated[int, Field(gt=0, le=2_147_483_647)] | None = None
    identity: Identity | None = None
    product_url: Annotated[str, Field(max_length=2048)] | None = None
    reference_url: Annotated[str, Field(max_length=2048)] | None = None
    sku_hint: str = Field('', max_length=255)
    guidance: str = Field('', max_length=400)


class SpoolArguments(ToolArguments):
    query: str = Field('', max_length=400)
    filament_id: ID | None = None
    package_weight_g: Annotated[FiniteFloat, Field(gt=0, le=50_000)] | None = None
    style_hint: str = Field('', max_length=400)
    intent: Literal['visual', 'dimensions', 'tare'] = 'visual'


class AuditArguments(ToolArguments):
    filament_ids: Annotated[list[ID], Field(min_length=1, max_length=50)]

    @field_validator('filament_ids')
    @classmethod
    def unique_ids(cls, value):
        return list(dict.fromkeys(value))




async def research_tool(args, db, principal):
    from . import research
    from .db import ensure_schema
    await read_access(db, principal, 'filaments:read')
    deadline = asyncio.get_running_loop().time() + 50
    await ensure_schema(db)
    data = research.ProposalRequest(mode='edit' if args.filament_id is not None else 'create', **args.model_dump())
    result = {'proposal': await research.research(db, data, deadline=deadline)}
    await read_access(db, principal, 'filaments:read')
    return bounded_result(result)


@router.post('/color')
async def color_preview(args: ColorReference, db: DBSession, principal: PrincipalDep):
    await read_access(db, principal, 'filaments:read')
    await db.rollback()
    preview = await resolve_preview(args.code)
    await read_access(db, principal, 'filaments:read')
    matches = await matching_swatches(db, [preview])
    return bounded_result({
        **args.model_dump(), **preview, 'canonical_name': canonical_name(args.code, preview),
        'matches': [swatch_data(color) for color in matches[:20]],
        'matches_truncated': len(matches) > 20, 'requires_review': True,
    })


@router.post('/spools')
async def spool_candidates(args: SpoolArguments, db: DBSession, principal: PrincipalDep):
    await read_access(db, principal, 'filaments:read')
    identity = {}
    question = args.query
    weight = args.package_weight_g
    profile_id = None
    if args.filament_id is not None:
        filament = await db.scalar(select(Filament).where(Filament.id == args.filament_id)
                                   .options(selectinload(Filament.manufacturer)))
        if filament is None:
            raise HTTPException(404, 'Filament not found')
        saved = filament_identity(filament)
        identity = {'manufacturer_name': saved.manufacturer, 'product_name': saved.designation,
                    'material_name': saved.material, 'sku': saved.sku}
        question = f'{saved.manufacturer} {saved.designation} {question}'.strip()
        weight = weight if weight is not None else saved.weight_g
        profile_id = getattr(filament, 'filamentdb_spool_profile_id', None)
    if not question.strip():
        raise HTTPException(422, 'Supply product clues or a filament ID')
    await db.rollback()
    try:
        async with asyncio.timeout(15):
            result = await find_spool_profiles(question, args.intent, style_hint=args.style_hint,
                package_weight_g=weight, strict_package=weight is not None, linked_profile_id=profile_id, **identity)
    except TimeoutError:
        result = {'reply': 'Spool sources did not respond in time. Try again or use a measured tare.',
                  'action': None, 'spool_profiles': [], 'source_status': 'unavailable'}
    await read_access(db, principal, 'filaments:read')
    return bounded_result({**result, 'requires_review': True})


@router.post('/color-audit')
async def audit_colors(args: AuditArguments, db: DBSession, principal: PrincipalDep):
    await read_access(db, principal, 'filaments:read')
    items = []
    deadline = asyncio.get_running_loop().time() + 40
    remaining = []
    for index, filament_id in enumerate(args.filament_ids):
        if asyncio.get_running_loop().time() >= deadline:
            remaining = args.filament_ids[index:]
            break
        filament = await db.scalar(select(Filament).where(Filament.id == filament_id)
                                   .options(selectinload(Filament.manufacturer)))
        if filament is None:
            items.append({'filament_id': filament_id, 'status': 'unavailable', 'references': []})
            continue
        snapshot = filament_snapshot(filament)
        saved = SimpleNamespace(**snapshot, custom_fields=filament.custom_fields,
                                record_token=getattr(filament, 'record_token', None))
        codes = await saved_references(db, filament)
        issues = []
        try:
            async with asyncio.timeout_at(deadline):
                if not codes:
                    proposal = await batch_proposal(db, snapshot, 'colors', deadline=deadline)
                    issues = proposal['issues']
                    codes = list(references_from(issues, saved.manufacturer_color_name))
                await db.rollback()
                previews = [(code, await resolve_preview(code)) for code in codes[:4]]
        except TimeoutError:
            remaining = args.filament_ids[index:]
            break
        await read_access(db, principal, 'filaments:read')
        assignments = await attached_swatches(db, filament_id)
        references = []
        for code, preview in previews:
            entry = {'code': code, **preview, 'canonical_name': canonical_name(code, preview),
                     'current': [swatch_data(color) for _, color in assignments]}
            if len(assignments) == 1:
                color = assignments[0][1]
                linked = await shared_color_links(db, color.id)
                conflicts = shared_reference_conflicts(linked, code)
                entry.update(usage_count=len({link.filament_id for link, _ in linked}), conflicts=conflicts)
                if rename_allowed(saved, assignments, code, {c: {} for c in codes}, preview) and not conflicts:
                    entry['proposed_name'] = canonical_name(code, preview)
            references.append(entry)
        items.append({'filament_id': filament_id, 'status': 'review' if references else 'needs_research',
                      'manufacturer_color_name': saved.manufacturer_color_name, 'references': references,
                      'issues': issues, 'review_url': f'/filaments/{filament_id}/edit'})
    return bounded_result({'items': items, 'complete': not remaining, 'remaining_filament_ids': remaining,
                           'requires_review': True})


mcp_read_tools = {
    'research_filament': {'description': 'Research a filament by ID, SKU, product URL or clues. Returns source-backed proposals; follow-up guidance can request other sources.',
                         'permission': 'filaments:read', 'arguments': ResearchArguments, 'handler': research_tool},
    'resolve_color_reference': {'description': 'Look up an exact official RAL/Pantone screen preview and matching saved swatches. Read-only; review before create_color or set_filament_colors.',
                               'permission': 'filaments:read', 'arguments': ColorReference, 'handler': color_preview},
    'find_spool_profiles': {'description': 'Compare public spool designs, photos, tare and dimensions for product clues or an existing filament. Designs require confirmation; never infer missing tare.',
                           'permission': 'filaments:read', 'arguments': SpoolArguments, 'handler': spool_candidates},
    'audit_filament_colors': {'description': 'Review selected filaments for exact RAL/Pantone references and canonical shared swatch names. Read-only; reports shared usage, conflicts and partial coverage.',
                             'permission': 'filaments:read', 'arguments': AuditArguments, 'handler': audit_colors},
}
