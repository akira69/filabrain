import { initI18n, t } from './shared/i18n'
import { researchHeaders } from './shared/filabrain'
import { mountModelSettings, type ModelEndpoint } from './model-settings'

const supplierDomain = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$/
const maxSupplierSites = 4
type SupplierSite = { domain: string; enabled: boolean }

export async function mountSettings() {
  await initI18n()
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
  let savedSources: string | undefined
  let savedModels: string | undefined
  let modelRevision: string | undefined
  let saving = false
  let testingSearch = false
  let sites: SupplierSite[] = []
  const models = mountModelSettings(form, refresh)
  const sourceValues = () => ({
    chat_live_sources: field('chat_live_sources').checked,
    search_url: field('search_url').value.trim(),
    supplier_sites: sites,
    supplier_download_mib: field('supplier_download_mib').valueAsNumber,
  })

  function refresh() {
    const ready = savedSources !== undefined
    const sources = sourceValues()
    const searchReady = !!sources.search_url && field('search_url').validity.valid
    field('chat_live_sources').disabled = saving || !ready || !searchReady
    document.getElementById('research-auto-search-setup')!.hidden = searchReady
    const dirtyModels = ready && JSON.stringify(models.values()) !== savedModels
    const dirty = dirtyModels || (ready && JSON.stringify(sources) !== savedSources)
    save.disabled = saving || !dirty || models.pending
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
    model_revision?: string
    supplier_sites?: SupplierSite[]; supplier_download_mib: number
    chat_live_sources?: boolean
  }) {
    field('search_url').value = config.search_url
    field('chat_live_sources').checked = config.chat_live_sources ?? true
    field('supplier_download_mib').valueAsNumber = config.supplier_download_mib
    enableSearch.hidden = !!config.search_url
    searchSetup.hidden = !config.search_url
    models.fill(config.model_endpoints, config.default_endpoint_id)
    modelRevision = config.model_revision
    sites = config.supplier_sites ?? []
    renderSupplierSites()
    supplierMessage.textContent = sites.length >= maxSupplierSites ? t('filaBrain.supplierLimit') : ''
    savedSources = JSON.stringify(sourceValues())
    savedModels = JSON.stringify(models.values())
  }

  async function send(action: 'save' | 'test-search') {
    const isSave = action === 'save'
    if (saving || (isSave ? save.disabled : testingSearch)) return
    const sources = sourceValues()
    const modelSettings = models.values()
    const dirtyModels = JSON.stringify(modelSettings) !== savedModels
    if (isSave ? (dirtyModels && !models.validate()) || !form.reportValidity() : !field('search_url').reportValidity()) return
    const changes = dirtyModels ? { ...sources, ...modelSettings, model_revision: modelRevision } : sources
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
        document.dispatchEvent(new CustomEvent('filaman:model-change', {
          detail: { chat_model: data.model_id, chat_endpoint_id: data.default_endpoint_id },
        }))
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
    refresh()
    form.addEventListener('submit', event => { event.preventDefault(); void send('save') })
    testSearch.addEventListener('click', () => void send('test-search'))
    form.addEventListener('input', event => {
      if (event.target === field('search_url')) searchMessage.textContent = ''
      if (event.target === field('search_url') || event.target === field('supplier_download_mib') || event.target === field('chat_live_sources')) refresh()
    })
  } catch {
    message.textContent = t('filaBrain.failed')
  }
}
