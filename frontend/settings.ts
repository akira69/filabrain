import { initI18n, t } from './shared/i18n'
import { researchHeaders } from './shared/filament-research'

export async function mountSettings() {
  await initI18n()
  document.querySelector<HTMLInputElement>('#research-mcp-url')!.value =
    new URL('/api/v1/filament-research/mcp', location.origin).href
  const route = '/api/v1/filament-research/settings'
  const form = document.querySelector<HTMLFormElement>('#filament-research-settings')!
  const message = document.getElementById('research-settings-message')!
  const keyState = document.getElementById('research-key-state')!
  const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement
  const save = form.querySelector<HTMLButtonElement>('button[type=submit]')!
  const load = form.querySelector<HTMLButtonElement>('#research-load-models')!
  const test = form.querySelector<HTMLButtonElement>('#research-test-model')!
  const modelList = form.elements.namedItem('available_model') as HTMLSelectElement
  const mcpSwitch = document.querySelector<HTMLInputElement>('#research-mcp-enabled')!
  const mcpState = document.getElementById('research-mcp-state')!
  const mcpOff = document.getElementById('research-mcp-off')!
  const mcpSetup = document.getElementById('research-mcp-setup')!
  const mcpMessage = document.getElementById('research-mcp-message')!
  let mcpEnabled: boolean
  let saved: string | undefined
  let busy = false
  let models: string[] = []
  const values = () => ({
    model_url: field('model_url').value.trim(), model_id: field('model_id').value.trim(),
    search_url: field('search_url').value.trim(), api_key: field('api_key').value || null,
    clear_key: field('clear_key').checked,
  })
  const connection = () => JSON.stringify([values().model_url, values().api_key, values().clear_key])

  function showMcp(enabled: boolean) {
    mcpEnabled = enabled
    mcpSwitch.checked = enabled
    mcpState.textContent = t(enabled ? 'common.on' : 'common.off')
    mcpOff.hidden = enabled
    mcpSetup.hidden = !enabled
    document.dispatchEvent(new CustomEvent('filaman:mcp-state', { detail: enabled }))
  }

  mcpSwitch.addEventListener('change', async () => {
    const hadFocus = document.activeElement === mcpSwitch
    mcpSwitch.disabled = true
    mcpMessage.textContent = ''
    try {
      const response = await fetch(`${route}/mcp`, {
        method: 'PUT', credentials: 'include', headers: researchHeaders(),
        body: JSON.stringify({ mcp_enabled: mcpSwitch.checked }),
      })
      if (!response.ok) throw new Error()
      const data = await response.json()
      showMcp(data.mcp_enabled)
    } catch {
      mcpSwitch.checked = mcpEnabled
      mcpMessage.textContent = t('filamentResearch.mcpSaveFailed')
    } finally {
      mcpSwitch.disabled = false
      if (hadFocus && document.activeElement === document.body) mcpSwitch.focus({ preventScroll: true })
    }
  })

  function refresh() {
    const ready = saved !== undefined
    const config = values()
    const dirty = ready && JSON.stringify(config) !== saved
    save.disabled = busy || !dirty
    save.classList.toggle('fm-btn-primary', dirty)
    save.classList.toggle('fm-btn-outline', !dirty)
    load.disabled = busy || !ready || !config.model_url
    test.disabled = load.disabled || !config.model_id
    modelList.disabled = busy || !ready
  }

  function showModels() {
    const selected = values().model_id
    const options = [...new Set([...models, ...(selected ? [selected] : [])])]
    modelList.replaceChildren(new Option(t('filamentResearch.chooseModel'), ''),
      ...options.map(model => new Option(model, model)))
    modelList.value = selected
  }

  function fill(config: { model_url: string; model_id: string; search_url: string; has_key: boolean }) {
    for (const key of ['model_url', 'model_id', 'search_url'] as const) field(key).value = config[key]
    field('api_key').value = ''
    field('clear_key').checked = false
    keyState.textContent = t(config.has_key ? 'filamentResearch.keyConfigured' : 'filamentResearch.keyOptional')
    saved = JSON.stringify(values())
    showModels()
  }

  async function send(action: 'save' | 'test' | 'models') {
    if (busy || (action === 'models' ? !field('model_url').reportValidity() : !form.reportValidity())) return
    if (action === 'save' && save.disabled) return
    const requestedConnection = connection()
    const payload = action === 'models' ? { ...values(), search_url: '' } : values()
    const inputs = action === 'save' ? Array.from(form.querySelectorAll<HTMLInputElement>('input:not(:disabled)')) : []
    inputs.forEach(input => { input.disabled = true })
    busy = true
    refresh()
    message.textContent = ''
    try {
      const response = await fetch(action === 'save' ? route : `${route}/${action}`, {
        method: action === 'save' ? 'PUT' : 'POST', credentials: 'include', headers: researchHeaders(),
        body: JSON.stringify(payload),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : t('filamentResearch.failed'))
      if (action === 'models') {
        if (requestedConnection !== connection()) return
        models = data.models
        showModels()
        message.textContent = t(data.models.length ? 'filamentResearch.modelsLoaded' : 'filamentResearch.noModels')
        return
      }
      if (action === 'save') fill(data)
      message.textContent = t(action === 'test' ? 'filamentResearch.testPassed' : 'filamentResearch.settingsSaved')
    } catch (error) {
      message.textContent = error instanceof Error ? error.message : t('filamentResearch.failed')
    } finally {
      inputs.forEach(input => { input.disabled = false })
      busy = false
      refresh()
    }
  }

  try {
    const response = await fetch(route, { credentials: 'include' })
    if (!response.ok) throw new Error()
    const data = await response.json()
    fill(data)
    showMcp(data.mcp_enabled)
    mcpSwitch.disabled = false
    if (location.hash === '#mcp-settings') {
      document.getElementById('mcp-settings')?.closest('section')?.scrollIntoView({ block: 'end' })
    }
    refresh()
    form.addEventListener('submit', event => { event.preventDefault(); void send('save') })
    test.addEventListener('click', () => void send('test'))
    load.addEventListener('click', () => void send('models'))
    form.addEventListener('input', event => {
      if (event.target === modelList) return
      if (['model_url', 'api_key', 'clear_key'].includes((event.target as HTMLInputElement).name)) {
        models = []
        showModels()
      } else if (event.target === field('model_id')) showModels()
      refresh()
    })
    modelList.addEventListener('change', () => {
      if (modelList.value) field('model_id').value = modelList.value
      showModels()
      refresh()
    })
    if (values().model_url) void send('models')
  } catch {
    message.textContent = t('filamentResearch.failed')
    mcpMessage.textContent = t('filamentResearch.failed')
  }
}
