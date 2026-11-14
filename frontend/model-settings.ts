import { api } from './shared/api'
import { t } from './shared/i18n'

export type ModelEndpoint = {
  id: string
  name: string
  model_url: string
  model_id: string
  has_key: boolean
  models: string[] | null
  hidden_models: string[]
}
type Draft = ModelEndpoint & { api_key: string; clear_key: boolean }

export function mountModelSettings(form: HTMLFormElement, changed: () => void) {
  const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement
  const select = form.querySelector<HTMLSelectElement>('#research-model-endpoint')!
  const add = form.querySelector<HTMLButtonElement>('#research-add-endpoint')!
  const fields = form.querySelector<HTMLFieldSetElement>('#research-endpoint-fields')!
  const load = form.querySelector<HTMLButtonElement>('#research-load-models')!
  const test = form.querySelector<HTMLButtonElement>('#research-test-model')!
  const makeDefault = form.querySelector<HTMLButtonElement>('#research-default-endpoint')!
  const remove = form.querySelector<HTMLButtonElement>('#research-remove-endpoint')!
  const modelList = form.querySelector<HTMLSelectElement>('#research-available-model')!
  const visibility = form.querySelector<HTMLDetailsElement>('#research-model-visibility')!
  const visibleModels = form.querySelector<HTMLElement>('#research-visible-models')!
  const message = form.querySelector<HTMLElement>('#research-model-message')!
  const keyState = form.querySelector<HTMLElement>('#research-key-state')!
  let entries: Draft[] = []
  let active: Draft | undefined
  let defaultId: string | null = null
  let ready = false
  let saving = false
  const testing = new Set<Draft>()
  const feedback = new WeakMap<Draft, string>()

  function newDraft(): Draft {
    return {
      id: Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join(''),
      name: t('filaBrain.defaultEndpoint'), model_url: '', model_id: '', has_key: false,
      models: [], hidden_models: [], api_key: '', clear_key: false,
    }
  }

  function choices(entry: Draft) {
    return [...new Set([...(entry.model_id ? [entry.model_id] : []), ...(entry.models ?? [])])]
  }

  function showEndpoints() {
    select.replaceChildren(...entries.map(entry => new Option(
      entry.name || t('filaBrain.endpointName'), entry.id,
    )))
    select.value = active?.id ?? ''
  }

  function showModels() {
    if (!active) return
    const entry = active
    const models = choices(entry)
    modelList.replaceChildren(new Option(t('filaBrain.chooseModel'), ''),
      ...models.map(model => new Option(model, model)))
    modelList.value = entry.model_id
    visibility.hidden = !models.length
    visibleModels.replaceChildren(...models.map(model => {
      const label = document.createElement('label')
      const checkbox = document.createElement('input')
      checkbox.type = 'checkbox'
      const isDefault = entry.id === defaultId && model === entry.model_id
      checkbox.checked = isDefault || !entry.hidden_models.includes(model)
      checkbox.disabled = isDefault
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) entry.hidden_models = entry.hidden_models.filter(id => id !== model)
        else entry.hidden_models.push(model)
        changed()
      })
      const text = document.createElement('span')
      text.textContent = isDefault ? `${model} · ${t('filaBrain.modelDefault')}` : model
      label.append(checkbox, text)
      return label
    }))
  }

  function refresh(isSaving = saving) {
    saving = isSaving
    const blocked = !ready || saving
    fields.disabled = select.disabled = blocked
    add.setAttribute('aria-disabled', String(blocked))
    const pending = !!active && testing.has(active)
    load.disabled = blocked || pending || !active?.model_url
    test.disabled = load.disabled || !active?.model_id
    makeDefault.disabled = blocked || !active?.model_id || active.id === defaultId
    makeDefault.textContent = t(active?.id === defaultId ? 'filaBrain.defaultEndpoint' : 'filaBrain.makeDefault')
    remove.disabled = blocked || !active
    modelList.disabled = blocked || pending
  }

  function show(entry: Draft, resetDetails = false) {
    active = entry
    for (const key of ['model_url', 'model_id', 'api_key'] as const) field(key).value = entry[key]
    field('endpoint_name').value = entry.name
    field('clear_key').checked = entry.clear_key
    field('clear_key').closest('label')!.hidden = !entry.has_key
    keyState.textContent = t(entry.has_key ? 'filaBrain.keyConfigured' : 'filaBrain.keyOptional')
    message.textContent = feedback.get(entry) ?? ''
    if (resetDetails) {
      visibility.open = false
      form.querySelector<HTMLDetailsElement>('.research-manual-model')!.open = false
    }
    showEndpoints()
    showModels()
    refresh()
  }

  const connection = (entry: Draft) => ({
    endpoint_id: entry.id, model_url: entry.model_url, model_id: entry.model_id,
    api_key: entry.api_key || null, clear_key: entry.clear_key,
  })

  function report(entry: Draft, text: string) {
    feedback.set(entry, text)
    if (active === entry) message.textContent = text
  }

  async function send(action: 'models' | 'test') {
    const entry = active
    if (!entry || testing.has(entry) || !field('model_url').reportValidity()) return
    const payload = connection(entry)
    testing.add(entry)
    report(entry, '')
    changed()
    const stillCurrent = () => {
      const current = connection(entry)
      return entries.includes(entry) && current.model_url === payload.model_url && current.api_key === payload.api_key
        && current.clear_key === payload.clear_key && (action === 'models' || current.model_id === payload.model_id)
    }
    try {
      if (action === 'models') {
        const { models } = await api.post<{ models: string[] }>('/filabrain/settings/models', payload)
        if (!stillCurrent()) return
        entry.models = models
        if (active === entry) showModels()
        report(entry, t(models.length ? 'filaBrain.modelsLoaded' : 'filaBrain.noModels'))
        changed()
      } else {
        await api.post('/filabrain/settings/test', payload)
        if (stillCurrent()) report(entry, t('filaBrain.testPassed'))
      }
    } catch (error) {
      if (stillCurrent()) report(entry, error instanceof Error ? error.message : t('filaBrain.failed'))
    } finally {
      testing.delete(entry)
      changed()
    }
  }

  fields.addEventListener('input', event => {
    if (!active) return
    const element = event.target as HTMLInputElement
    if (element.name === 'endpoint_name') {
      active.name = element.value.trim()
      showEndpoints()
    } else if (element.name === 'model_id') {
      active.model_id = element.value.trim()
      if (active.id === defaultId) active.hidden_models = active.hidden_models.filter(id => id !== active!.model_id)
      showModels()
    } else if (element.name === 'model_url') {
      if (element.value.trim() !== active.model_url) {
        active.model_url = element.value.trim()
        active.models = []
        showModels()
      }
    } else if (element.name === 'api_key') active.api_key = element.value
    else if (element.name === 'clear_key') active.clear_key = element.checked
    else return
    report(active, '')
    changed()
  })
  modelList.addEventListener('change', () => {
    if (!modelList.value) return
    const input = field('model_id')
    input.value = modelList.value
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  select.addEventListener('change', () => show(entries.find(entry => entry.id === select.value)!, true))
  add.addEventListener('click', () => {
    if (!ready || saving) return
    if (entries.length === 1 && !entries[0].model_url && !entries[0].model_id && !entries[0].api_key) {
      entries = []
      defaultId = null
    }
    const entry = newDraft()
    entry.name = ''
    entries.push(entry)
    defaultId ??= entry.id
    show(entry, true)
    field('endpoint_name').focus()
    changed()
  })
  makeDefault.addEventListener('click', () => {
    if (!active) return
    defaultId = active.id
    active.hidden_models = active.hidden_models.filter(id => id !== active!.model_id)
    showModels()
    changed()
  })
  remove.addEventListener('click', () => {
    entries = entries.filter(entry => entry !== active)
    if (!entries.length) entries.push(newDraft())
    if (!entries.some(entry => entry.id === defaultId)) {
      defaultId = entries[0].id
      entries[0].hidden_models = entries[0].hidden_models.filter(id => id !== entries[0].model_id)
    }
    show(entries[0], true)
    select.focus()
    changed()
  })
  load.addEventListener('click', () => void send('models'))
  test.addEventListener('click', () => void send('test'))

  return {
    refresh,
    get pending() { return testing.size > 0 },
    fill(endpoints: ModelEndpoint[], defaultEndpoint: string | null) {
      const activeId = active?.id
      entries = endpoints.map(entry => ({ ...entry, api_key: '', clear_key: false }))
      if (!entries.length) entries.push(newDraft())
      defaultId = defaultEndpoint ?? entries[0].id
      ready = true
      show(entries.find(entry => entry.id === activeId) ?? entries.find(entry => entry.id === defaultId) ?? entries[0])
    },
    validate() {
      const incomplete = entries.find(entry => (entry.model_url || entry.model_id || entry.api_key || entries.length > 1)
        && (!entry.name || !entry.model_id || /\s/.test(entry.model_id) || !/^https?:\/\/[^\s]+$/i.test(entry.model_url)))
      if (!incomplete) return true
      show(incomplete)
      report(incomplete, t('filaBrain.endpointIncomplete'))
      message.tabIndex = -1
      message.focus()
      return false
    },
    values() {
      const configured = entries.filter(entry => entry.model_url || entry.model_id || entry.api_key)
      return {
        default_endpoint_id: configured.length ? defaultId : null,
        model_endpoints: configured.map(({ has_key, ...entry }) => ({ ...entry, api_key: entry.api_key || null })),
      }
    },
  }
}
