"""Read-only color research for the host's native color form."""
import asyncio

from app.api.deps import DBSession, PrincipalDep
from fastapi import APIRouter
from pydantic import ValidationError, field_validator

from .agent_access import bounded_result, read_access
from .agent_tools import IntakeArguments, color_preview
from .color_audit import explicit_references
from .color_review import ColorReference
from .color_standards import references_from

router = APIRouter(prefix='/agent', tags=['FilaBrain'])


class ColorArguments(IntakeArguments):
    @field_validator('query')
    @classmethod
    def valid_written_codes(cls, value):
        for code in explicit_references(value):
            ColorReference(code=code)
        return value


@router.post('/color-intake')
async def color_intake(args: ColorArguments, db: DBSession, principal: PrincipalDep):
    from . import research
    await read_access(db, principal, 'filaments:read')
    deadline = asyncio.get_running_loop().time() + 60
    codes = {code: {} for code in explicit_references(args.query)} if not (
        args.product_url or args.reference_url or args.sku_hint or args.identity) else {}
    proposal = {'target': {'mode': 'create', 'identity': {}}, 'suggestions': [], 'issues': []}
    notices = []
    if not codes:
        research_deadline = asyncio.get_running_loop().time() + 40
        try:
            async with asyncio.timeout_at(research_deadline + 2):
                proposal = await research.research(db, research.ProposalRequest(mode='create', **args.model_dump()), deadline=research_deadline)
        except TimeoutError:
            proposal['issues'].append({'code': 'search_incomplete'})
        color_names = [item['value'] for item in proposal['suggestions'] if item['field'] == 'manufacturer_color_name']
        codes = references_from(proposal['issues'], None)
        for code in explicit_references(color_names):
            codes.setdefault(code, {})
    choice = any(issue.get('code') in {'possible_match', 'ambiguous_match'} or issue.get('requires_product_choice') for issue in proposal['issues'])
    if choice:
        codes = {}
    previews = []
    for code, reference in list(codes.items())[:4]:
        try:
            # Only normalized publisher codes reach the lookup; product URLs remain evidence.
            data = ColorReference(code=code, manufacturer_source=reference.get('manufacturer_source'), from_product_url=reference.get('from_product_url', False))
        except ValidationError:
            notices.append('color_lookup_incomplete')
            continue
        try:
            async with asyncio.timeout_at(deadline):
                previews.append(await color_preview(data, db, principal))
        except TimeoutError:
            break
    if not previews or len(previews) < len(codes) or any(not preview['hex_code'] for preview in previews):
        notices.append('color_lookup_incomplete')
    if any(issue['code'] in {'search_incomplete', 'unavailable_source', 'no_match', 'ambiguous_match'} for issue in proposal['issues']):
        notices.append('product_lookup_incomplete')
    proposal['color_previews'] = previews
    await read_access(db, principal, 'filaments:read')
    return bounded_result({'reply': 'Choose the matching product to research its color.' if choice else
        'Review the publisher screen preview and existing swatches. Use the native color form to save reviewed changes.',
        'action': {'page': 'color'}, 'proposal': proposal, 'evidence': [],
        'notices': list(dict.fromkeys(notices)), 'complete': not choice and bool(previews) and not notices,
        'requires_review': True})
