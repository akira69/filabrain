import { element, errorText, hostRequest, type Status } from './main'
import { api, fetchAllPages } from './shared/api'
import { researchFieldLabel, researchOptionValue, researchRecommendation, type ResearchIdentity, type ResearchOption, type ResearchProposal } from './shared/filabrain'
import { t } from './shared/i18n'
import type { Chat } from './chat'
import type { ExpectedFilament } from './shared/filabrain-batches'
import type { SpoolResearchInput } from './spool-intake'
import { spoolProfileCards, spoolSource, type SpoolProfile } from './shared/spool-profiles'

type Value = ResearchOption['value'] | boolean | null
type Fields = Record<string, Value>
type Unit = Record<string, { old: Value; value: Value }>

export type EditorContext = {
  mode: 'create' | 'edit'; filament_id: number | null; fields: Fields; record_token?: string | null
}
export type ResearchDraft = {
  fields: Record<string, string>
  context: EditorContext
  proposal: ResearchProposal
  action: string
  selection?: Unit[]
  staged?: true
  saveId?: string
}
export type SelectionContext = { mode: 'selection'; filament_ids: number[]; expected_filaments?: ExpectedFilament[] }

const route = '/filabrain'
const sourceNotes = new Set([
  'manufacturer_page', 'archived_page', 'unavailable_source', 'manual_datasheet',
  'supplier_alternative_found', 'supplier_download_limit', 'supplier_rate_limited',
])
const checkedSources = new Set(['manufacturer_page', 'archived_page'])
const nativeFields = new Set([
  'manufacturer_id', 'color_id', 'designation', 'material_type', 'diameter_mm',
  'manufacturer_color_name', 'manufacturer_sku', 'density_g_cm3', 'raw_material_weight_g',
  'extruder_temp_range_c', 'bed_temp_range_c', 'drying_temp_c', 'drying_time_hours',
  'datasheet_url', 'default_spool_weight_g', 'spool_outer_diameter_mm', 'spool_width_mm',
  'spool_material', 'material_subgroup', 'finish_type', 'shop_url', 'softening_temp_c',
  'cooling_fan_range_percent', 'chamber_temp_c', 'max_volumetric_speed_mm3_s', 'flow_ratio',
  'pressure_advance_k', 'image_url', 'ams_compatibility', 'build_plate_compatibility',
])
const draftFields = [
  'manufacturer_id', 'designation', 'material_type', 'diameter_mm',
  'manufacturer_color_name', 'color_id', 'manufacturer_sku',
]
const productFields = [
  'manufacturer_id', 'manufacturer_sku', 'designation', 'material_type', 'diameter_mm',
  'manufacturer_color_name', 'raw_material_weight_g', 'finish_type', 'material_subgroup', 'color_id', 'color_mode',
]
const numericDraft = new Set(['manufacturer_id', 'color_id', 'diameter_mm'])
const spoolFields = [
  ['default_spool_weight_g', 'empty_weight_g'],
  ['spool_outer_diameter_mm', 'outer_diameter_mm'],
  ['spool_width_mm', 'width_mm'],
] as const
const blank = (value: Value | undefined) => value == null || value === ''
const equal = (a: Value | undefined, b: Value | undefined) => blank(a) && blank(b) ||
  a === b || (a != null && b != null && typeof a === 'object' && typeof b === 'object' && JSON.stringify(a) === JSON.stringify(b))
export const identity = (fields: Fields): ResearchIdentity => ({
  manufacturer: String(fields.manufacturer_name || ''),
  designation: String(fields.designation || ''),
  material: String(fields.material_type || ''),
  color: String(fields.manufacturer_color_name || ''),
  diameter_mm: typeof fields.diameter_mm === 'number' ? fields.diameter_mm : null,
  weight_g: typeof fields.raw_material_weight_g === 'number' ? fields.raw_material_weight_g : null,
  sku: String(fields.manufacturer_sku || ''),
  material_subgroup: String(fields.material_subgroup || '') || null,
  finish_type: String(fields.finish_type || '') || null,
  color_mode: String(fields.color_mode || '') || null,
})
const valueText = (value: Value) => value == null || value === '' ? '—'
  : typeof value === 'boolean' ? String(value) : researchOptionValue(value)

function acceptedFields(fields: Fields, selection: Unit[]): Fields {
  const accepted = { ...fields }
  for (const unit of selection) {
    for (const [field, change] of Object.entries(unit)) accepted[field] = change.value
  }
  return accepted
}

function packageWeight(fields: Fields) {
  const weight = fields.raw_material_weight_g
  return typeof weight === 'number' && Number.isFinite(weight) && weight > 0 ? weight : null
}

function spoolLookupKey(fields: Fields, hint: string) {
  const { manufacturer, designation, material, sku } = identity(fields)
  return JSON.stringify([manufacturer, designation, material, sku || hint].map(value => value.trim().toLowerCase()))
}

function matchesSpoolPackage(profile: SpoolProfile, weight?: Value) {
  if (typeof weight !== 'number') return true
  const weights = profile.package_weights_g
  if (Array.isArray(weights)) return !weights.length || weights.includes(weight)
  if (typeof profile.net_weight_g === 'number') return profile.net_weight_g === weight
  // Older FilaManDB cards could omit a known package weight.
  return profile.source !== 'FilaManDB'
}

function sameResolvedProduct(previous: ResearchProposal, fresh: ResearchProposal, accepted: Fields, known?: Fields) {
  const values = (proposal: ResearchProposal) => new Map(proposal.suggestions.map(item => [item.field, item.value]))
  const before = values(previous)
  const after = values(fresh)
  return productFields.every(field => {
    const old = accepted[field] ?? before.get(field) ?? known?.[field]
    const value = fresh.issues.some(issue => issue.field === field &&
      issue.options?.some(option => equal(accepted[field], option.value))) ? accepted[field] : after.get(field)
    return known && (blank(old) || blank(value)) || equal(old, value)
  })
}

function carryAcceptedProductFields(previous: ResearchProposal, fresh: ResearchProposal, selection: Unit[]) {
  const accepted = acceptedFields({}, selection)
  const reported = new Set([
    ...fresh.suggestions.map(item => item.field),
    ...fresh.issues.flatMap(issue => issue.field ? [issue.field] : []),
  ])
  const carried = previous.suggestions.filter(item => !reported.has(item.field) &&
    equal(accepted[item.field], item.value))
  for (const { field, options } of previous.issues) {
    if (!field || reported.has(field) || carried.some(item => item.field === field)) continue
    const acceptedOption = options?.find(option => equal(accepted[field], option.value))
    if (acceptedOption) carried.push({ field, ...acceptedOption })
  }
  return { ...fresh, suggestions: [...fresh.suggestions, ...carried] }
}

function keepSpoolMeasurements(selection: Unit[], profile: SpoolProfile, fresh: ResearchProposal) {
  return selection.filter(unit => {
    const [field] = Object.keys(unit)
    const key = spoolFields.find(([name]) => name === field)?.[1]
    if (!key || Object.keys(unit).length !== 1 || !equal(unit[field].value, profile[key])) return false
    const agrees = (value: Value) => equal(value, profile[key])
    return !fresh.suggestions.some(item => item.field === field && !agrees(item.value)) &&
      !fresh.proposals?.some(item => field in item.fields && !agrees(item.fields[field].value)) &&
      !fresh.issues.some(issue => issue.field === field && issue.options?.some(option => !agrees(option.value)))
  })
}

function mergeFollowUp(previous: ResearchProposal, fresh: ResearchProposal, field: string): ResearchProposal {
  const affects = (unit: NonNullable<ResearchProposal['proposals']>[number]) => field in unit.fields
  return {
    ...fresh,
    spool_profiles: previous.spool_profiles,
    spool_profile_id: previous.spool_profile_id,
    spool_lookup_key: previous.spool_lookup_key,
    suggestions: [...previous.suggestions.filter(item => item.field !== field), ...fresh.suggestions.filter(item => item.field === field)],
    proposals: [...(previous.proposals || []).filter(unit => !affects(unit)), ...(fresh.proposals || []).filter(affects)],
    issues: [...previous.issues.filter(issue => issue.field && issue.field !== field),
      ...fresh.issues.filter(issue => !issue.field || issue.field === field)],
  }
}

function externalLink(url: string, title: string) {
  const a = document.createElement('a')
  a.href = url
  a.textContent = title
  a.target = '_blank'
  a.rel = 'noopener noreferrer'
  return a
}

function appendSources(row: HTMLElement, sources: ResearchOption['sources']) {
  for (const source of sources) {
    const p = document.createElement('p')
    p.className = 'research-note'
    p.append(source.url && /^https?:\/\//i.test(source.url) ? externalLink(source.url, source.title) : source.title,
      source.evidence ? ` — ${source.evidence}` : '')
    row.append(p)
  }
}

function issueText(issue: ResearchProposal['issues'][number]) {
  const key = `filaBrain.${issue.code}`
  const message = t(key, {
    name: issue.name || (issue.field ? researchFieldLabel(issue.field) : issue.source || ''),
    source: issue.source || '',
  })
  const explanation = message === key ? issue.code.replaceAll('_', ' ') : message
  return explanation + (issue.reason ? ` (${issue.reason})` : '')
}

export function mountResearch(status: Status, chat: Chat, openChat: () => void, editor?: EditorContext) {
  const form = element<HTMLFormElement>('product-research')
  const researchMark = document.querySelector<SVGElement>('#plugin-header .filabrain-mark')!
  const results = element('research-results')
  const notice = element('research-status')
  const save = element<HTMLButtonElement>('save-filament')
  const confirm = element<HTMLDialogElement>('create-confirm')
  const createButton = element<HTMLButtonElement>('create-filament')
  const manufacturer = element<HTMLInputElement>('draft-manufacturer')
  const manufacturerOptions = element<HTMLDataListElement>('draft-manufacturer-options')
  let manufacturers: { id: number; name: string }[] = []
  const palette = form.elements.namedItem('color_id') as HTMLSelectElement
  const buttons = [...form.querySelectorAll<HTMLButtonElement>('button[type=submit]')]
  const advanced = element<HTMLDetailsElement>('research-advanced-fields')
  const searchOptions = element<HTMLDetailsElement>('research-options')
  const optionsSummary = searchOptions.querySelector('summary')!
  const conversation = element('research-conversation')
  const reply = element('research-reply')
  const followUpControls = document.createElement('div')
  const followUpLabel = document.createElement('label')
  const followUpScope = document.createElement('select')
  followUpControls.className = 'actions'
  followUpControls.hidden = true
  followUpScope.id = followUpLabel.htmlFor = 'research-follow-up-scope'
  followUpLabel.textContent = t('filaBrain.followUpScope')
  followUpControls.append(followUpLabel, followUpScope)
  document.querySelector('[data-chat-form] textarea')!.before(followUpControls)
  const inputs = element('research-inputs')
  const chatInput = document.querySelector('[data-chat-research-input]')!
  const chatResults = document.querySelector('[data-chat-research-results]')!
  const proposalTarget = (context: EditorContext) => ({
    mode: context.mode, filament_id: context.filament_id, identity: identity(context.fields),
    ...(context.mode === 'create' && typeof context.fields.color_id === 'number'
      ? { palette_color_id: context.fields.color_id } : {}),
  })
  let choices = new Map<HTMLInputElement, Unit>()
  let bound: EditorContext | undefined
  let boundProposal: ResearchProposal | undefined
  let refreshSpoolProfiles: (() => void) | undefined
  let draft: Fields | undefined
  let busy = false
  let saving = false
  let duplicate = false
  let lastAction = 'fill'
  let lastProductUrl = ''
  let lastSku = ''
  let formRevision = 0

  function showInChat(movePicker = true, reveal = true, newDraft = false) {
    if (reveal) openChat()
    if (movePicker) document.querySelector('[data-chat-form] .chat-toolbar')!.prepend(chat.picker)
    if (inputs.parentElement !== chatInput) chatInput.append(inputs)
    if (conversation.parentElement !== chatResults) chatResults.append(conversation)
    return chat.startResearch(newDraft)
  }

  let pendingOptions: Promise<unknown> = Promise.resolve()
  if (editor) {
    document.documentElement.dataset.researchEditor = ''
    buttons[0].before(chat.picker)
    element('draft-fields').hidden = true
    advanced.hidden = true
    element('research-identity').append(element('research-sku-hint'))
    element('research-heading').textContent = t(editor.mode === 'edit' ? 'filaBrain.editorResearchExisting' : 'filaBrain.editorResearchDraft')
    const target = element('research-target')
    target.hidden = !editor.fields.designation
    target.textContent = [editor.fields.designation, editor.fields.manufacturer_name].filter(Boolean).join(' · ')
    buttons[1].hidden = editor.mode !== 'edit'
  } else {
    optionsSummary.textContent = t('filaBrain.editSearch')
    async function options(path: string, select: HTMLSelectElement) {
      try {
        const { items } = await fetchAllPages<{ id: number; name: string }>(path)
        select.append(...items.map(item => new Option(item.name, String(item.id))))
      } catch (error) {
        conversation.hidden = false
        notice.textContent = errorText(error)
      }
    }
    pendingOptions = Promise.all([
      fetchAllPages<{ id: number; name: string }>('/api/v1/manufacturers').then(({ items }) => {
        manufacturers = items
        manufacturerOptions.append(...items.map(item => new Option(item.name)))
      }).catch(showResearchError),
      options('/api/v1/colors', palette),
    ])
  }

  function spoolLookupFields(fields: Fields, selection: Unit[]) {
    const accepted = acceptedFields(fields, selection)
    const maker = manufacturers.find(item => item.id === accepted.manufacturer_id)
    if (maker) accepted.manufacturer_name = maker.name
    return accepted
  }

  function localFields() {
    const formData = new FormData(form)
    const fields: Fields = {}
    for (const key of draftFields) {
      const value = String(formData.get(key) || '').trim()
      if (value) fields[key] = numericDraft.has(key) ? Number(value) : value
    }
    const name = manufacturer.value.trim()
    const sameName = manufacturers.filter(item => item.name.toLowerCase() === name.toLowerCase())
    const known = sameName.find(item => item.name === name) ?? (sameName.length === 1 ? sameName[0] : undefined)
    if (known) fields.manufacturer_id = known.id
    if (name) fields.manufacturer_name = known?.name || name
    return fields
  }

  let editorRecordToken = editor?.record_token
  async function recordToken(id: number) {
    const record = await api.get<{ record_token: string | null }>(
      `${route}/filaments/${id}/research-record`, { cache: 'no-store' },
    )
    return record.record_token
  }
  const initialRecord = editor?.filament_id && editorRecordToken === undefined
    ? recordToken(editor.filament_id).then(token => { editorRecordToken = token }).catch(() => {})
    : undefined

  async function target(): Promise<EditorContext> {
    if (!editor) {
      await pendingOptions
      return { mode: 'create', filament_id: null, fields: localFields() }
    }
    const context = await hostRequest<EditorContext>('filaman:filament-context')
    if (!context.filament_id) return context
    await initialRecord
    const token = await recordToken(context.filament_id)
    if (editorRecordToken !== undefined && token !== editorRecordToken) {
      throw new Error(t('filaBrain.identityChanged'))
    }
    editorRecordToken = token
    return { ...context, record_token: token }
  }

  function clearResults() {
    choices.clear()
    results.replaceChildren()
    bound = undefined
    boundProposal = undefined
    refreshSpoolProfiles = undefined
    followUpControls.hidden = true
    updateSave()
    reply.hidden = true
  }

  function showResearchError(error: unknown) {
    conversation.hidden = false
    notice.textContent = errorText(error)
  }

  function selected() {
    const units: Unit[] = []
    const seen = new Set<string>()
    for (const [control, unit] of choices) {
      const keys = Object.keys(unit)
      if (!control.checked || keys.some(key => seen.has(key))) continue
      keys.forEach(key => seen.add(key))
      units.push(unit)
    }
    return units
  }

  function restoreSelection(selection: Unit[]) {
    const remaining = [...selection]
    for (const [control, unit] of choices) {
      const match = remaining.findIndex(saved => Object.keys(unit).length === Object.keys(saved).length &&
        Object.entries(unit).every(([field, change]) => field in saved &&
          equal(change.old, saved[field].old) && equal(change.value, saved[field].value)))
      control.checked = match >= 0
      if (match >= 0) remaining.splice(match, 1)
    }
    refreshSpoolProfiles?.()
    updateSave()
  }

  function updateSave() {
    const count = editor ? selected().reduce((total, unit) => total + Object.keys(unit).length, 0) : 0
    save.disabled = !bound || duplicate || busy || saving || (!!editor && count === 0)
    followUpScope.disabled = busy || saving
    for (const control of results.querySelectorAll<HTMLButtonElement | HTMLInputElement>(
      '.chat-spool-picker button, input[type=checkbox], input[type=radio]',
    )) control.disabled = busy || saving
    if (editor) {
      save.textContent = t(count === 0 ? 'filaBrain.stageFields' : count === 1 ? 'filaBrain.stageOne' : 'filaBrain.stageMany', { count })
    }
  }

  function clearMeasurementChoices(field: string, keep?: HTMLInputElement) {
    for (const [control, unit] of choices) {
      if (control !== keep && field in unit) control.checked = false
    }
  }

  results.addEventListener('change', event => {
    const control = event.target
    if (control instanceof HTMLInputElement && control.checked) {
      const unit = choices.get(control)
      if (unit) for (const [field] of spoolFields) {
        if (field in unit) clearMeasurementChoices(field, control)
      }
    }
    refreshSpoolProfiles?.()
    updateSave()
    chat.saveSelection(selected())
  })

  function option(unit: Unit, sources: ResearchOption['sources'], reason?: string, group?: string) {
    const row = document.createElement('div')
    row.className = 'proposal-row'
    const label = document.createElement('label')
    const control = document.createElement('input')
    control.type = group ? 'radio' : 'checkbox'
    if (group) control.name = group
    choices.set(control, unit)
    const changes = document.createElement('span')
    for (const [field, { old, value }] of Object.entries(unit)) {
      const name = researchFieldLabel(field)
      const reference = field === 'manufacturer_id' ? manufacturers.find(item => item.id === value)?.name
        : field === 'color_id' ? palette.querySelector<HTMLOptionElement>(`option[value="${Number(value)}"]`)?.text : undefined
      const display = reference || (['manufacturer_id', 'color_id'].includes(field) && sources[0]?.evidence) || valueText(value)
      const previous = document.createElement('span')
      previous.className = 'research-note'
      previous.textContent = `${valueText(old)} → `
      if (changes.childNodes.length) changes.append(' · ')
      changes.append(`${name}: `, previous, display)
    }
    label.append(control, changes)
    row.append(label)
    if (reason || sources.length) {
      const evidence = document.createElement('div')
      evidence.className = 'proposal-evidence'
      if (reason) {
        const p = document.createElement('p')
        p.className = 'research-note'
        p.textContent = reason
        evidence.append(p)
      }
      appendSources(evidence, sources)
      row.append(evidence)
    }
    return row
  }

  function renderSpoolProfiles(proposal: ResearchProposal, fields: Fields, selection: Unit[]) {
    if (!Array.isArray(proposal.spool_profiles)) return
    const pickerHost = document.createElement('div')
    const measurements = document.createElement('div')
    let previousWeight: number | null | undefined
    let previousKey: string | undefined
    const lookupKey = proposal.spool_lookup_key ?? spoolLookupKey(fields, lastSku)
    function show(profile?: SpoolProfile, fromClick = false) {
      for (const control of measurements.querySelectorAll<HTMLInputElement>('input')) choices.delete(control)
      measurements.replaceChildren()
      const source = profile && spoolSource(profile.url)
      if (profile && source) {
        for (const [field, measurement] of spoolFields) {
          const value = profile[measurement]
          if (!blank(fields[field]) || typeof value !== 'number' || !Number.isFinite(value) || value <= 0) continue
          if (fromClick) clearMeasurementChoices(field)
          const row = option({ [field]: { old: fields[field] ?? null, value } }, [{
            title: profile.source, url: source.href, evidence: profile.title,
          }])
          row.querySelector('input')!.checked = fromClick
          measurements.append(row)
        }
      }
      if (fromClick) {
        proposal.spool_profile_id = profile?.id
        updateSave()
        chat.saveSelection(selected())
      }
    }
    function refresh(selection: Unit[]) {
      const accepted = spoolLookupFields(fields, selection)
      const weight = packageWeight(accepted)
      const key = spoolLookupKey(editor ? fields : accepted, lastSku)
      if (weight === previousWeight && key === previousKey) return
      const profiles = key === lookupKey
        ? (proposal.spool_profiles || []).filter(profile => profile && matchesSpoolPackage(profile, weight))
        : []
      const active = profiles.find(profile => profile?.id === proposal.spool_profile_id)
      if (previousWeight === undefined || !active) show(active)
      proposal.spool_profile_id = active?.id
      previousWeight = weight
      previousKey = key
      const picker = spoolProfileCards(profiles, active?.id, profile => show(profile, true))
      pickerHost.replaceChildren(...(picker ? [picker] : []))
      pickerHost.hidden = !picker
      measurements.hidden = !picker
    }
    results.append(pickerHost, measurements)
    refresh(selection)
    refreshSpoolProfiles = () => refresh(selected())
  }

  function render(proposal: ResearchProposal, context: EditorContext, action = 'fill', field = '', selection: Unit[] = []) {
    clearResults()
    bound = context
    boundProposal = proposal
    const fields = context.fields
    renderSpoolProfiles(proposal, fields, selection)
    const unitFor = (field: string, value: Value): Unit => ({ [field]: { old: fields[field] ?? null, value } })
    duplicate = proposal.issues.some(issue => issue.code === 'duplicate')
    const alternatives = new Set<string>()
    const checks = document.createElement('details')
    checks.className = 'research-checks research-note'
    const summary = document.createElement('summary')
    checks.append(summary)
    const seenChecks = new Set<string>()
    for (const issue of proposal.issues) {
      const p = document.createElement('p')
      if (sourceNotes.has(issue.code)) p.className = 'research-note'
      p.textContent = issueText(issue)
      const productUrl = issue.product_url && /^https?:\/\//i.test(issue.product_url) ? issue.product_url : undefined
      const actions = productUrl ? document.createElement('span') : null
      if (issue.filament_id && issue.code === 'duplicate') {
        p.append(' ', externalLink(`/filaments/${issue.filament_id}`, t('filaBrain.openExisting')))
      }
      if (issue.url && /^https?:\/\//i.test(issue.url)) {
        const url = issue.url
        const websiteFound = issue.code === 'manufacturer_website_found'
        if (websiteFound) {
          const note = document.createElement('span')
          note.className = 'research-note'
          note.textContent = p.textContent
          p.className = 'research-website-result'
          p.replaceChildren(note, document.createElement('br'))
        } else if (!productUrl) {
          p.append(' ')
        }
        const sourceLink = externalLink(url, websiteFound ? url : t('filaBrain.openSource'))
        const linkRow = actions ?? p
        linkRow.append(sourceLink)
        if (websiteFound && editor?.mode === 'edit' && status.can_update_manufacturer &&
            context.filament_id && typeof fields.manufacturer_id === 'number') {
          const saveWebsite = document.createElement('button')
          saveWebsite.type = 'button'
          saveWebsite.textContent = t('filaBrain.saveManufacturerWebsite')
          saveWebsite.addEventListener('click', async () => {
            saveWebsite.disabled = true
            try {
              const current = await target()
              if (current.filament_id !== context.filament_id ||
                  current.record_token !== context.record_token || current.fields.manufacturer_id !== fields.manufacturer_id) {
                throw new Error(t('filaBrain.manufacturerChanged'))
              }
              await api.put(`${route}/filaments/${context.filament_id}/manufacturer-website`, {
                manufacturer_id: fields.manufacturer_id, name: issue.name, url, record_token: context.record_token,
              })
              saveWebsite.replaceWith(t('filaBrain.manufacturerWebsiteSaved'))
            } catch (error) {
              notice.textContent = errorText(error)
              saveWebsite.disabled = false
            }
          })
          const action = document.createElement('span')
          action.className = 'research-website-action'
          action.setAttribute('aria-live', 'polite')
          action.append(saveWebsite)
          p.append(action)
        }
      }
      if (productUrl && actions) {
        actions.className = 'research-match-actions'
        const choose = document.createElement('button')
        choose.type = 'button'
        choose.className = 'fm-btn fm-btn-outline research-page-action'
        const label = t('filaBrain.useProductPage')
        choose.append(researchMark.cloneNode(true), label)
        choose.addEventListener('click', () => {
          if (form.reportValidity()) void search('fill', '', label, productUrl).catch(showResearchError)
        })
        actions.append(choose)
        p.append(actions)
      }
      if (checkedSources.has(issue.code)) {
        const check = issue.url || p.textContent
        if (!seenChecks.has(check)) {
          seenChecks.add(check)
          checks.append(p)
        }
      } else results.append(p)
      const field = issue.field
      if (field && nativeFields.has(field) && issue.options?.length && blank(fields[field]) &&
          !alternatives.has(field)) {
        alternatives.add(field)
        const group = document.createElement('fieldset')
        const legend = document.createElement('legend')
        legend.textContent = researchFieldLabel(field)
        group.append(legend)
        if (issue.requires_product_choice) {
          appendVariantChoices(group, field, issue.options)
          results.append(group)
          continue
        }
        const none = document.createElement('label')
        const noChoice = document.createElement('input')
        noChoice.type = 'radio'
        noChoice.name = `choice-${field}`
        noChoice.checked = true
        none.append(noChoice, t('filaBrain.leaveBlank'))
        group.append(none)
        const preferred = proposal.suggestions.find(suggestion => suggestion.field === field)
        for (const candidate of issue.options) {
          group.append(option(unitFor(field, candidate.value), candidate.sources,
            researchRecommendation(issue, candidate.value, preferred?.value), noChoice.name))
        }
        results.append(group)
      }
    }
    for (const { field, value, sources } of proposal.suggestions) {
      if (!nativeFields.has(field) || alternatives.has(field) || !blank(fields[field])) continue
      if (field === 'color_id' && fields.color_mode && fields.color_mode !== 'single') continue
      results.append(option(unitFor(field, value), sources))
    }
    for (const proposalUnit of proposal.proposals || []) {
      const changes = Object.entries(proposalUnit.fields)
      if (!changes.length || changes.some(([field, change]) => !nativeFields.has(field) || !equal(fields[field], change.old))) continue
      const unit = Object.fromEntries(changes.map(([field, change]) => [field, { old: fields[field] ?? null, value: change.value }]))
      const sources = proposalUnit.sources.map(source => ({ title: source.title, url: source.url, evidence: source.quote }))
      results.append(option(unit, sources, proposalUnit.reason))
    }
    if (seenChecks.size) {
      summary.textContent = t('filaBrain.sourcesChecked', { count: seenChecks.size })
      results.append(checks)
    }
    const researchable = action === 'standardize'
      ? [...new Set((proposal.proposals || []).flatMap(unit => Object.keys(unit.fields)))]
      : [...nativeFields].filter(key => blank(fields[key]))
    followUpScope.replaceChildren(new Option(t('filaBrain.followUpAllFields'), ''),
      ...researchable.map(key => new Option(researchFieldLabel(key), key)).sort((a, b) => a.text.localeCompare(b.text)))
    if (researchable.includes(field)) followUpScope.value = field
    followUpControls.hidden = !researchable.length
    updateSave()
    reply.hidden = !!editor && !results.childElementCount
  }

  async function search(action: string, prompt = '', label?: string, correctedUrl?: string, referenceUrl?: string) {
    if (busy || saving) return
    const revision = formRevision
    const context = await target()
    if (revision !== formRevision || busy || saving) return
    const formData = new FormData(form)
    const productInput = form.elements.namedItem('product_url') as HTMLInputElement
    const productUrl = correctedUrl || String(formData.get('product_url') || '').trim()
    const sku = String(formData.get('sku_hint') || '').trim()
    const product = identity(context.fields)
    const previous = boundProposal
    const spoolLookup = action === 'spool'
    if (spoolLookup) action = 'fill'
    const scope = prompt && !correctedUrl && !spoolLookup ? followUpScope.value : ''
    const sameContext = previous && bound && productUrl === lastProductUrl && sku === lastSku &&
      context.mode === bound.mode && context.filament_id === bound.filament_id && context.record_token === bound.record_token &&
      context.fields.manufacturer_id === bound.fields.manufacturer_id && context.fields.color_id === bound.fields.color_id &&
      JSON.stringify(product) === JSON.stringify(identity(bound.fields))
    const focusing = scope && sameContext && action === lastAction
    const currentSelection = sameContext ? selected() : []
    const acceptedProduct = context.mode === 'create'
      ? currentSelection.filter(unit => Object.keys(unit).every(field => productFields.includes(field)))
      : []
    const acceptedProductValues = acceptedFields({}, acceptedProduct)
    const lookupContext = acceptedProduct.length
      ? { ...context, fields: spoolLookupFields(context.fields, acceptedProduct) }
      : context
    const lookupProduct = identity(lookupContext.fields)
    const lookupHint = !product.sku && lookupProduct.sku ? '' : sku
    const actionLabel = spoolLookup ? t('filaBrain.findSpoolSpecs') : buttons[action === 'standardize' ? 1 : 0].textContent?.trim() || ''
    const proposalTitle = [lookupProduct.manufacturer, lookupProduct.designation].filter(Boolean).join(' ') || productUrl || lookupHint
    if (!lookupProduct.designation && !lookupProduct.sku && !productUrl && !lookupHint) {
      conversation.hidden = false
      notice.textContent = t('filaBrain.chatResearchStart')
      return
    }
    if (action === 'fill' && !spoolLookup && chat.needsModelSelection) {
      notice.textContent = t('filaBrain.chooseModel')
      return
    }
    showInChat()
    busy = true
    buttons.forEach(button => { button.disabled = true })
    updateSave()
    notice.textContent = ''
    conversation.hidden = false
    if (action === 'fill' && !spoolLookup) chat.preserveModel()
    if (!prompt) {
      optionsSummary.hidden = false
      searchOptions.open = false
    }
    try {
      await chat.researchTurn(prompt || label || (proposalTitle && !editor ? `${actionLabel} · ${proposalTitle}` : actionLabel), async () => {
        try {
          let proposal = await api.post<ResearchProposal>(route + '/proposals', {
            ...proposalTarget(lookupContext),
            record_token: context.record_token,
            action,
            product_url: productUrl || undefined,
            reference_url: referenceUrl,
            sku_hint: lookupHint,
            guidance: referenceUrl ? prompt.replace(/https?:\/\/[^\s<>()]+/gi, '').trim() : prompt,
            ...(spoolLookup ? {
              spool_lookup: true,
              spool_package_weight_g: packageWeight(acceptedFields({}, currentSelection)) ?? undefined,
            } : action === 'fill' ? chat.model : undefined),
          })
          if (revision !== formRevision) return { reply: t('filaBrain.identityChanged') }
          const unsafeToMerge = proposal.issues.some(issue => issue.code === 'duplicate' ||
            ((issue.code === 'ambiguous_match' || issue.requires_product_choice) &&
              !previous?.issues.some(prior => JSON.stringify(prior) === JSON.stringify(issue))))
          if (previous && !spoolLookup && !unsafeToMerge && acceptedProduct.length) {
            proposal = carryAcceptedProductFields(previous, proposal, acceptedProduct)
          }
          const keep = focusing && !unsafeToMerge && sameResolvedProduct(previous, proposal, acceptedProductValues)
          let selection = []
          if (keep) {
            selection = currentSelection.filter(unit => !(scope in unit))
            proposal = mergeFollowUp(previous, proposal, scope)
          } else if (spoolLookup) {
            selection = currentSelection
          } else if (!unsafeToMerge) {
            selection = acceptedProduct
          }
          const profile = !keep && !spoolLookup && !unsafeToMerge && sameContext && action === 'fill' &&
            previous.spool_profiles?.find(item => item.id === previous.spool_profile_id)
          const proposedWeight = proposal.suggestions.find(item => item.field === 'raw_material_weight_g')?.value
          if (profile && matchesSpoolPackage(profile, proposedWeight) && sameResolvedProduct(previous, proposal, acceptedProductValues, {
            ...context.fields,
            manufacturer_sku: product.sku || sku,
            raw_material_weight_g: product.weight_g ?? null,
          })) {
            selection.push(...keepSpoolMeasurements(currentSelection.filter(unit => !(scope in unit)), profile, proposal))
            proposal = {
              ...proposal, spool_profiles: previous.spool_profiles, spool_profile_id: profile.id,
              spool_lookup_key: previous.spool_lookup_key,
            }
          }
          if (spoolLookup && sameContext) proposal = {
            ...previous, spool_profiles: proposal.spool_profiles, spool_reply: proposal.spool_reply,
            spool_profile_id: previous.spool_profile_id,
            issues: [...previous.issues.filter(issue => issue.code !== 'duplicate'), ...proposal.issues],
          }
          if (spoolLookup) proposal.spool_lookup_key = spoolLookupKey(lookupContext.fields, lookupHint)
          if (correctedUrl) {
            productInput.value = correctedUrl
            formData.set('product_url', correctedUrl)
          }
          lastProductUrl = productUrl
          lastSku = sku
          render(proposal, context, action, keep ? scope : '', selection)
          restoreSelection(selection)
          lastAction = action
          return {
            reply: proposal.spool_reply || t(results.childElementCount ? 'filaBrain.researchReview' : 'filaBrain.noSuggestions'),
            draft: { fields: Object.fromEntries([...formData].map(([key, value]) => [key, String(value)])),
              context, proposal, action, selection: selected() },
          }
        } catch (error) {
          return { reply: errorText(error) }
        }
      })
    } finally {
      buttons.forEach(button => { button.disabled = false })
      busy = false
      updateSave()
    }
  }

  form.addEventListener('submit', event => {
    event.preventDefault()
    void search((event.submitter as HTMLButtonElement | null)?.value || 'fill').catch(showResearchError)
  })

  function reset() {
    formRevision++
    form.reset()
    searchOptions.open = true
    optionsSummary.hidden = true
    conversation.hidden = true
    clearResults()
    notice.textContent = ''
    lastAction = 'fill'
    if (editor) buttons[0].before(chat.picker)
  }

  function archivedValue(field: string, value: Value, sources: ResearchOption['sources']) {
    const item = document.createElement('div')
    item.className = 'research-history-value'
    item.textContent = `${researchFieldLabel(field)}: ${valueText(value)}`
    appendSources(item, sources)
    return item
  }

  function appendVariantChoices(group: HTMLElement, field: string, options: ResearchOption[]) {
    const warning = document.createElement('p')
    warning.textContent = t('filaBrain.chooseProductFirst')
    group.append(warning)
    for (const option of options) group.append(archivedValue(field, option.value, option.sources))
  }

  form.addEventListener('input', () => {
    formRevision++
    clearResults()
    notice.textContent = ''
  })

  chat.setResearch({
    start: showInChat,
    reset,
    archive(proposal) {
      const details = document.createElement('details')
      details.className = 'research-history research-note'
      const heading = document.createElement('summary')
      heading.textContent = t('filaBrain.earlierFindings')
      details.append(heading)
      const profile = proposal.spool_profiles?.find(item => item.id === proposal.spool_profile_id)
      const source = profile && spoolSource(profile.url)
      if (profile && source) for (const [field, measurement] of spoolFields) {
        const value = profile[measurement]
        if (typeof value === 'number') details.append(archivedValue(field, value, [{
          title: profile.source, url: source.href, evidence: profile.title,
        }]))
      }
      for (const result of proposal.suggestions) details.append(archivedValue(result.field, result.value, result.sources))
      for (const unit of proposal.proposals || []) {
        for (const [field, change] of Object.entries(unit.fields)) {
          details.append(archivedValue(field, change.value, unit.sources.map(source => ({ ...source, evidence: source.quote }))))
        }
      }
      const seenIssues = new Set<string>()
      for (const issue of proposal.issues) {
        const explanation = issueText(issue)
        const url = issue.url && /^https?:\/\//i.test(issue.url) ? issue.url : undefined
        const line = document.createElement('p')
        line.textContent = explanation
        if (url) line.append(' ', externalLink(url, t('filaBrain.openSource')))
        if (issue.field && issue.options?.length) {
          const group = document.createElement('fieldset')
          const legend = document.createElement('legend')
          legend.textContent = researchFieldLabel(issue.field)
          group.append(legend, line)
          if (issue.requires_product_choice) {
            appendVariantChoices(group, issue.field, issue.options)
          } else for (const option of issue.options) group.append(archivedValue(issue.field, option.value, option.sources))
          details.append(group)
        } else {
          const key = JSON.stringify([explanation, url])
          if (!seenIssues.has(key)) {
            seenIssues.add(key)
            details.append(line)
          }
        }
      }
      return details
    },
    async resume(draft) {
      await pendingOptions
      for (const [key, value] of Object.entries(draft.fields)) {
        const field = form.elements.namedItem(key)
        if (field instanceof HTMLInputElement || field instanceof HTMLSelectElement) field.value = value
      }
      if (!editor) manufacturer.value = String(draft.context.fields.manufacturer_name
        || manufacturers.find(item => item.id === draft.context.fields.manufacturer_id)?.name || '')
      const context = await target()
      if (draft.context.mode !== context.mode || draft.context.filament_id !== context.filament_id ||
          (!blank(draft.context.fields.manufacturer_id) && draft.context.fields.manufacturer_id !== context.fields.manufacturer_id) ||
          JSON.stringify(identity(draft.context.fields)) !== JSON.stringify(identity(context.fields))) {
        conversation.hidden = false
        notice.textContent = t('filaBrain.identityChanged')
        return false
      }
      if (context.mode === 'edit' && (!context.record_token || context.record_token !== draft.context.record_token)) {
        conversation.hidden = false
        notice.textContent = t('filaBrain.researchHistoryReadOnly')
        return false
      }
      conversation.hidden = false
      lastAction = draft.action
      lastProductUrl = (draft.fields.product_url || '').trim()
      lastSku = (draft.fields.sku_hint || '').trim()
      optionsSummary.hidden = false
      searchOptions.open = false
      const selection = draft.selection || []
      render(draft.proposal, context, draft.action, '', selection)
      restoreSelection(selection)
      return true
    },
    followUp(prompt) {
      const url = prompt.match(/https?:\/\/[^\s<>()]+/i)?.[0]
      const spoolQuestion = /\b(?:spool|reel|bobine|spule)\b/i.test(prompt) &&
        /\b(?:tare|weight|weigh|dimensions?|diameter|width|photo|picture|poids|diamètre|largeur|leergewicht|durchmesser|breite)\b/i.test(prompt)
      const spoolDescription = boundProposal?.spool_profiles?.length && !prompt.includes('?') &&
        !/^\s*(?:search|find|look|recheck|check|verify)\b/i.test(prompt) &&
        /\b(?:clear|plastic|cardboard|slots?|cutouts?|transparent|plastique|carton|kunststoff|pappe|schlitze)\b/i.test(prompt) &&
        prompt.split(/\s+/).length <= 20
      if (!url && (spoolQuestion || spoolDescription)) {
        return search('spool', prompt)
      }
      if (url && !/\.pdf(?:[?#]|$)/i.test(url)) {
        const instruction = prompt.slice(0, prompt.indexOf(url)).trim()
        const choosesProduct = /^(?:use|choose|select|switch to|change to|verwende|nutze|wähle|wechsle zu|utilise|choisis|sélectionne|passe à)(?:\s|$)/i.test(instruction) &&
          /\b(?:product|variant|refill|spool|produkt|variante|nachfüllung|spule|produit|recharge|bobine)\b/i.test(instruction)
        if (url === prompt || choosesProduct) return search('fill', '', prompt, url)
      }
      return search(url ? 'fill' : lastAction, prompt, undefined, undefined, url)
    },
  })

  save.addEventListener('click', async () => {
    if (!bound || save.disabled) return
    if (!editor && chat.savedDraft) {
      clearResults()
      chat.completeResearch(chat.draftId, true)
      return
    }
    const snapshot = bound
    saving = true
    if (editor) chat.setStaging(true)
    updateSave()
    try {
      const context = await target()
      if (bound !== snapshot || context.mode !== snapshot.mode || context.filament_id !== snapshot.filament_id ||
          context.record_token !== snapshot.record_token || JSON.stringify(identity(context.fields)) !== JSON.stringify(identity(snapshot.fields))) {
        clearResults()
        throw new Error(t('filaBrain.identityChanged'))
      }
      refreshSpoolProfiles?.()
      const units = selected()
      if (editor) {
        const { staged, skipped } = await hostRequest<{ staged: string[]; skipped: string[] }>('filaman:filament-stage', { units })
        if (bound !== snapshot) return
        if (!staged.length) {
          notice.textContent = t('filaBrain.identityChanged')
          return
        }
        const fields = { ...context.fields }
        for (const unit of units) {
          for (const [field, change] of Object.entries(unit)) {
            if (staged.includes(field)) fields[field] = change.value
          }
        }
        chat.archiveResearch()
        if (boundProposal) render(boundProposal, { ...context, fields }, lastAction)
        notice.textContent = t(staged.length === 1 ? 'filaBrain.stagedOne' : 'filaBrain.stagedMany', { count: staged.length })
          + (skipped.length ? ' ' + t('filaBrain.stagedSkipped', { count: skipped.length }) : '')
        return
      }
      const base = Object.fromEntries(draftFields.filter(field => !blank(context.fields[field]))
        .map(field => [field, context.fields[field]]))
      const data = acceptedFields(base, units)
      if (!Number(data.manufacturer_id) || !data.designation || !data.material_type || !(Number(data.diameter_mm) > 0)) {
        optionsSummary.hidden = false
        searchOptions.open = advanced.open = true
        throw new Error(t('filaBrain.draftRequiredFields'))
      }
      if (!manufacturers.some(item => item.id === data.manufacturer_id) ||
          data.color_id && ![...palette.options].some(item => Number(item.value) === data.color_id)) {
        throw new Error(t('filaBrain.draftInvalidReferences'))
      }
      draft = data
      const preview = element('create-preview')
      preview.replaceChildren()
      for (const [field, value] of Object.entries(draft)) {
        const dt = document.createElement('dt')
        const dd = document.createElement('dd')
        dt.textContent = researchFieldLabel(field)
        const reference = field === 'manufacturer_id' ? manufacturers.find(item => item.id === value)?.name
          : field === 'color_id' ? [...palette.options].find(item => Number(item.value) === value)?.text : undefined
        dd.textContent = reference || valueText(value)
        preview.append(dt, dd)
      }
      element('create-message').textContent = ''
      confirm.showModal()
    } catch (error) {
      notice.textContent = errorText(error)
    } finally {
      saving = false
      if (editor) chat.setStaging(false)
      updateSave()
    }
  })
  element('create-cancel').addEventListener('click', () => confirm.close())
  confirm.addEventListener('cancel', event => { if (createButton.disabled) event.preventDefault() })
  createButton.addEventListener('click', async () => {
    const draftId = chat.draftId
    if (!draft || !draftId || createButton.disabled) return
    if (chat.savedDraft) {
      confirm.close()
      clearResults()
      draft = undefined
      chat.completeResearch(draftId, true)
      return
    }
    const cancel = element<HTMLButtonElement>('create-cancel')
    const { color_id, ...data } = draft
    createButton.disabled = cancel.disabled = true
    try {
      const result = await api.post<{ already_saved: boolean }>(`${route}/saved-filaments/${encodeURIComponent(draftId)}`, {
        ...data, ...(color_id ? { colors: [{ color_id }] } : {}),
      })
      confirm.close()
      clearResults()
      draft = undefined
      chat.completeResearch(draftId, result.already_saved)
    } catch (error) {
      element('create-message').textContent = errorText(error)
    } finally {
      createButton.disabled = cancel.disabled = false
    }
  })

  window.addEventListener('focus', async () => {
    if (form.closest<HTMLElement>('[data-view]')?.hidden) return
    try {
      const current = await api.get<Status>(route + '/status', { cache: 'no-store' })
      if (status.chat_model !== current.chat_model || status.chat_endpoint_id !== current.chat_endpoint_id) {
        document.dispatchEvent(new CustomEvent('filaman:model-change', {
          detail: { chat_model: current.chat_model, chat_endpoint_id: current.chat_endpoint_id },
        }))
      }
    } catch { /* Keep the last known settings while the server is unreachable. */ }
  })
  if (editor) showInChat(false)
  return (input?: SpoolResearchInput) => {
    if (!showInChat(true, true, !!input) || !input) return
    manufacturer.value = input.manufacturer || ''
    for (const [name, value] of Object.entries({
      designation: input.designation,
      material_type: input.material,
      sku_hint: input.sku,
      product_url: input.productUrl,
    })) {
      if (value) form.querySelector<HTMLInputElement>(`[name="${name}"]`)!.value = value
    }
    if (input.material || input.sku) advanced.open = true
  }
}
