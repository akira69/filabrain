"""Review one system extra field before retiring its definition."""
import hashlib
import json
import re
from typing import Annotated

from app.api.deps import DBSession, PrincipalDep, ensure_any_permission
from app.api.v1.schemas_filament import NumericRange
from app.api.v1.system_extra_fields import delete_system_extra_field
from app.models import Filament, LabelPreset, Spool, SystemExtraField, User
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from .agent_access import ToolArguments, read_access
from .agent_inventory import native_filament_fields
from .db import ExtraFieldCleanupArchive
from .model import LIMITS
from .proposals import is_missing
from .settings import lock_research_plugin

router = APIRouter(prefix='/extra-field-cleanup', tags=['FilaBrain'])
RANGES = {'bed_temp_range_c', 'extruder_temp_range_c', 'cooling_fan_range_percent'}


class ApplyCleanup(ToolArguments):
    fingerprint: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]


async def _admin(db, principal):
    if principal.auth_type != 'session':
        raise HTTPException(403, 'Open FilaBrain as a signed-in administrator.')
    await read_access(db, principal, 'admin:system')
    await ensure_any_permission(db, principal, 'filaments:read')


def _listed(field):
    return {'id': field.id, 'key': field.key, 'label': field.label, 'target_type': field.target_type}


def _normalized_value(field, key, value):
    if key in RANGES and field.field_type in ('number', 'range'):
        try:
            normalized = NumericRange.model_validate(value).model_dump()
        except ValueError:
            return None
        values = [v for v in normalized.values() if v is not None]
    elif field.field_type == 'number' and type(value) in (int, float):
        normalized, values = value, [value]
    else:
        return None
    low, high = LIMITS[key]
    return normalized if values and all(low <= v <= high for v in values) else None


def _references(value, pattern):
    if isinstance(value, dict):
        return any(_references(item, pattern) for item in (*value.keys(), *value.values()))
    if isinstance(value, list):
        return any(_references(item, pattern) for item in value)
    return isinstance(value, str) and bool(pattern.search(value))


async def _has_reference(db, field):
    key = re.escape(field.key)
    token = re.compile(r'(?:extra\.(?:filament\.|spool\.)?|custom_fields\.)' + key + r'(?![\w-])|^' + key + r'$',
                       re.IGNORECASE)
    for column in (LabelPreset.data, User.ui_preferences, Filament.label_field_aliases, Filament.custom_field_definitions):
        stream = await db.stream_scalars(select(column).where(column.is_not(None)).execution_options(yield_per=100))
        try:
            async for value in stream:
                if _references(value, token):
                    return True
        finally:
            await stream.close()
    # Formula references can use a bare extra field key.
    bare = re.compile(r'(?<![\w-])' + key + r'(?![\w-])', re.IGNORECASE)
    formulas = await db.execute(select(SystemExtraField.config, SystemExtraField.default_value)
                                .where(SystemExtraField.field_type == 'formula'))
    return any(_references(config, bare) or _references(default, bare) for config, default in formulas)


def _has_default(field):
    value = field.default_value
    if not value:
        return False
    try:
        default = json.loads(value)
    except (ValueError, TypeError):
        return True
    return default is not None and not (type(default) is dict and default == {'min': None, 'max': None})


def _compatible_unit(field, native_key):
    configured = field.config.get('unit') if isinstance(field.config, dict) else None
    if not configured:
        # A unit in the label still matters even when no display unit was configured.
        configured = field.label.rpartition('(')[2].removesuffix(')') if field.label.endswith(')') else ''
    units = {'density_g_cm3': {'g/cm3', 'g/cm³'}, 'drying_time_hours': {'h', 'hour', 'hours'},
             'cooling_fan_range_percent': {'%'}, 'max_volumetric_speed_mm3_s': {'mm3/s', 'mm³/s'}}
    allowed = {'°c', 'c'} if 'temp' in native_key else units.get(native_key, set())
    return not configured or isinstance(configured, str) and configured.strip().casefold() in allowed


@router.get('')
async def cleanup_candidates(db: DBSession, principal: PrincipalDep):
    await _admin(db, principal)
    permissions = await ensure_any_permission(db, principal, 'filaments:read', 'spools:read')
    targets = ['filament', 'spool'] if 'spools:read' in permissions else ['filament']
    fields = await db.scalars(select(SystemExtraField).where(SystemExtraField.target_type.in_(targets))
                              .order_by(SystemExtraField.target_type, SystemExtraField.label, SystemExtraField.id))
    archives = await db.execute(select(ExtraFieldCleanupArchive.id, ExtraFieldCleanupArchive.label,
                                      ExtraFieldCleanupArchive.target_type, ExtraFieldCleanupArchive.created_at)
                                .where(ExtraFieldCleanupArchive.target_type.in_(targets))
                                .order_by(ExtraFieldCleanupArchive.id.desc()))
    return {'items': [_listed(field) for field in fields if native_filament_fields(field)],
            'archives': [dict(row._mapping) for row in archives]}


@router.get('/archives/{archive_id}')
async def download_archive(archive_id: int, db: DBSession, principal: PrincipalDep):
    await _admin(db, principal)
    archive = await db.get(ExtraFieldCleanupArchive, archive_id)
    if archive is None:
        raise HTTPException(404, 'Cleanup archive not found.')
    if archive.target_type == 'spool':
        await ensure_any_permission(db, principal, 'spools:read')
    return JSONResponse(archive.payload, headers={
        'Content-Disposition': f'attachment; filename="filabrain-extra-field-{archive_id}.json"',
    })


async def _preview(db, principal, field_id):
    field = await db.get(SystemExtraField, field_id)
    if field is None or field.target_type not in ('filament', 'spool'):
        raise HTTPException(404, 'Extra field not found.')
    if field.target_type == 'spool':
        await ensure_any_permission(db, principal, 'spools:read')
    matches = native_filament_fields(field)
    key = matches[0] if len(matches) == 1 else ''
    blockers = []
    rows, changes, identities = [], [], []
    if field.source:
        blockers.append('plugin_managed')
    if (not key or key not in LIMITS or not _compatible_unit(field, key)
            or field.field_type not in ('number', 'range') or '.' in field.key):
        blockers.append('unsupported')
    if _has_default(field):
        blockers.append('default_value')
    if await _has_reference(db, field):
        blockers.append('referenced')

    if key and 'unsupported' not in blockers:
        model = Spool if field.target_type == 'spool' else Filament
        query = select(model, getattr(Filament, key)).where(model.custom_fields.is_not(None)).order_by(model.id)
        if model is Spool:
            query = query.join(Filament, Spool.filament_id == Filament.id)
        for row, native in await db.execute(query):
            if not isinstance(row.custom_fields, dict) or field.key not in row.custom_fields:
                continue
            identities.append((row.id, str(row.created_at), getattr(row, 'record_token', None),
                               row.filament_id if model is Spool else None))
            custom = row.custom_fields[field.key]
            if custom in (None, '', {'min': None, 'max': None}):
                rows.append({'id': row.id, 'custom': custom, 'native': native, 'action': 'discardEmpty'})
                changes.append((row, None))
                continue
            value = _normalized_value(field, key, custom)
            action = 'conflict'
            if value is not None:
                if value == native:
                    action = 'same'
                    changes.append((row, None))
                elif model is Filament and is_missing(key, native):
                    action = 'copy'
                    changes.append((row, value))
            rows.append({'id': row.id, 'custom': custom, 'native': native, 'action': action})
        if any(row['action'] == 'conflict' for row in rows):
            blockers.append('values_disagree')

    preview = {**_listed(field), 'native_field': key, 'rows': rows, 'blockers': blockers, 'can_apply': not blockers}
    definition = {**_listed(field), 'field_type': field.field_type, 'options': field.options, 'config': field.config,
                  'default_value': field.default_value, 'source': field.source, 'updated_at': field.updated_at.isoformat()}
    preview['fingerprint'] = hashlib.sha256(json.dumps([preview, definition, identities], sort_keys=True).encode()).hexdigest()
    return preview, definition, changes


@router.get('/{field_id}')
async def preview_cleanup(field_id: int, db: DBSession, principal: PrincipalDep):
    await _admin(db, principal)
    preview, _, _ = await _preview(db, principal, field_id)
    return preview


@router.post('/{field_id}')
async def apply_cleanup(field_id: int, data: ApplyCleanup, db: DBSession, principal: PrincipalDep):
    await _admin(db, principal)
    await db.rollback()
    try:
        if db.get_bind().dialect.name != 'sqlite':
            await db.connection(execution_options={'isolation_level': 'SERIALIZABLE'})
        if not await lock_research_plugin(db):
            raise HTTPException(404, 'FilaBrain is disabled')
        preview, definition, changes = await _preview(db, principal, field_id)
        if preview['fingerprint'] != data.fingerprint or not preview['can_apply']:
            raise HTTPException(409, 'Review the current cleanup preview before applying.')
        if changes:
            permission = 'spools:update' if preview['target_type'] == 'spool' else 'filaments:update'
            await ensure_any_permission(db, principal, permission)
        key = preview['key']
        for record, value in changes:
            record.custom_fields = {name: existing for name, existing in record.custom_fields.items() if name != key}
            if value is not None:
                setattr(record, preview['native_field'], value)
        archive = ExtraFieldCleanupArchive(target_type=preview['target_type'], label=preview['label'],
                                           payload={'definition': definition, 'native_field': preview['native_field'],
                                                    'rows': preview['rows']})
        db.add(archive)
        await db.flush()
        archive_id = archive.id
        # The host commits the archive, inventory changes and definition removal and invalidates its cache.
        await delete_system_extra_field(field_id, db)
    except HTTPException:
        await db.rollback()
        raise
    except DBAPIError as exc:
        await db.rollback()
        raise HTTPException(409, 'Could not apply cleanup. Refresh the preview and try again.') from exc
    return {'retired_field_id': field_id, 'archive_id': archive_id,
            'copied_values': sum(value is not None for _, value in changes)}
