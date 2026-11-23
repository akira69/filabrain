"""Shared, read-only agent services for HTTP and the host MCP endpoint."""
import asyncio
import math
from types import SimpleNamespace
from typing import Annotated, Literal

from app.api.deps import DBSession, PrincipalDep
from app.models import Filament, Manufacturer
from fastapi import APIRouter, HTTPException
from pydantic import Field, FiniteFloat, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .agent_access import ToolArguments, bounded_result, read_access
from .batch_api import ID, attached_swatches, matching_swatches, swatch_data
from .chat_context import fact
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
INTAKE_RESEARCH_SECONDS = 38




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
            from .research import linked_spool_result
            result = await linked_spool_result(profile_id, args.intent)
            alternatives = None
            if result is None or args.style_hint:
                alternatives = await find_spool_profiles(question, args.intent, style_hint=args.style_hint,
                    package_weight_g=weight, strict_package=weight is not None, linked_profile_id=profile_id, **identity)
            if result is None:
                result = alternatives
            elif alternatives and alternatives['spool_profiles']:
                result['spool_profiles'].extend(alternatives['spool_profiles'][:3])
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
        items.append({'filament_id': filament_id, 'omitted_reference_codes': codes[4:], 'status': 'review' if references else 'needs_research',
                      'manufacturer_color_name': saved.manufacturer_color_name, 'references': references,
                      'issues': issues, 'review_url': f'/filaments/{filament_id}/edit'})
    await db.rollback()
    await read_access(db, principal, 'filaments:read')
    unavailable = [item['filament_id'] for item in items if item['status'] == 'unavailable']
    verified = all(item['status'] == 'review' and not item.get('omitted_reference_codes') and all(ref['hex_code'] for ref in item['references']) for item in items)
    return bounded_result({'items': items, 'complete': not remaining and not unavailable and verified, 'remaining_filament_ids': remaining,
                           'unavailable_filament_ids': unavailable, 'requires_review': True})


class IntakeIdentity(Identity):
    weight_g: Annotated[FiniteFloat, Field(gt=0, le=50_000)] | None = None


class IntakeArguments(ToolArguments):
    query: str = Field('', max_length=400)
    identity: IntakeIdentity | None = None
    product_url: str | None = Field(None, max_length=2048)
    reference_url: str | None = Field(None, max_length=2048)
    sku_hint: str = Field('', max_length=255)
    guidance: str = Field('', max_length=400)


@router.post('/intake')
async def plan_filament_intake(args: IntakeArguments, db: DBSession, principal: PrincipalDep):
    from . import research
    await read_access(db, principal, 'filaments:read')
    deadline = asyncio.get_running_loop().time() + 50
    research_deadline = asyncio.get_running_loop().time() + INTAKE_RESEARCH_SECONDS
    try:
        # Let the research service return its verified fields after its network budget expires.
        async with asyncio.timeout_at(research_deadline + 2):
            proposal = await research.research(db, research.ProposalRequest(mode='create', **args.model_dump()), deadline=research_deadline)
    except TimeoutError as error:
        raise HTTPException(504, 'Product research timed out. Narrow the clues or choose a product page.') from error
    # Discovery choices stay explicit, including exact-looking SKU matches.
    choice = any(issue.get('code') in {'possible_match', 'ambiguous_match'} or issue.get('requires_product_choice') for issue in proposal['issues'])
    draft = {}
    if not choice:
        identity = proposal['target']['identity']
        if isinstance(identity, Identity):
            identity = identity.model_dump()
        for key, field in [('manufacturer', 'manufacturer_name'), ('designation', 'designation'),
                           ('material', 'material_type'), ('color', 'manufacturer_color_name'),
                           ('diameter_mm', 'diameter_mm'), ('weight_g', 'raw_material_weight_g'), ('sku', 'manufacturer_sku')]:
            if identity.get(key):
                draft[field] = identity[key]
        for suggestion in proposal['suggestions']:
            field = suggestion['field']
            values = [entry['value'] for entry in proposal['suggestions'] if entry['field'] == field]
            if all(value == values[0] for value in values):
                draft[field] = values[0]
    if draft.get('manufacturer_id'):
        draft['manufacturer_name'] = await db.scalar(select(Manufacturer.name).where(Manufacturer.id == draft['manufacturer_id']))
    elif not draft.get('manufacturer_name') and not choice:
        draft['manufacturer_name'] = next((issue.get('name') for issue in proposal['issues'] if issue['code'] == 'manufacturer_required'), None)
    if draft.get('manufacturer_name') and draft.get('manufacturer_sku') and draft.get('material_type') and draft.get('diameter_mm'):
        from .agent_inventory import duplicate_group
        maker_id = await db.scalar(select(Manufacturer.id).where(func.lower(Manufacturer.name) == draft['manufacturer_name'].casefold()))
        if maker_id is not None:
            group = await duplicate_group(db, SimpleNamespace(manufacturer_id=maker_id, manufacturer_sku=draft['manufacturer_sku'],
                material_type=draft['material_type'], diameter_mm=draft['diameter_mm']), minimum=1)
            seen = {issue.get('filament_id') for issue in proposal['issues'] if issue['code'] == 'duplicate'}
            if group:
                proposal['issues'].extend({'code': 'duplicate', 'filament_id': identifier} for identifier in group['filament_ids'] if identifier not in seen)
    previews, notices = [], []
    if any(issue['code'] in {'search_incomplete', 'unavailable_source', 'no_match', 'ambiguous_match'} for issue in proposal['issues']):
        notices.append('product_lookup_incomplete')
    codes = references_from(proposal['issues'], draft.get('manufacturer_color_name')) if not choice else {}
    for code, reference in list(codes.items())[:2]:
        try:
            async with asyncio.timeout_at(deadline - 5):
                previews.append(await color_preview(ColorReference(code=code, manufacturer_source=reference.get('manufacturer_source'), from_product_url=reference.get('from_product_url', False)), db, principal))
        except TimeoutError:
            notices.append('color_lookup_incomplete')
            break
    if (len(codes) > len(previews) or any(preview['hex_code'] is None for preview in previews)) and 'color_lookup_incomplete' not in notices:
        notices.append('color_lookup_incomplete')
    spool_result = {'spool_profiles': []}
    package = draft.get('raw_material_weight_g')
    if package is not None and (not isinstance(package, (int, float)) or not math.isfinite(package) or not 0 < package <= 50_000):
        package = None
        notices.append('package_weight_unverified')
    if draft.get('designation') and not choice:
        try:
            async with asyncio.timeout_at(deadline):
                spool_result = await spool_candidates(SpoolArguments(
                    query=f"{draft.get('manufacturer_name', '')} {draft['designation']}"[:400],
                    package_weight_g=package), db, principal)
        except TimeoutError:
            notices.append('spool_lookup_incomplete')
    if spool_result.get('source_status') == 'unavailable':
        notices.append('spool_lookup_incomplete')
    proposal['spool_profiles'] = spool_result.get('spool_profiles', [])
    proposal['intake_fields'] = draft
    proposal['color_previews'] = previews
    if spool_result.get('reply'):
        proposal['spool_reply'] = spool_result['reply']
    missing = [field for field in ('manufacturer_name', 'designation', 'material_type', 'diameter_mm') if not draft.get(field)]
    evidence = []
    for preview in previews:
        evidence.append(fact('color', preview['code'], preview['canonical_name'],
            preview.get('source') or '/filaments', text=preview.get('hex_code') or 'Publisher screen preview unavailable.'))
    await read_access(db, principal, 'filaments:read')
    reply = ('Choose the matching product before filling the draft.' if choice else
             'No verified product fields are available yet. Try another source, a datasheet or more specific clues.' if not proposal['suggestions'] and not (args.identity and args.identity.designation) else
             'Review the filament draft, color and spool design before saving. Manufacturer color names remain separate from standard swatch names.')
    return bounded_result({'reply': reply, 'action': {'page': 'intake'},
        'evidence': evidence, 'notices': notices, 'proposal': proposal, 'filament_draft': draft,
        'color_previews': previews, 'spool_profiles': spool_result.get('spool_profiles', []),
        'spool_draft': {'empty_weight_g': None, 'requires_design_confirmation': True},
        'missing_fields': missing, 'requires_product_choice': choice, 'requires_review': True,
        'complete': not notices})


mcp_read_tools = {
    'plan_filament_intake': {'description': 'Plan a new filament from SKU, URL or clues, with exact color previews/reuse, product choices and spool designs. Read-only; confirm the product, swatch and design before native create tools.',
                             'permission': 'filaments:read', 'arguments': IntakeArguments, 'handler': plan_filament_intake},
    'research_filament': {'description': 'Research a filament by ID, SKU, product URL or clues. Returns source-backed proposals; follow-up guidance can request other sources.',
                         'permission': 'filaments:read', 'arguments': ResearchArguments, 'handler': research_tool},
    'resolve_color_reference': {'description': 'Look up an exact official RAL/Pantone screen preview and matching saved swatches. Read-only; review before create_color or set_filament_colors.',
                               'permission': 'filaments:read', 'arguments': ColorReference, 'handler': color_preview},
    'find_spool_profiles': {'description': 'Compare public spool designs, photos, tare and dimensions for product clues or an existing filament. Designs require confirmation; never infer missing tare.',
                           'permission': 'filaments:read', 'arguments': SpoolArguments, 'handler': spool_candidates},
    'audit_filament_colors': {'description': 'Review selected filaments for exact RAL/Pantone references and canonical shared swatch names. Read-only; reports shared usage, conflicts and partial coverage.',
                             'permission': 'filaments:read', 'arguments': AuditArguments, 'handler': audit_colors},
}
