"""Saved standard references and state-bound shared palette naming."""
import hashlib
import json
import re

from app.models import Color, Filament, FilamentColor
from sqlalchemy import select

from .color_standards import PANTONE_CODE, RAL_CODE, normalize_reference

COLOR_REFERENCES = 'filabrain_color_references'
_REFERENCE_START = re.compile(r'\b(?:RAL|Pantone)\s*', re.I)


def explicit_references(value) -> list[str]:
    """Read written references only; RGB values never imply a standard."""
    found = []
    if isinstance(value, str):
        for match in _REFERENCE_START.finditer(value):
            if code := normalize_reference(value[match.start():]):
                found.append(code)
    elif isinstance(value, dict):
        for child in value.values():
            found.extend(explicit_references(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(explicit_references(child))
    return list(dict.fromkeys(found))


def code_only(value: str | None) -> bool:
    value = (value or '').strip()
    return bool(RAL_CODE.fullmatch(value) or PANTONE_CODE.fullmatch(value))


def reference_issues(codes: list[str]) -> list[dict]:
    return [{'code': 'manufacturer_color_reference', 'name': code, 'url': None,
             'saved_reference': True} for code in codes]


async def saved_references(db, filament, assignments=None) -> list[str]:
    if assignments is None:
        assignments = (await db.execute(select(FilamentColor, Color).join(Color)
            .where(FilamentColor.filament_id == filament.id))).all()
    return explicit_references([filament.manufacturer_color_name, filament.custom_fields,
                               *[[assignment.display_name_override, color.name, color.custom_fields]
                                 for assignment, color in assignments]])


def rename_allowed(filament, assignments, code, references, preview) -> bool:
    if filament.color_mode == 'multi' or len(assignments) != 1 or not preview.get('hex_code') or not preview.get('source'):
        return False
    color = assignments[0][1]
    named = explicit_references(color.name)
    if named and set(named) != {code}:
        return False
    own = explicit_references([color.name, color.custom_fields])
    system = code.split()[0]
    same_system = {reference for reference in own if reference.split()[0] == system}
    if not same_system:
        same_system = {reference for reference in references if reference.split()[0] == system}
    return same_system == {code}


async def rename_state(db, item, filament, assignments, code, source, preview, *, locked=False):
    """Bind every linked assignment and product identity, not merely its count."""
    color = assignments[0][1]
    query = (select(FilamentColor, Filament).join(Filament, Filament.id == FilamentColor.filament_id)
             .where(FilamentColor.color_id == color.id).order_by(FilamentColor.id)
             .execution_options(populate_existing=True))
    if locked and db.get_bind().dialect.name != 'sqlite':
        query = query.with_for_update()
    linked = (await db.execute(query)).all()
    usage = [[link.id, link.filament_id, link.color_id, link.position, link.display_name_override,
              product.record_token, product.manufacturer_id, product.designation, product.material_type,
              product.manufacturer_color_name, product.color_mode, product.custom_fields,
              product.material_subgroup, product.diameter_mm, product.raw_material_weight_g,
              product.manufacturer_sku, product.finish_type,
              getattr(product, "filamentdb_spool_profile_id", None)] for link, product in linked]
    state = ['rename', item.batch_id, item.id, item.revision, item.identity, filament.id,
             color.id, color.name, color.hex_code, color.custom_fields, usage, code, source, preview]
    token = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    conflicts = sorted({
        reference
        for link, product in linked
        for reference in explicit_references([
            link.display_name_override, product.manufacturer_color_name, product.custom_fields,
        ])
        if reference.split()[0] == code.split()[0] and reference != code
    })
    return token, len({link.filament_id for link, _ in linked}), conflicts
