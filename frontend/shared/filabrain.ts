import { t } from './i18n'
import type { Reference } from './swatch-review'
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
  intake_fields?: Record<string, ResearchOption['value'] | boolean | null>
  color_previews?: Array<Omit<Reference, 'saved' | 'token'>>
  omit_library_color?: true
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

function candidateLink(url: string, label: string) {
  const link = document.createElement('a')
  link.href = url
  link.target = '_blank'
  link.rel = 'noopener noreferrer'
  link.textContent = label
  return link
}

export function productCandidate(issue: ResearchIssue, chooseProduct: (url: string, sku?: string) => void, mark?: SVGElement) {
  const productUrl = issue.product_url && /^https?:\/\//i.test(issue.product_url) ? issue.product_url : undefined
  const sourceUrl = issue.url && /^https?:\/\//i.test(issue.url) ? issue.url : productUrl
        const card = document.createElement('article')
        card.className = 'research-candidate'
        const title = document.createElement('h3')
        title.textContent = issue.name || t('filaBrain.possible_match', { name: '', source: issue.source || '' })
        card.append(title)
        if (issue.identity) {
          const product = issue.identity
          const metadata: [string, string | number | null | undefined][] = [
            ['manufacturer_id', product.manufacturer], ['manufacturer_sku', product.sku],
            ['diameter_mm', product.diameter_mm], ['raw_material_weight_g', product.weight_g],
            ['manufacturer_color_name', product.color],
          ]
          const details = document.createElement('p')
          details.className = 'research-candidate-identity research-note'
          details.textContent = metadata.filter(([, value]) => value != null && value !== '')
            .map(([field, value]) => `${researchFieldLabel(field)}: ${value}`).join(' · ')
          if (details.textContent) card.append(details)
        }
        if (issue.source_product_code) {
          const sourceCode = document.createElement('p')
          sourceCode.className = 'research-note'
          sourceCode.textContent = t('filaBrain.sourceProductCode', { code: issue.source_product_code })
          card.append(sourceCode)
        }
        if (issue.match_kind) {
          const kind = document.createElement('p')
          kind.className = 'research-note'
          kind.textContent = t(`filaBrain.match_${issue.match_kind}`)
          card.append(kind)
        }
        const actions = document.createElement('div')
        actions.className = 'research-match-actions'
        if (sourceUrl) actions.append(candidateLink(sourceUrl, issue.source || t('filaBrain.openSource')))
        if (productUrl) {
          const choose = document.createElement('button')
          choose.type = 'button'
          choose.className = 'fm-btn fm-btn-outline research-page-action'
          const label = t('filaBrain.useProductPage')
          choose.append(...(mark ? [mark.cloneNode(true)] : []), label)
          choose.addEventListener('click', () => chooseProduct(productUrl, issue.identity?.sku))
          actions.append(choose)
        }
        card.append(actions)
  return card
}

export function researchIssueText(issue: ResearchIssue) {
  const key = `filaBrain.${issue.code}`
  const message = t(key, {
    name: issue.name || (issue.field ? researchFieldLabel(issue.field) : issue.source || ''),
    source: issue.source || '',
  })
  const explanation = message === key ? issue.code.replaceAll('_', ' ') : message
  return explanation + (issue.reason ? ` (${issue.reason})` : '')
}
