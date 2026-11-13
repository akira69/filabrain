import { element, errorText, hostRequest, type Status } from './main'
import { api, fetchAllPages, getCsrfToken } from './shared/api'
import { researchFieldLabel, researchOptionValue, type ResearchIdentity, type ResearchOption, type ResearchProposal } from './shared/filabrain'
import { t } from './shared/i18n'
import { mountModelPicker } from './shared/model-picker'

type Value = ResearchOption['value'] | boolean | null
type Fields = Record<string, Value>
type Unit = Record<string, { old: Value; value: Value }>
export type EditorContext = { mode: 'create' | 'edit'; filament_id: number | null; fields: Fields }
export type SelectionContext = { mode: 'selection'; filament_ids: number[] }

const route = '/filabrain'
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
const numericDraft = new Set(['manufacturer_id', 'color_id', 'diameter_mm'])
const blank = (value: Value | undefined) => value == null || value === ''
const equal = (a: Value | undefined, b: Value | undefined) => blank(a) && blank(b) || a === b
const identity = (fields: Fields): ResearchIdentity => ({
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
    p.className = 'evidence'
    p.append(source.url && /^https?:\/\//i.test(source.url) ? externalLink(source.url, source.title) : source.title,
      source.evidence ? ` — ${source.evidence}` : '')
    row.append(p)
  }
}

export function mountResearch(status: Status, editor?: EditorContext) {
  const form = element<HTMLFormElement>('product-research')
  const results = element('research-results')
  const notice = element('research-status')
  const save = element<HTMLButtonElement>('save-filament')
  const confirm = element<HTMLDialogElement>('create-confirm')
  const createButton = element<HTMLButtonElement>('create-filament')
  const saved = element<HTMLAnchorElement>('saved-filament')
  const pairing = element<HTMLDetailsElement>('research-pairing')
  const pair = element<HTMLButtonElement>('pair-client')
  const credentials = element('pair-credentials')
  const manufacturer = form.elements.namedItem('manufacturer_id') as HTMLSelectElement
  const palette = form.elements.namedItem('color_id') as HTMLSelectElement
  const buttons = [...form.querySelectorAll<HTMLButtonElement>('button[type=submit]')]
  const followUp = element<HTMLFormElement>('research-follow-up')
  const guidance = element<HTMLTextAreaElement>('research-guidance')
  const sendFollowUp = followUp.querySelector<HTMLButtonElement>('button[type=submit]')!
  const startActions = element('research-start-actions')
  const picker = startActions.querySelector<HTMLElement>('.chat-model-picker')!
  const advanced = element<HTMLDetailsElement>('research-advanced-fields')
  const modelPicker = mountModelPicker(form, status.chat_model)
  const searchOptions = element<HTMLDetailsElement>('research-options')
  const optionsSummary = searchOptions.querySelector('summary')!
  const conversation = element('research-conversation')
  const transcript = element('research-transcript')
  const reply = element('research-reply')
  const mcpTarget = (context: EditorContext) => ({
    mode: context.mode, filament_id: context.filament_id, identity: identity(context.fields),
  })
  let choices = new Map<HTMLInputElement, Unit>()
  let bound: EditorContext | undefined
  let draft: Fields | undefined
  let pairId: string | undefined
  let pairGeneration = 0
  let pairPoll = 0
  let busy = false
  let saving = false
  let duplicate = false
  let lastAction = 'fill'
  let formRevision = 0
  function showFollowUp(visible: boolean) {
    if (visible) sendFollowUp.before(picker)
    else buttons[0].before(picker)
    followUp.hidden = !visible
  }
  document.addEventListener('filaman:model-change', () => { void modelPicker.setDefault(status.chat_model) })

  if (editor) {
    document.documentElement.dataset.researchEditor = ''
    element('draft-fields').hidden = true
    advanced.hidden = true
    element('research-identity').append(element('research-sku-hint'))
    element('research-heading').textContent = editor.mode === 'edit' ? 'Research this filament' : 'Research the new filament draft'
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
    void options('/api/v1/manufacturers', manufacturer)
    void options('/api/v1/colors', palette)
  }

  function localFields() {
    const formData = new FormData(form)
    const fields: Fields = {}
    for (const key of draftFields) {
      const value = String(formData.get(key) || '').trim()
      if (value) fields[key] = numericDraft.has(key) ? Number(value) : value
    }
    if (fields.manufacturer_id) fields.manufacturer_name = manufacturer.selectedOptions[0].textContent
    return fields
  }

  function target() {
    return editor ? hostRequest<EditorContext>('filaman:filament-context')
      : Promise.resolve<EditorContext>({ mode: 'create', filament_id: null, fields: localFields() })
  }

  function clearResults() {
    choices.clear()
    results.replaceChildren()
    bound = undefined
    updateSave()
    reply.hidden = true
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

  function updateSave() {
    const count = editor ? selected().reduce((total, unit) => total + Object.keys(unit).length, 0) : 0
    save.disabled = !bound || duplicate || busy || saving || (!!editor && count === 0)
    if (editor) {
      save.textContent = t(count === 0 ? 'filaBrain.stageFields' : count === 1 ? 'filaBrain.stageOne' : 'filaBrain.stageMany', { count })
    }
  }

  results.addEventListener('change', updateSave)

  function option(unit: Unit, sources: ResearchOption['sources'], reason?: string, group?: string) {
    const row = document.createElement('div')
    row.className = 'proposal-row'
    const label = document.createElement('label')
    const control = document.createElement('input')
    control.type = group ? 'radio' : 'checkbox'
    if (group) control.name = group
    choices.set(control, unit)
    label.append(control, Object.entries(unit).map(([field, { old, value }]) => {
      const name = researchFieldLabel(field)
      const select = field === 'manufacturer_id' ? manufacturer : field === 'color_id' ? palette : undefined
      const display = select?.querySelector<HTMLOptionElement>(`option[value="${Number(value)}"]`)?.text
        || (select && sources[0]?.evidence) || valueText(value)
      return `${name}: ${valueText(old)} → ${display}`
    }).join(' · '))
    row.append(label)
    if (reason) {
      const p = document.createElement('p')
      p.textContent = reason
      row.append(p)
    }
    appendSources(row, sources)
    return row
  }

  function render(proposal: ResearchProposal, context: EditorContext) {
    clearResults()
    bound = context
    const fields = context.fields
    const unitFor = (field: string, value: Value): Unit => ({ [field]: { old: fields[field] ?? null, value } })
    duplicate = proposal.issues.some(issue => issue.code === 'duplicate')
    const alternatives = new Set<string>()
    for (const issue of proposal.issues) {
      const p = document.createElement('p')
      const key = `filaBrain.${issue.code}`
      const explanation = t(key, { name: issue.name || '', source: issue.source || '' })
      p.textContent = explanation === key ? issue.code.replaceAll('_', ' ') : explanation
      if (issue.reason) p.append(` (${issue.reason})`)
      if (issue.filament_id && issue.code === 'duplicate') {
        p.append(' ', externalLink(`/filaments/${issue.filament_id}`, t('filaBrain.openExisting')))
      }
      if (issue.url && /^https?:\/\//i.test(issue.url)) {
        const url = issue.url
        const websiteFound = issue.code === 'manufacturer_website_found'
        if (websiteFound) p.className = 'research-website-result'
        p.append(' ', externalLink(url, websiteFound ? url : t('filaBrain.openSource')))
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
                  current.fields.manufacturer_id !== fields.manufacturer_id) {
                throw new Error(t('filaBrain.manufacturerChanged'))
              }
              await api.put(`${route}/filaments/${context.filament_id}/manufacturer-website`, {
                manufacturer_id: fields.manufacturer_id, name: issue.name, url,
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
      if (issue.product_url && /^https?:\/\//i.test(issue.product_url)) {
        const choose = document.createElement('button')
        choose.type = 'button'
        choose.textContent = t('filaBrain.useProductPage')
        choose.addEventListener('click', () => {
          ;(form.elements.namedItem('product_url') as HTMLInputElement).value = issue.product_url!
          form.requestSubmit(buttons[0])
        })
        p.append(' ', choose)
      }
      results.append(p)
      const field = issue.field
      if (field && nativeFields.has(field) && issue.options?.length && blank(fields[field]) &&
          !alternatives.has(field) && !issue.requires_product_choice) {
        alternatives.add(field)
        const group = document.createElement('fieldset')
        const legend = document.createElement('legend')
        legend.textContent = researchFieldLabel(field)
        group.append(legend)
        const none = document.createElement('label')
        const noChoice = document.createElement('input')
        noChoice.type = 'radio'
        noChoice.name = `choice-${field}`
        noChoice.checked = true
        none.append(noChoice, t('filaBrain.leaveBlank'))
        group.append(none)
        for (const candidate of issue.options) {
          group.append(option(unitFor(field, candidate.value), candidate.sources, undefined, noChoice.name))
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
    updateSave()
    reply.hidden = !!editor && !results.childElementCount
  }

  function message(text: string, fromUser = false) {
    const item = document.createElement('p')
    item.className = fromUser ? 'chat-message chat-user' : 'chat-message research-assistant'
    item.textContent = text
    transcript.append(item)
    return item
  }

  async function search(action: string, prompt = '') {
    if (busy || saving) return
    const revision = formRevision
    const selectedModel = modelPicker.selected
    busy = true
    modelPicker.setBusy(true)
    buttons.forEach(button => { button.disabled = true })
    sendFollowUp.disabled = true
    updateSave()
    saved.hidden = true
    notice.textContent = ''
    conversation.hidden = false
    message(prompt || (action === 'standardize' ? buttons[1].textContent! : buttons[0].textContent!), true)
    const answer = message(t('filaBrain.searching'))
    if (!prompt) {
      optionsSummary.hidden = false
      searchOptions.open = false
    }
    if (editor) answer.scrollIntoView({ block: 'nearest' })
    try {
      const context = await target()
      if (revision !== formRevision) return
      const formData = new FormData(form)
      const proposal = await api.post<ResearchProposal>(route + '/proposals', {
        ...mcpTarget(context),
        action,
        product_url: String(formData.get('product_url') || '').trim() || undefined,
        sku_hint: String(formData.get('sku_hint') || '').trim(),
        guidance: prompt,
        model_id: action === 'fill' ? selectedModel : undefined,
      })
      if (revision !== formRevision) return
      render(proposal, context)
      answer.textContent = results.childElementCount ? t('filaBrain.researchReview') : t('filaBrain.noSuggestions')
      if (prompt && guidance.value.trim() === prompt) guidance.value = ''
    } catch (error) {
      if (revision === formRevision) answer.textContent = errorText(error)
    } finally {
      if (revision === formRevision) {
        lastAction = action
        showFollowUp(true)
      }
      buttons.forEach(button => { button.disabled = false })
      sendFollowUp.disabled = false
      modelPicker.setBusy(false)
      busy = false
      updateSave()
    }
  }

  form.addEventListener('submit', event => {
    event.preventDefault()
    void search((event.submitter as HTMLButtonElement | null)?.value || 'fill')
  })

  followUp.addEventListener('submit', event => {
    event.preventDefault()
    const prompt = guidance.value.trim()
    if (prompt) void search(lastAction, prompt)
  })
  guidance.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return
    event.preventDefault()
    if (!busy) followUp.requestSubmit()
  })

  form.addEventListener('input', () => {
    formRevision++
    showFollowUp(false)
    guidance.value = ''
    transcript.replaceChildren()
    conversation.hidden = true
    clearResults()
    saved.hidden = true
    notice.textContent = ''
    void disconnect()
  })

  save.addEventListener('click', async () => {
    if (!bound || save.disabled) return
    const snapshot = bound
    const units = selected()
    saving = true
    updateSave()
    try {
      const context = await target()
      if (bound !== snapshot || context.mode !== snapshot.mode || context.filament_id !== snapshot.filament_id ||
          JSON.stringify(identity(context.fields)) !== JSON.stringify(identity(snapshot.fields))) {
        clearResults()
        throw new Error(t('filaBrain.identityChanged'))
      }
      if (editor) {
        const { staged, skipped } = await hostRequest<{ staged: string[]; skipped: string[] }>('filaman:filament-stage', { units })
        clearResults()
        notice.textContent = `${staged.length} field${staged.length === 1 ? '' : 's'} staged. Save in the FilaMan editor to keep the changes.`
          + (skipped.length ? ` ${skipped.length} skipped because the form changed or the field is unavailable.` : '')
        return
      }
      const data: Fields = Object.fromEntries(draftFields.filter(field => !blank(context.fields[field]))
        .map(field => [field, context.fields[field]]))
      for (const unit of units) for (const [field, change] of Object.entries(unit)) data[field] = change.value
      if (!Number(data.manufacturer_id) || !data.designation || !data.material_type || !(Number(data.diameter_mm) > 0)) {
        optionsSummary.hidden = false
        searchOptions.open = advanced.open = true
        throw new Error('Choose or enter a manufacturer, product name, material and diameter before saving.')
      }
      if (![...manufacturer.options].some(item => Number(item.value) === data.manufacturer_id) ||
          data.color_id && ![...palette.options].some(item => Number(item.value) === data.color_id)) {
        throw new Error('Choose a manufacturer and palette color already available in FilaMan.')
      }
      draft = data
      const preview = element('create-preview')
      preview.replaceChildren()
      for (const [field, value] of Object.entries(draft)) {
        const dt = document.createElement('dt')
        const dd = document.createElement('dd')
        dt.textContent = researchFieldLabel(field)
        dd.textContent = (field === 'manufacturer_id' ? [...manufacturer.options] : field === 'color_id' ? [...palette.options] : [])
          .find(item => Number(item.value) === value)?.text || valueText(value)
        preview.append(dt, dd)
      }
      element('create-message').textContent = ''
      confirm.showModal()
    } catch (error) {
      notice.textContent = errorText(error)
    } finally {
      saving = false
      updateSave()
    }
  })
  element('create-cancel').addEventListener('click', () => confirm.close())
  confirm.addEventListener('cancel', event => { if (createButton.disabled) event.preventDefault() })
  createButton.addEventListener('click', async () => {
    if (!draft || createButton.disabled) return
    const cancel = element<HTMLButtonElement>('create-cancel')
    const { color_id, ...data } = draft
    createButton.disabled = cancel.disabled = true
    try {
      const created = await api.post<{ id: number }>('/filaments', {
        ...data, ...(color_id ? { colors: [{ color_id }] } : {}),
      })
      confirm.close()
      clearResults()
      draft = undefined
      saved.href = `/filaments/${created.id}`
      saved.hidden = false
      notice.textContent = 'Filament saved.'
    } catch (error) {
      element('create-message').textContent = errorText(error)
    } finally {
      createButton.disabled = cancel.disabled = false
    }
  })

  function revoke(id: string) {
    return api.delete(route + '/pairings/' + encodeURIComponent(id), { keepalive: true, csrfToken: getCsrfToken() }).catch(() => {})
  }

  function disconnect() {
    pairGeneration++
    window.clearTimeout(pairPoll)
    credentials.hidden = true
    element<HTMLInputElement>('pair-url').value = ''
    element<HTMLInputElement>('pair-token').value = ''
    pair.disabled = false
    const id = pairId
    pairId = undefined
    if (id) return revoke(id)
  }

  function showPairing(enabled: boolean) {
    pairing.hidden = !enabled
    if (!enabled) {
      pairing.open = false
      void disconnect()
    }
  }
  showPairing(status.mcp_enabled)
  document.addEventListener('filaman:mcp-state', event => showPairing((event as CustomEvent<boolean>).detail))
  window.addEventListener('focus', async () => {
    if (form.closest<HTMLElement>('[data-view]')?.hidden) return
    try {
      const current = await api.get<Status>(route + '/status', { cache: 'no-store' })
      if (status.chat_model !== current.chat_model) {
        status.chat_model = current.chat_model
        void modelPicker.setDefault(current.chat_model)
      }
      showPairing(current.mcp_enabled)
    } catch { /* Pairing endpoints enforce current access. */ }
  })
  element('pair-revoke').addEventListener('click', () => { void disconnect() })
  window.addEventListener('pagehide', () => { void disconnect() })

  pair.addEventListener('click', async () => {
    const generation = ++pairGeneration
    pair.disabled = true
    try {
      const context = await target()
      const session = await api.post<{ id: string; url: string; token: string }>(route + '/pairings', mcpTarget(context))
      if (generation !== pairGeneration) {
        void revoke(session.id)
        return
      }
      pairId = session.id
      element<HTMLInputElement>('pair-url').value = session.url
      element<HTMLInputElement>('pair-token').value = session.token
      credentials.hidden = false
      const path = route + '/pairings/' + encodeURIComponent(session.id)
      let lastId: string | undefined
      const poll = async () => {
        if (pairId !== session.id) return
        try {
          const incoming = await api.get<{ proposal_id?: string; proposal?: ResearchProposal }>(path, { cache: 'no-store' })
          if (pairId !== session.id) return
          if (incoming.proposal && incoming.proposal_id && incoming.proposal_id !== lastId) {
            lastId = incoming.proposal_id
            conversation.hidden = false
            render(incoming.proposal, context)
            notice.textContent = t('filaBrain.pairReceived')
          }
          pairPoll = window.setTimeout(poll, 2000)
        } catch {
          if (pairId !== session.id) return
          void disconnect()
          notice.textContent = t('filaBrain.pairExpired')
        }
      }
      pairPoll = window.setTimeout(poll, 2000)
    } catch (error) {
      if (generation !== pairGeneration) return
      conversation.hidden = false
      notice.textContent = errorText(error)
      void disconnect()
    }
  })
}
