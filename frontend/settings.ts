import { initI18n, t } from './shared/i18n'
import { researchHeaders } from './shared/filabrain'

const supplierDomain = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$/
const maxSupplierSites = 4
type SupplierSite = { domain: string; enabled: boolean }

export async function mountSettings() {
  await initI18n()
  document.querySelector<HTMLInputElement>('#research-mcp-url')!.value =
    new URL('/api/v1/filabrain/mcp', location.origin).href
  const route = '/api/v1/filabrain/settings'
  const form = document.querySelector<HTMLFormElement>('#filabrain-settings')!
  const message = document.getElementById('research-settings-message')!
  const keyState = document.getElementById('research-key-state')!
  const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement
  const save = form.querySelector<HTMLButtonElement>('button[type=submit]')!
  const enableSearch = document.getElementById('research-enable-search')!
  const searchSetup = document.getElementById('research-search-setup')!
  const sourceControls = form.querySelector<HTMLFieldSetElement>('#research-source-controls')!
  const searchSource = document.getElementById('research-search-source')!
  const supplierSites = document.getElementById('research-supplier-sites')!
  const domainInput = form.querySelector<HTMLInputElement>('#research-supplier-domain')!
  const addSupplier = form.querySelector<HTMLButtonElement>('#research-add-supplier')!
  const supplierMessage = document.getElementById('research-supplier-message')!
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
  let sites: SupplierSite[] = []
  const values = () => ({
    model_url: field('model_url').value.trim(), model_id: field('model_id').value.trim(),
    search_url: field('search_url').value.trim(), api_key: field('api_key').value || null,
    clear_key: field('clear_key').checked,
    supplier_sites: sites,
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
      mcpMessage.textContent = t('filaBrain.mcpSaveFailed')
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
    sourceControls.disabled = busy || !ready
    domainInput.disabled = addSupplier.disabled = sites.length >= maxSupplierSites
  }

  function renderSupplierSites() {
    supplierSites.hidden = sites.length === 0
    supplierSites.replaceChildren(...sites.map(site => {
      const row = document.createElement('li')
      row.className = 'research-supplier-row'
      const label = document.createElement('label')
      const checkbox = document.createElement('input')
      checkbox.type = 'checkbox'
      checkbox.checked = site.enabled
      checkbox.addEventListener('change', () => {
        site.enabled = checkbox.checked
        refresh()
      })
      const domain = document.createElement('span')
      domain.textContent = site.domain
      label.append(checkbox, domain)
      const remove = document.createElement('button')
      remove.type = 'button'
      remove.className = 'fm-btn fm-btn-outline'
      remove.textContent = t('filaBrain.removeSupplier')
      remove.setAttribute('aria-label', t('filaBrain.removeSupplierDomain', { domain: site.domain }))
      remove.addEventListener('click', () => {
        sites = sites.filter(entry => entry !== site)
        supplierMessage.textContent = ''
        renderSupplierSites()
        refresh()
        domainInput.focus()
      })
      row.append(label, remove)
      return row
    }))
  }

  function addSupplierSite() {
    if (sourceControls.disabled || addSupplier.disabled) return
    const domain = domainInput.value.trim().toLowerCase().replace(/\.$/, '').replace(/^www\./, '')
    if (!supplierDomain.test(domain)) {
      supplierMessage.textContent = t('filaBrain.invalidSupplierDomain')
      domainInput.focus()
      return
    }
    if (sites.some(site => site.domain === domain)) {
      supplierMessage.textContent = t('filaBrain.duplicateSupplierDomain')
      domainInput.focus()
      return
    }
    sites.push({ domain, enabled: true })
    domainInput.value = ''
    supplierMessage.textContent = sites.length === maxSupplierSites ? t('filaBrain.supplierLimit') : ''
    renderSupplierSites()
    refresh()
    if (domainInput.disabled) supplierSites.lastElementChild?.querySelector('input')?.focus()
    else domainInput.focus()
  }
  addSupplier.addEventListener('click', addSupplierSite)
  domainInput.addEventListener('input', () => { supplierMessage.textContent = '' })
  domainInput.addEventListener('keydown', event => {
    if (event.key !== 'Enter') return
    event.preventDefault()
    addSupplierSite()
  })

  enableSearch.addEventListener('click', () => {
    enableSearch.hidden = true
    searchSetup.hidden = false
    field('search_url').focus()
  })

  function showModels() {
    const selected = values().model_id
    const options = [...new Set([...models, ...(selected ? [selected] : [])])]
    modelList.replaceChildren(new Option(t('filaBrain.chooseModel'), ''),
      ...options.map(model => new Option(model, model)))
    modelList.value = selected
  }

  function fill(config: {
    model_url: string; model_id: string; search_url: string; has_key: boolean
    supplier_sites?: SupplierSite[]
  }) {
    for (const key of ['model_url', 'model_id', 'search_url'] as const) field(key).value = config[key]
    enableSearch.hidden = !!config.search_url
    searchSetup.hidden = !config.search_url
    field('api_key').value = ''
    field('clear_key').checked = false
    keyState.textContent = t(config.has_key ? 'filaBrain.keyConfigured' : 'filaBrain.keyOptional')
    searchSource.textContent = t(config.search_url ? 'filaBrain.sourceConfigured' : 'filaBrain.sourceNotConfigured')
    sites = config.supplier_sites ?? []
    renderSupplierSites()
    supplierMessage.textContent = sites.length >= maxSupplierSites ? t('filaBrain.supplierLimit') : ''
    saved = JSON.stringify(values())
    showModels()
  }

  async function send(action: 'save' | 'test' | 'models') {
    if (busy || (action === 'models' ? !field('model_url').reportValidity() : !form.reportValidity())) return
    if (action === 'save' && save.disabled) return
    const requestedConnection = connection()
    const payload = action === 'models' ? { ...values(), search_url: '' } : values()
    const inputs = action === 'save' ? Array.from(form.querySelectorAll<HTMLInputElement>('input:not(:disabled)'))
      .filter(input => !sourceControls.contains(input)) : []
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
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : t('filaBrain.failed'))
      if (action === 'models') {
        if (requestedConnection !== connection()) return
        models = data.models
        showModels()
        message.textContent = t(data.models.length ? 'filaBrain.modelsLoaded' : 'filaBrain.noModels')
        return
      }
      if (action === 'save') {
        fill(data)
        document.dispatchEvent(new CustomEvent('filaman:model-change', { detail: data.model_id }))
      }
      message.textContent = t(action === 'test' ? 'filaBrain.testPassed' : 'filaBrain.settingsSaved')
    } catch (error) {
      message.textContent = error instanceof Error ? error.message : t('filaBrain.failed')
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
    message.textContent = t('filaBrain.failed')
    mcpMessage.textContent = t('filaBrain.failed')
  }
}
