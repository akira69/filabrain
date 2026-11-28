"""Measured inventory decisions and review proposals, shared by Chat and MCP."""
import asyncio
import http.client
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import httpx
from app.api.deps import DBSession, PrincipalDep, ensure_any_permission
from app.models import (
    Filament,
    FilamentColor,
    Manufacturer,
    Printer,
    PrinterSlot,
    PrinterSlotAssignment,
    Spool,
    SpoolEvent,
    SpoolStatus,
    SystemExtraField,
)
from fastapi import APIRouter, HTTPException
from pydantic import Field, FiniteFloat, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from . import sources
from .agent_access import ToolArguments, bounded_result, read_access
from .batch_api import ID
from .chat_context import collect_context, fact, lookup_live
from .proposals import _sku_key, batch_proposal, filament_snapshot, is_missing
from .settings import config_record

router = APIRouter(prefix='/agent', tags=['FilaBrain'])
Grams = Annotated[FiniteFloat, Field(gt=0, le=100_000_000)]

# shortcut: compare known labels; add an alias when its equivalence to a native field is established.
NATIVE_FILAMENT_FIELD_ALIASES = {
    'bed_temp_range_c': ('bed temp', 'bed temperature', 'build plate temperature', 'druckbetttemperatur'),
    'extruder_temp_range_c': ('extruder temperature', 'nozzle temp', 'nozzle temperature', 'print temperature', 'düsentemperatur'),
    'drying_temp_c': ('drying temp', 'drying temperature', 'trocknungstemperatur'),
    'drying_time_hours': ('drying time', 'drying duration', 'trocknungsdauer'),
    'chamber_temp_c': ('chamber temperature', 'chamber temp'),
    'softening_temp_c': ('softening temperature',),
    'cooling_fan_range_percent': ('cooling fan speed', 'fan speed'),
    'max_volumetric_speed_mm3_s': ('max volumetric speed', 'max volumetric flow'),
    'flow_ratio': ('flow ratio',),
    'pressure_advance_k': ('pressure advance',),
    'manufacturer_sku': ('manufacturer sku',),
    'density_g_cm3': ('density',),
}


class JobArguments(ToolArguments):
    query: str = Field('', max_length=400)
    required_g: Grams | None = None
    filament_id: ID | None = None
    printer_id: ID | None = None
    nozzle_mm: Annotated[FiniteFloat, Field(gt=0, le=4)] | None = None
    web_search: bool = False


class SelectionArguments(ToolArguments):
    query: str = Field('', max_length=200)
    filament_ids: Annotated[list[ID], Field(min_length=1, max_length=50)] | None = None
    page: int = Field(1, ge=1, le=1_000_000)
    page_size: int = Field(25, ge=1, le=50)

    @field_validator('filament_ids')
    @classmethod
    def unique_ids(cls, values):
        return list(dict.fromkeys(values)) if values is not None else None


class HealthArguments(SelectionArguments):
    check_sources: bool = False


class ExtraFieldAuditArguments(ToolArguments):
    target_type: Literal['filament', 'spool'] = 'filament'


class StockArguments(SelectionArguments):
    target_g: Grams | None = None
    history_days: int = Field(30, ge=1, le=365)


async def selected_filaments(db, args):
    filters = []
    if args.filament_ids is not None:
        filters.append(Filament.id.in_(args.filament_ids))
    searchable = func.lower(Manufacturer.name + ' ' + Filament.designation + ' ' + Filament.material_type + ' '
                            + func.coalesce(Filament.manufacturer_color_name, '') + ' '
                            + func.coalesce(Filament.manufacturer_sku, ''))
    for token in args.query.casefold().split():
        token = token.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        filters.append(searchable.like('%' + token + '%', escape='\\'))
    total = await db.scalar(select(func.count(Filament.id)).join(Filament.manufacturer).where(*filters))
    query = select(Filament).join(Filament.manufacturer).where(*filters).options(selectinload(Filament.manufacturer)).order_by(Filament.id)
    if args.filament_ids is None:
        query = query.offset((args.page - 1) * args.page_size).limit(args.page_size)
    rows = list(await db.scalars(query))
    missing = sorted(set(args.filament_ids or ()) - {row.id for row in rows})
    return rows, {'records_checked': len(rows), 'matching_records': total, 'page': args.page,
                  'has_more': args.filament_ids is None and args.page * args.page_size < total,
                  'unavailable_filament_ids': missing}


@router.post('/job-fit')
async def choose_job_spools(args: JobArguments, db: DBSession, principal: PrincipalDep):
    await read_access(db, principal, 'filaments:read')
    await read_access(db, principal, 'spools:read')
    permissions = await ensure_any_permission(db, principal, 'filaments:read', 'spools:read', 'printers:read')
    loaded = set()
    if args.printer_id is not None:
        await ensure_any_permission(db, principal, 'printers:read')
        printer = await db.scalar(select(Printer.id).where(Printer.id == args.printer_id, Printer.deleted_at.is_(None)))
        if printer is None:
            raise HTTPException(404, 'Printer not found')
        loaded = set(await db.scalars(select(PrinterSlotAssignment.spool_id).join(PrinterSlot)
            .where(PrinterSlot.printer_id == args.printer_id, PrinterSlot.is_active.is_(True),
                   PrinterSlotAssignment.present.is_(True), PrinterSlotAssignment.spool_id.is_not(None))))
    if args.filament_id is not None and await db.get(Filament, args.filament_id) is None:
        raise HTTPException(404, 'Filament not found')
    context = await collect_context(db, 'Which spools in my inventory? ' + args.query, permissions,
        required_g=args.required_g, filament_id=args.filament_id, printer_id=args.printer_id,
        nozzle_mm=args.nozzle_mm, web_search=args.web_search)
    if context is None:
        raise HTTPException(422, 'Supply a printing use case and required grams for an inventory check.')
    await db.rollback()
    if args.web_search:
        _, config = await config_record(db)
        await db.rollback()
        await lookup_live(context, config['search_url'], config['supplier_download_mib'])
    candidates = []
    for evidence in context.evidence:
        if evidence['kind'] != 'spool':
            continue
        spool_id = int(evidence['id'].removeprefix('spool-'))
        candidates.append({'id': spool_id, 'remaining_g': evidence['remaining_g'],
                           'meets_required': evidence['meets_required'], 'loaded_in_selected_printer': spool_id in loaded})
        if spool_id in loaded:
            evidence.setdefault('details', {})['loaded_in_selected_printer'] = True
    enough = [row['id'] for row in candidates if row['meets_required'] is True]
    reply = ('Supply the required filament weight to check individual spools.' if context.required_g is None else
             f'Showing {len(enough)} candidate spool(s) with enough measured filament for {context.required_g:g} g. '
             'Material and printer compatibility still need review; separate rolls are not combined.')
    await read_access(db, principal, 'filaments:read')
    await read_access(db, principal, 'spools:read')
    if args.printer_id is not None:
        await read_access(db, principal, 'printers:read')
    return bounded_result({'reply': reply, 'action': None, 'evidence': context.evidence, 'notices': context.notices,
        'required_g': context.required_g, 'search': context.search_status, 'candidates': candidates,
        'coverage': {'candidates_shown': len(candidates), 'exhaustive': False},
        'enough_material_spool_ids': enough, 'compatibility_requires_review': True, 'requires_review': True})


async def duplicate_group(db, filament, *, minimum=2):
    key = _sku_key(filament.manufacturer_sku or '')
    if not key:
        return None
    sku = func.lower(func.coalesce(Filament.manufacturer_sku, ''))
    for character in '_-.+/ ':
        sku = func.replace(sku, character, '')
    filters = (Filament.manufacturer_id == filament.manufacturer_id, sku == key,
               func.lower(Filament.material_type) == filament.material_type.casefold(),
               Filament.diameter_mm == filament.diameter_mm)
    count = await db.scalar(select(func.count(Filament.id)).where(*filters))
    if count < minimum:
        return None
    ids = list(await db.scalars(select(Filament.id).where(*filters).order_by(Filament.id).limit(50)))
    return {'filament_ids': ids, 'total': count, 'truncated': count > len(ids),
            'reason': 'Same manufacturer, normalized SKU, material and diameter. Review variants before merging.'}


@router.post('/library-health')
async def audit_library(args: HealthArguments, db: DBSession, principal: PrincipalDep):
    await read_access(db, principal, 'filaments:read')
    rows, coverage = await selected_filaments(db, args)
    ids = [row.id for row in rows]
    colored = set(await db.scalars(select(FilamentColor.filament_id).where(FilamentColor.filament_id.in_(ids))))
    items, groups, seen = [], [], set()
    important = ('manufacturer_sku', 'manufacturer_color_name', 'density_g_cm3', 'raw_material_weight_g',
                 'extruder_temp_range_c', 'bed_temp_range_c', 'default_spool_weight_g', 'shop_url')
    for row in rows:
        snapshot = filament_snapshot(row)
        findings = [{'code': 'missing_field', 'field': field} for field in important if is_missing(field, snapshot.get(field))]
        if row.id not in colored:
            findings.insert(0, {'code': 'missing_swatch'})
        convention = await batch_proposal(db, snapshot, 'standardize')
        for change in convention['proposals']:
            findings.append({'code': 'naming_convention', 'proposal': change})
        group = await duplicate_group(db, row)
        if group and tuple(group['filament_ids']) not in seen:
            seen.add(tuple(group['filament_ids']))
            groups.append(group)
        items.append({'filament_id': row.id, 'title': f'{row.manufacturer.name} {row.designation}',
                      'findings': findings, 'product_url': sources.public_link(row.shop_url),
                      'review_url': f'/filaments/{row.id}/edit'})
    await db.rollback()
    unchecked = []
    if args.check_sources:
        eligible = [item for item in items if item['product_url']]
        unchecked = [item['filament_id'] for item in eligible[5:]]
        deadline = asyncio.get_running_loop().time() + 25
        for index, item in enumerate(eligible[:5]):
            try:
                async with asyncio.timeout_at(deadline):
                    await sources.fetch_supplier(item['product_url'])
                status = 'available'
            except TimeoutError:
                unchecked.extend(entry['filament_id'] for entry in eligible[index:5])
                break
            except (ValueError, OSError, http.client.HTTPException, httpx.HTTPError) as error:
                reason = str(error)
                status = ('missing' if reason.startswith(('HTTP 404', 'HTTP 410')) else
                          'blocked' if reason.startswith(('HTTP 403', 'HTTP 429', 'Automated access blocked')) else 'unverified')
            item['source_check'] = {'status': status}
            if status == 'missing':
                item['findings'].append({'code': 'broken_link'})
    unverified = [item['filament_id'] for item in items if item.get('source_check', {}).get('status') in {'blocked', 'unverified'}]
    coverage.update(unverified_source_filament_ids=unverified, sources_checked=sum('source_check' in item for item in items),
                    unchecked_source_filament_ids=unchecked)
    evidence = []
    for item in items:
        findings = []
        for finding in item['findings']:
            if finding['code'] == 'naming_convention':
                findings.extend(f"{field}: {change['old']} → {change['value']}"
                                for field, change in finding['proposal']['fields'].items())
            else:
                findings.append(finding.get('field') or finding['code'].replace('_', ' '))
        evidence.append(fact('health', f"health-{item['filament_id']}", item['title'], f"/filaments/{item['filament_id']}",
            text='Review: ' + ', '.join(findings) if findings else 'No issues found in the checks performed.',
            details={'source_status': item.get('source_check', {}).get('status', 'not checked')},
            links=[{'title': 'Product page', 'url': item['product_url']}] if item['product_url'] else []))
    for index, group in enumerate(groups):
        evidence.append(fact('health', f'duplicates-{index}', 'Duplicate SKU candidates', '/filaments',
            text=group['reason'], details={'total': group['total'], 'truncated': group['truncated']},
            links=[{'title': f'Filament #{identifier}', 'url': f'/filaments/{identifier}'} for identifier in group['filament_ids']]))
    await read_access(db, principal, 'filaments:read')
    return bounded_result({'reply': f"Checked {len(items)} of {coverage['matching_records']} matching filaments. Review findings before changes.",
        'action': None, 'evidence': evidence, 'notices': [], 'items': items, 'duplicate_groups': groups,
        'coverage': coverage, 'complete': not coverage['has_more'] and not unchecked and not unverified and not coverage['unavailable_filament_ids'],
        'requires_review': True})


@router.post('/stock')
async def plan_stock(args: StockArguments, db: DBSession, principal: PrincipalDep):
    await read_access(db, principal, 'filaments:read')
    await read_access(db, principal, 'spools:read')
    rows, coverage = await selected_filaments(db, args)
    ids = [row.id for row in rows]
    spools = list(await db.scalars(select(Spool).join(Spool.status).where(Spool.filament_id.in_(ids),
        SpoolStatus.key.not_in(('archived', 'empty'))).order_by(Spool.id)))
    since = datetime.now(UTC) - timedelta(days=args.history_days)
    consumption = dict((await db.execute(select(Spool.filament_id, -func.sum(SpoolEvent.delta_weight_g)).join(
        SpoolEvent, SpoolEvent.spool_id == Spool.id).where(Spool.filament_id.in_(ids),
            SpoolEvent.event_type == 'print_consumption', SpoolEvent.delta_weight_g < 0, SpoolEvent.event_at >= since)
        .group_by(Spool.filament_id))).all())
    items = []
    for row in rows:
        active = [spool for spool in spools if spool.filament_id == row.id]
        known, unknown = 0.0, []
        for spool in active:
            weight = spool.remaining_weight_g
            if weight is None or not math.isfinite(weight) or weight < 0:
                unknown.append(spool.id)
            else:
                known += weight
        saved_target = getattr(row, 'low_stock_threshold_g', None)
        target = args.target_g if args.target_g is not None else saved_target
        if target is not None and (not math.isfinite(target) or target <= 0):
            target = None
        package = row.raw_material_weight_g
        package_known = package is not None and math.isfinite(package) and package > 0
        quantity = (max(0, math.ceil((target - known) / package))
                    if target is not None and package_known and not unknown else None)
        items.append({'filament_id': row.id, 'title': f'{row.manufacturer.name} {row.designation}',
            'known_available_g': known, 'unknown_weight_spool_ids': unknown, 'target_g': target,
            'target_source': 'request' if args.target_g is not None else 'saved threshold' if target is not None else None,
            'package_weight_g': package if package_known else None, 'order_quantity': quantity,
            'needs_target': target is None, 'needs_measurement': bool(unknown), 'needs_package_weight': not package_known,
            'recorded_consumption_g': consumption.get(row.id, 0), 'history_days': args.history_days,
            'product_url': sources.public_link(row.shop_url), 'review_url': f'/filaments/{row.id}/edit'})
    evidence = [fact('stock', f"stock-{item['filament_id']}", item['title'], f"/filaments/{item['filament_id']}",
        text=('Weigh the unknown spools before deciding whether to reorder.' if item['needs_measurement'] else
              'Supply a target stock level.' if item['needs_target'] else
              'Supply the package weight before calculating a quantity.' if item['needs_package_weight'] else
              f"Suggested order: {item['order_quantity']} package(s). Review before purchasing."),
        details={key: item[key] for key in ('known_available_g', 'target_g', 'package_weight_g', 'recorded_consumption_g', 'history_days')},
        links=[{'title': 'Product page', 'url': item['product_url']}] if item['product_url'] else [])
        for item in items]
    await read_access(db, principal, 'filaments:read')
    await read_access(db, principal, 'spools:read')
    return bounded_result({'reply': f"Checked {len(items)} of {coverage['matching_records']} matching filaments. Stock planning uses measured usable inventory and recorded consumption, not weight corrections. Nothing has been ordered.",
        'action': None, 'evidence': evidence, 'notices': [], 'items': items, 'coverage': coverage,
        'complete': not coverage['has_more'] and not coverage['unavailable_filament_ids'], 'requires_review': True})


def _field_name(value: str) -> str:
    label = re.sub(r'\s*\((?:°\s*[cf]|mm[³3]/s|g/cm[³3]|%|hours?|h)\)\s*$', '', value.casefold())
    return re.sub(r'[\W_]+', '', label)


async def audit_extra_fields(args: ExtraFieldAuditArguments, db, principal):
    await read_access(db, principal, 'filaments:read')
    if args.target_type == 'spool':
        await ensure_any_permission(db, principal, 'spools:read')
    fields = list(await db.scalars(select(SystemExtraField).where(SystemExtraField.target_type == args.target_type)
                                  .order_by(SystemExtraField.key)))
    items = []
    for field in fields:
        names = {_field_name(field.key), _field_name(field.label)}
        matches = [key for key, aliases in NATIVE_FILAMENT_FIELD_ALIASES.items()
                   if names.intersection(map(_field_name, (key, *aliases)))]
        if matches:
            items.append({'id': field.id, 'key': field.key, 'label': field.label, 'field_type': field.field_type,
                          'source': field.source, 'native_fields': matches,
                          'values': {key: {'populated': 0, 'same': 0, 'different': 0, 'native_blank': 0,
                                           'different_record_ids': []} for key in matches}})
    if items:
        keys = sorted({key for item in items for key in item['native_fields']})
        model = Spool if args.target_type == 'spool' else Filament
        query = select(model.id, model.custom_fields, *(getattr(Filament, key) for key in keys)).order_by(model.id)
        if model is Spool:
            query = query.join(Filament, Spool.filament_id == Filament.id)
        async for record_id, custom, *native in await db.stream(query.execution_options(yield_per=100)):
            if not isinstance(custom, dict) or not custom:
                continue
            for item in items:
                value = custom.get(item['key'])
                if value is None or value == '':
                    continue
                for key, current in zip(keys, native):
                    if key not in item['values']:
                        continue
                    counts = item['values'][key]
                    counts['populated'] += 1
                    state = 'native_blank' if is_missing(key, current) else 'same' if current == value else 'different'
                    counts[state] += 1
                    if state == 'different' and len(counts['different_record_ids']) < 10:
                        counts['different_record_ids'].append(record_id)
    return bounded_result({'items': items, 'target_type': args.target_type, 'native_target_type': 'filament',
                           'definitions_checked': len(fields), 'requires_review': True})


mcp_read_tools = {
    'choose_job_spools': {'description': 'Find individual inventory spools with enough measured filament and material/printer evidence. Unknown weights and compatibility remain explicit; separate rolls are not pooled.',
                         'permission': 'spools:read', 'arguments': JobArguments, 'handler': choose_job_spools},
    'audit_library': {'description': 'Read a selected or paged library health report: missing specs/swatches, naming and duplicate SKU candidates. Optional bounded source checks distinguish blocked from missing pages. No changes applied.',
                      'permission': 'filaments:read', 'arguments': HealthArguments, 'handler': audit_library},
    'audit_extra_fields': {'description': 'Find filament or spool system extra fields whose key or label overlaps a built-in filament field. Spool settings may be intentional overrides. Review before changing definitions or migrating values.',
                           'permission': 'filaments:read', 'arguments': ExtraFieldAuditArguments, 'handler': audit_extra_fields},
    'plan_stock': {'description': 'Plan replenishment from known usable grams, explicit targets/saved thresholds and recorded consumption. Unknown weights or package sizes prevent confident quantities. Never buys or changes stock.',
                   'permission': 'spools:read', 'arguments': StockArguments, 'handler': plan_stock},
}
