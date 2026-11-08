import { mountChat } from './chat'
import { mountSettings } from './settings'
import { mountResearch, type EditorContext, type SelectionContext } from './research'
import { api } from './shared/api'
import { batchHref, createResearchBatch, mountResearchBatches, type BatchMode } from './shared/filament-research-batches'
import { initI18n } from './shared/i18n'

export type Status = {
  enabled: boolean
  can_chat: boolean
  can_create: boolean
  can_update: boolean
  can_configure: boolean
  mcp_enabled: boolean
}

export function element<T extends HTMLElement = HTMLElement>(id: string) {
  return document.getElementById(id) as T
}

export function hostRequest<T>(type: string, payload: Record<string, unknown> = {}) {
  return new Promise<T>((resolve, reject) => {
    const request_id = Array.from(crypto.getRandomValues(new Uint32Array(4))).join('-')
    const timer = window.setTimeout(() => {
      window.removeEventListener('message', receive)
      reject(new Error('The FilaMan editor did not respond. Reopen Filament Research and try again.'))
    }, 3500)
    function receive(event: MessageEvent) {
      if (event.origin !== location.origin || event.source !== window.parent ||
          event.data?.type !== type || event.data?.request_id !== request_id) return
      window.clearTimeout(timer)
      window.removeEventListener('message', receive)
      resolve(event.data)
    }
    window.addEventListener('message', receive)
    window.parent.postMessage({ type, request_id, ...payload }, location.origin)
  })
}

export const errorText = (error: unknown) => error instanceof Error ? error.message : 'The request failed.'

function mountLibrary(context?: SelectionContext) {
  const selected = new Set<number>(context?.filament_ids)
  const form = element<HTMLFormElement>('library-research')
  const search = element<HTMLFormElement>('library-search')
  const rows = element('library-records')
  const pages = element('library-pages')
  const prev = element<HTMLButtonElement>('library-prev')
  const next = element<HTMLButtonElement>('library-next')
  const submit = form.querySelector<HTMLButtonElement>('button[type=submit]')!
  const message = element('library-status')
  let currentPage = 1
  let request = 0
  let busy = false
  let loadFailed = false

  function updateSelection() {
    element('library-selected').textContent = `${selected.size} of 50 selected`
    submit.disabled = busy || !selected.size || selected.size > 50 || loadFailed
  }

  async function load() {
    const generation = ++request
    const query = new URLSearchParams({ page: String(currentPage), page_size: '25' })
    const name = String(new FormData(search).get('search')).trim()
    if (name) query.set('search', name)
    rows.textContent = 'Loading filaments…'
    pages.hidden = true
    try {
      const { items, total } = await api.get<{ items: { id: number; designation: string; material_type: string; manufacturer?: { name: string } }[]; total: number }>(`/filaments?${query}`, { cache: 'no-store' })
      if (generation !== request) return
      loadFailed = false
      message.textContent = ''
      rows.replaceChildren()
      for (const item of items) {
        const label = document.createElement('label')
        const checkbox = document.createElement('input')
        checkbox.type = 'checkbox'
        checkbox.checked = selected.has(item.id)
        checkbox.addEventListener('change', () => {
          if (checkbox.checked && selected.size === 50) {
            checkbox.checked = false
            return
          }
          if (checkbox.checked) selected.add(item.id)
          else selected.delete(item.id)
          updateSelection()
        })
        label.append(checkbox, [item.designation, item.manufacturer?.name, item.material_type].filter(Boolean).join(' · '))
        rows.append(label)
      }
      if (!items.length) rows.textContent = 'No matching filaments.'
      prev.disabled = currentPage === 1
      next.disabled = currentPage * 25 >= total
      pages.hidden = prev.disabled && next.disabled
    } catch (error) {
      if (generation !== request) return
      loadFailed = true
      rows.replaceChildren()
      message.textContent = errorText(error)
    }
    updateSelection()
  }

  if (context) {
    search.hidden = true
    element('library-hint').textContent = 'Review proposals for the filaments selected in FilaMan before applying changes.'
  } else {
    search.addEventListener('submit', event => { event.preventDefault(); currentPage = 1; void load() })
    prev.addEventListener('click', () => { currentPage--; void load() })
    next.addEventListener('click', () => { currentPage++; void load() })
    void load()
  }

  form.addEventListener('submit', async event => {
    event.preventDefault()
    if (submit.disabled) return
    const mode = new FormData(form).get('mode') as BatchMode
    busy = true
    updateSelection()
    message.textContent = 'Starting research…'
    try {
      const batch = await createResearchBatch([...selected], mode)
      location.assign(batchHref(batch.id))
    } catch (error) {
      message.textContent = errorText(error)
      busy = false
      updateSelection()
    }
  })
  updateSelection()
}

async function main() {
  await initI18n()
  const theme = () => { document.documentElement.dataset.theme = localStorage.getItem('theme') || 'brand' }
  theme()
  window.addEventListener('storage', event => { if (event.key === 'theme') theme() })
  const params = new URLSearchParams(location.search)
  const id = params.get('id')
  const hostMode = window.parent === window ? null : params.get('filament_host')
  const message = element('plugin-status')
  try {
    const status = await api.get<Status>('/filament-research/status', { cache: 'no-store' })
    document.addEventListener('filaman:mcp-state', event => {
      status.mcp_enabled = (event as CustomEvent<boolean>).detail
    })
    if (!status.enabled) {
      message.textContent = 'Filament Research is disabled.'
      return
    }
    if (hostMode === 'editor' || hostMode === 'selection') {
      const context = await hostRequest<EditorContext | SelectionContext>('filaman:filament-context')
      if (hostMode === 'editor' && (context.mode === 'create' || context.mode === 'edit')) {
        if (context.mode === 'create' ? !status.can_create : !status.can_update) throw new Error('Filament access is required.')
        element('plugin-header').hidden = true
        document.querySelector<HTMLElement>('[data-view="new"]')!.hidden = false
        mountResearch(status, context)
      } else if (hostMode === 'selection' && context.mode === 'selection' && status.can_update) {
        element('plugin-header').hidden = true
        document.querySelector<HTMLElement>('[data-view="library"]')!.hidden = false
        mountLibrary(context)
      } else throw new Error('This FilaMan selection is unavailable.')
      message.textContent = ''
      return
    }
    const mount: Record<string, (() => void) | false> = {
      chat: status.can_chat && mountChat,
      new: status.can_create && (() => mountResearch(status)),
      library: status.can_update && (() => mountLibrary()),
      batches: status.can_update && (() => {
        const batch = element('filament-batches')
        batch.querySelector<HTMLElement>('[data-batch-history-link]')!.hidden = !id
        batch.querySelector<HTMLElement>('[data-batch-message]')!.removeAttribute('data-i18n')
        mountResearchBatches(batch, id)
      }),
      settings: status.can_configure && mountSettings,
    }
    const buttons = [...document.querySelectorAll<HTMLButtonElement>('[data-tab]')]
    const visited = new Set<string>()
    function show(tab: string) {
      const run = Object.hasOwn(mount, tab) && mount[tab]
      if (!run) return
      for (const button of buttons) {
        if (button.dataset.tab === tab) button.setAttribute('aria-current', 'page')
        else button.removeAttribute('aria-current')
      }
      for (const view of document.querySelectorAll<HTMLElement>('[data-view]')) view.hidden = view.dataset.view !== tab
      if (!visited.has(tab)) {
        visited.add(tab)
        run()
      }
    }
    for (const button of buttons) {
      const tab = button.dataset.tab!
      button.hidden = !mount[tab]
      button.addEventListener('click', () => show(tab))
    }
    const available = Object.keys(mount).find(tab => mount[tab])
    if (!available) {
      message.textContent = 'You do not have permission to use Filament Research.'
      return
    }
    message.textContent = ''
    const tab = params.get('tab') || available
    show(Object.hasOwn(mount, tab) && mount[tab] ? tab : available)
  } catch (error) {
    message.textContent = errorText(error)
  }
}

void main()
