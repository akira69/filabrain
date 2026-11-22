import { t } from './i18n'
import type { SpoolProfile } from './spool-profiles'

export type ResearchIdentity = {
  manufacturer: string
  designation: string
  material: string
  color: string
  diameter_mm: number | null
  weight_g: number | null
  sku: string
  material_subgroup?: string | null
  finish_type?: string | null
  color_mode?: string | null
}

export type ResearchOption = {
  value: string | number | string[] | { min: number | null; max: number | null }
  sources: Array<{ url?: string; title: string; evidence: string }>
}

export type ResearchIssue = ResearchProposal['issues'][number]

export type ResearchProposal = {
  target: { mode: 'edit' | 'create'; filament_id?: number | null; identity: ResearchIdentity }
  suggestions: Array<ResearchOption & { field: string }>
  issues: Array<{
    code: string; filament_id?: number; source?: string; name?: string; field?: string
    reference_code?: string | null; from_product_url?: boolean; url?: string; product_url?: string; source_product_code?: string; options?: ResearchOption[]; requires_product_choice?: boolean; reason?: string
    match_kind?: 'sku_exact' | 'sku_format' | 'similar_sku' | 'search'; identity?: Partial<ResearchIdentity>
  }>
  proposals?: Array<{
    id: string
    fields: Record<string, { old: string | null; value: string }>
    sources: Array<{ url?: string; title: string; quote: string }>
    reason: string
    group_key: string | null
  }>
  standardization_context?: Record<string, string | null>
  spool_profiles?: SpoolProfile[]
  spool_profile_id?: string
  spool_lookup_key?: string
  spool_reply?: string
  discovery_sku?: string
}

export function researchHeaders() {
  const csrf = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]*)/)?.[1]
  return { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf ? decodeURIComponent(csrf) : '' }
}

const prefix = '/api/v1/filabrain'
const labelKeys: Record<string, string> = {
  manufacturer_id: 'manufacturer', manufacturer_sku: 'sku', designation: 'designation',
  material_type: 'material', diameter_mm: 'diameter', manufacturer_color_name: 'color', color_id: 'paletteColor',
  density_g_cm3: 'density', datasheet_url: 'datasheet', raw_material_weight_g: 'weight',
  drying_temp_c: 'dryingTemp', drying_time_hours: 'dryingTime',
  extruder_temp_range_c: 'nozzleTemp', bed_temp_range_c: 'bedTemp',
}

const filamentFields: Record<string, string> = {
  default_spool_weight_g: 'defaultSpoolWeight', finish_type: 'finishType',
  material_subgroup: 'colSubgroup', shop_url: 'shopUrl', softening_temp_c: 'softeningTemperature',
  cooling_fan_range_percent: 'coolingFan', chamber_temp_c: 'chamberTemperature',
  max_volumetric_speed_mm3_s: 'maxVolumetricSpeed', flow_ratio: 'flowRatio', pressure_advance_k: 'pressureAdvance',
  image_url: 'imageUrl', ams_compatibility: 'amsCompatibility', build_plate_compatibility: 'buildPlateCompatibility',
}
const spoolFields: Record<string, string> = {
  spool_outer_diameter_mm: 'spoolOuterDiameter', spool_width_mm: 'spoolWidth', spool_material: 'spoolMaterial',
}

export function researchFieldLabel(field: string) {
  if (filamentFields[field]) return t(`filaments.${filamentFields[field]}`)
  if (spoolFields[field]) return t(`spools.${spoolFields[field]}`)
  return labelKeys[field] ? t(`filaBrain.${labelKeys[field]}`) : field.replaceAll('_', ' ')
}

export function researchOptionValue(value: ResearchOption['value']) {
  if (Array.isArray(value)) return value.join(', ')
  return typeof value === 'object'
    ? [...new Set([value.min, value.max].filter(endpoint => endpoint != null))].join(' – ')
    : String(value)
}

export function researchRecommendation(issue: ResearchIssue, value: ResearchOption['value'], preferred: unknown) {
  if (issue.requires_product_choice || JSON.stringify(value) !== JSON.stringify(preferred)) return ''
  return t(`filaBrain.${issue.code === 'source_disagreement' ? 'manufacturerPreferred'
    : issue.code === 'corroborated' ? 'corroboratedPreferred' : 'recommendedValue'}`)
}
