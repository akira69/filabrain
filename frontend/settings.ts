import { initI18n, t } from './shared/i18n'
import { researchHeaders } from './shared/filabrain'
import { mountModelSettings, type ModelEndpoint } from './model-settings'

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
  const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement
  const save = form.querySelector<HTMLButtonElement>('button[type=submit]')!
  const enableSearch = document.getElementById('research-enable-search')!
  const searchSetup = document.getElementById('research-search-setup')!
  const sourceControls = form.querySelector<HTMLFieldSetElement>('#research-source-controls')!
  const supplierSites = document.getElementById('research-supplier-sites')!
  const domainInput = form.querySelector<HTMLInputElement>('#research-supplier-domain')!
  const addSupplier = form.querySelector<HTMLButtonElement>('#research-add-supplier')!
  const supplierMessage = document.getElementById('research-supplier-message')!
  const testSearch = form.querySelector<HTMLButtonElement>('#research-test-search')!
  const searchMessage = document.getElementById('research-search-message')!
  const mcpSwitch = document.querySelector<HTMLInputElement>('#research-mcp-enabled')!
  const mcpState = document.getElementById('research-mcp-state')!
  const mcpOff = document.getElementById('research-mcp-off')!
  const mcpSetup = document.getElementById('research-mcp-setup')!
  const mcpMessage = document.getElementById('research-mcp-message')!
  let mcpEnabled: boolean
  let savedSources: string | undefined
  let savedModels: string | undefined
  let saving = false
  let testingSearch = false
  let sites: SupplierSite[] = []
  const models = mountModelSettings(form, refresh)
  const sourceValues = () => ({
    search_url: field('search_url').value.trim(),
    supplier_sites: sites,
    supplier_download_mib: field('supplier_download_mib').valueAsNumber,
  })

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
    const ready = savedSources !== undefined
    const sources = sourceValues()
    const dirtyModels = ready && JSON.stringify(models.values()) !== savedModels
    const dirty = dirtyModels || (ready && JSON.stringify(sources) !== savedSources)
    save.disabled = saving || !dirty || (dirtyModels && models.pending)
    save.classList.toggle('fm-btn-primary', dirty)
    save.classList.toggle('fm-btn-outline', !dirty)
    models.refresh(saving)
    testSearch.disabled = saving || testingSearch || !ready || !sources.search_url || !field('search_url').validity.valid
    sourceControls.disabled = saving || !ready
    sourceControls.hidden = !sources.search_url || !field('search_url').validity.valid
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

  function fill(config: {
    search_url: string
    model_endpoints: ModelEndpoint[]; default_endpoint_id: string | null
    supplier_sites?: SupplierSite[]; supplier_download_mib: number
  }) {
    field('search_url').value = config.search_url
    field('supplier_download_mib').valueAsNumber = config.supplier_download_mib
    enableSearch.hidden = !!config.search_url
    searchSetup.hidden = !config.search_url
    models.fill(config.model_endpoints, config.default_endpoint_id)
    sites = config.supplier_sites ?? []
    renderSupplierSites()
    supplierMessage.textContent = sites.length >= maxSupplierSites ? t('filaBrain.supplierLimit') : ''
    savedSources = JSON.stringify(sourceValues())
    savedModels = JSON.stringify(models.values())
  }

  async function send(action: 'save' | 'test-search') {
    const isSave = action === 'save'
    if (saving || (isSave ? save.disabled : testingSearch)) return
    if (isSave ? !models.validate() || !form.reportValidity() : !field('search_url').reportValidity()) return
    const sources = sourceValues()
    const modelSettings = models.values()
    const changes = JSON.stringify(modelSettings) === savedModels ? sources : { ...sources, ...modelSettings }
    const stillCurrent = () => isSave || sources.search_url === sourceValues().search_url
    const inputs = isSave ? Array.from(form.querySelectorAll<HTMLInputElement>('input:not(:disabled)'))
      .filter(input => !sourceControls.contains(input) && !input.closest('#research-endpoint-fields')) : []
    inputs.forEach(input => { input.disabled = true })
    if (isSave) saving = true
    else testingSearch = true
    refresh()
    const feedback = isSave ? message : searchMessage
    feedback.textContent = ''
    try {
      const response = await fetch(isSave ? route : `${route}/test-search`, {
        method: isSave ? 'PUT' : 'POST', credentials: 'include', headers: researchHeaders(),
        body: JSON.stringify(isSave ? changes : { search_url: sources.search_url }),
      })
      const data = await response.json()
      if (!stillCurrent()) return
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : t('filaBrain.failed'))
      if (isSave) {
        fill(data)
        document.dispatchEvent(new CustomEvent('filaman:model-change', { detail: data.model_id }))
      }
      feedback.textContent = t(isSave ? 'filaBrain.settingsSaved' : 'filaBrain.searchTestPassed')
    } catch (error) {
      if (stillCurrent()) feedback.textContent = error instanceof Error ? error.message : t('filaBrain.failed')
    } finally {
      inputs.forEach(input => { input.disabled = false })
      if (isSave) saving = false
      else testingSearch = false
      refresh()
    }
  }

  try {
    const response = await fetch(route, { credentials: 'include' })
    if (!response.ok) throw new Error()
    const data = await response.json()
    const focusUrl = document.activeElement === form.querySelector('#research-add-endpoint')
    fill(data)
    if (focusUrl) field('model_url').focus()
    showMcp(data.mcp_enabled)
    mcpSwitch.disabled = false
    if (location.hash === '#mcp-settings') {
      document.getElementById('mcp-settings')?.closest('section')?.scrollIntoView({ block: 'end' })
    }
    refresh()
    form.addEventListener('submit', event => { event.preventDefault(); void send('save') })
    testSearch.addEventListener('click', () => void send('test-search'))
    form.addEventListener('input', event => {
      if (event.target === field('search_url')) searchMessage.textContent = ''
      if (event.target === field('search_url') || event.target === field('supplier_download_mib')) refresh()
    })
  } catch {
    message.textContent = t('filaBrain.failed')
    mcpMessage.textContent = t('filaBrain.failed')
  }
}
