import { mountChat } from './chat'
import { mountDocs } from './docs'
import { mountSettings } from './settings'
import { mountResearch, type EditorContext, type SelectionContext } from './research'
import { ApiError, api } from './shared/api'
import { batchHref, createResearchBatch, mountResearchBatches, type BatchMode, type ExpectedFilament } from './shared/filabrain-batches'
import { initI18n, t } from './shared/i18n'

export type Status = {
  enabled: boolean
  can_chat: boolean
  chat_model?: string | null
  can_create: boolean
  can_update: boolean
  can_update_manufacturer?: boolean
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
      reject(new Error('The FilaMan editor did not respond. Reopen FilaBrain and try again.'))
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
  const expectedFilaments = new Map<number, ExpectedFilament>()
  const verifiesSelection = !context || context.expected_filaments !== undefined
  for (const item of context?.expected_filaments ?? []) expectedFilaments.set(item.id, item)
  const form = element<HTMLFormElement>('library-research')
  const search = element<HTMLFormElement>('library-search')
  const rows = element('library-records')
  const pages = element('library-pages')
  const prev = element<HTMLButtonElement>('library-prev')
  const next = element<HTMLButtonElement>('library-next')
  const submit = form.querySelector<HTMLButtonElement>('button[type=submit]')!
  const message = element('library-status')
  const picker = element<HTMLIFrameElement>('library-picker')
  const clearSelection = element<HTMLButtonElement>('library-clear-selection')
  let currentPage = 1
  let request = 0
  let busy = false
  let loadFailed = false

  function updateSelection() {
    element('library-selected').textContent = `${selected.size} of 50 selected`
    submit.disabled = busy || !selected.size || selected.size > 50 || loadFailed ||
      (verifiesSelection && (expectedFilaments.size !== selected.size || [...selected].some(id => !expectedFilaments.has(id))))
    clearSelection.hidden = !!context || !selected.size
    clearSelection.disabled = busy
  }

  async function load(notice = '') {
    const generation = ++request
    const query = new URLSearchParams({ page: String(currentPage), page_size: '25', include_record_token: 'true' })
    const name = String(new FormData(search).get('search')).trim()
    if (name) query.set('search', name)
    rows.textContent = 'Loading filaments…'
    pages.hidden = true
    try {
      const { items, total } = await api.get<{ items: (ExpectedFilament & { manufacturer?: { name: string } })[]; total: number }>(`/filaments?${query}`, { cache: 'no-store' })
      if (generation !== request) return
      loadFailed = false
      message.textContent = notice
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
          if (checkbox.checked) {
            const { id, manufacturer_id, designation, material_type, record_token } = item
            selected.add(id)
            expectedFilaments.set(id, { id, manufacturer_id, designation, material_type, record_token })
          } else {
            selected.delete(item.id)
            expectedFilaments.delete(item.id)
          }
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
    element('library-hint').textContent = 'Review proposals for the filaments selected in FilaMan before applying changes.'
  } else {
    search.addEventListener('submit', event => { event.preventDefault(); currentPage = 1; void load() })
    prev.addEventListener('click', () => { currentPage--; void load() })
    next.addEventListener('click', () => { currentPage++; void load() })
    window.addEventListener('message', event => {
      if (picker.hidden || event.source !== picker.contentWindow || event.origin !== location.origin ||
          event.data?.type !== 'filaman:filament-picker-selection') return
      const ids: unknown = event.data.filament_ids
      if (!Array.isArray(ids) || ids.length > 50 ||
          !ids.every(id => Number.isSafeInteger(id) && id > 0)) return
      const expected: unknown = event.data.expected_filaments
      if (!Array.isArray(expected) || expected.length !== ids.length ||
          !expected.every(item => item && ids.includes(item.id) && Number.isSafeInteger(item.manufacturer_id) &&
            item.manufacturer_id > 0 && typeof item.designation === 'string' && typeof item.material_type === 'string' &&
            (item.record_token == null || (typeof item.record_token === 'string' && /^[0-9a-f]{32}$/.test(item.record_token)))) ||
          new Set(expected.map(item => item.id)).size !== ids.length) return
      selected.clear()
      expectedFilaments.clear()
      for (const item of expected) {
        const { id, manufacturer_id, designation, material_type, record_token } = item
        selected.add(id)
        expectedFilaments.set(id, { id, manufacturer_id, designation, material_type, record_token })
      }
      const resetNotice = t('filaBrain.libraryInventoryChanged')
      if (event.data.inventory_changed === true) message.textContent = resetNotice
      else if (ids.length && message.textContent === resetNotice) message.textContent = ''
      updateSelection()
    })
    message.textContent = 'Loading filaments…'
    picker.addEventListener('load', () => {
      if (picker.contentDocument?.querySelector('[data-filament-picker="1"]')) {
        message.textContent = ''
        picker.hidden = false
      } else {
        picker.remove()
        element('library-fallback').hidden = false
        void load()
      }
    }, { once: true })
    picker.src = '/filament-picker/'
  }

  function clearLibrarySelection(refresh = false) {
    const notice = refresh ? t('filaBrain.libraryInventoryChanged') : ''
    selected.clear()
    expectedFilaments.clear()
    message.textContent = notice
    if (picker.hidden) {
      if (refresh) void load(notice)
      else for (const checkbox of rows.querySelectorAll<HTMLInputElement>('input:checked')) checkbox.checked = false
      search.querySelector('input')?.focus()
    } else {
      picker.contentWindow?.postMessage({ type: 'filaman:filament-picker-clear-selection', refresh }, location.origin)
      picker.focus()
    }
    updateSelection()
  }

  clearSelection.addEventListener('click', () => clearLibrarySelection())

  form.addEventListener('submit', async event => {
    event.preventDefault()
    if (submit.disabled) return
    const mode = new FormData(form).get('mode') as BatchMode
    busy = true
    updateSelection()
    message.textContent = 'Starting research…'
    try {
      const batch = await createResearchBatch([...selected], mode, verifiesSelection ? [...expectedFilaments.values()] : undefined)
      location.assign(batchHref(batch.id))
    } catch (error) {
      busy = false
      if (error instanceof ApiError && error.code === 'filament_selection_changed') {
        if (context) {
          selected.clear()
          message.textContent = t('filaBrain.libraryInventoryChanged')
          updateSelection()
        } else clearLibrarySelection(true)
      } else {
        message.textContent = errorText(error)
        updateSelection()
      }
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
    const status = await api.get<Status>('/filabrain/status', { cache: 'no-store' })
    document.addEventListener('filaman:mcp-state', event => {
      status.mcp_enabled = (event as CustomEvent<boolean>).detail
    })
    document.addEventListener('filaman:model-change', event => {
      status.chat_model = (event as CustomEvent<string>).detail || null
    })
    if (!status.enabled) {
      message.textContent = 'FilaBrain is disabled.'
      return
    }
    const setGuide = mountDocs()
    if (hostMode === 'editor' || hostMode === 'selection') {
      const context = await hostRequest<EditorContext | SelectionContext>('filaman:filament-context')
      if (hostMode === 'editor' && (context.mode === 'create' || context.mode === 'edit')) {
        if (context.mode === 'create' ? !status.can_create : !status.can_update) throw new Error('Filament access is required.')
        element('plugin-header').hidden = true
        const chatView = document.querySelector<HTMLElement>('[data-view="chat"]')!
        const settingsView = document.querySelector<HTMLElement>('[data-view="settings"]')!
        const settingsButton = chatView.querySelector<HTMLButtonElement>('[data-editor-settings]')!
        const back = element<HTMLButtonElement>('settings-back-to-chat')
        let settingsMounted = false
        function showChat() {
          settingsView.hidden = true
          chatView.hidden = false
        }
        function openEditorSettings() {
          chatView.hidden = true
          settingsView.hidden = false
          back.focus()
          if (status.can_configure && !settingsMounted) {
            settingsMounted = true
            void mountSettings()
          }
        }
        element('research-admin-settings').hidden = !status.can_configure
        settingsButton.hidden = !status.can_configure && !status.mcp_enabled
        settingsButton.addEventListener('click', openEditorSettings)
        back.hidden = false
        back.addEventListener('click', () => {
          showChat()
          settingsButton.focus()
        })
        const chat = mountChat(status.chat_model, status.can_configure ? openEditorSettings : undefined,
          context.filament_id ? 'editor:' + context.filament_id : '')
        mountResearch(status, chat, showChat, context)
      } else if (hostMode === 'selection' && context.mode === 'selection' && status.can_update) {
        element('plugin-header').hidden = true
        document.querySelector<HTMLElement>('[data-view="library"]')!.hidden = false
        mountLibrary(context)
      } else throw new Error('This FilaMan selection is unavailable.')
      message.textContent = ''
      return
    }
    let chat: ReturnType<typeof mountChat> | undefined
    let startResearch: (() => void) | undefined
    let draftOpen = false
    const canOpenSettings = () => status.can_configure || (draftOpen && status.mcp_enabled)
    element('research-admin-settings').hidden = !status.can_configure
    function openConversation() {
      const view = document.querySelector<HTMLElement>('[data-view="chat"]')!
      view.hidden = false
      chat ??= mountChat(status.chat_model, status.can_configure ? openSettings : undefined, undefined, !status.can_chat)
      if (status.can_create && !startResearch) {
        startResearch = mountResearch(status, chat, () => setGuide('new'))
      }
    }
    const mount: Record<string, (() => void) | false> = {
      chat: status.can_chat && (() => { openConversation(); chat?.showChat() }),
      new: status.can_create && (() => { openConversation(); startResearch?.() }),
      library: status.can_update && (() => mountLibrary()),
      batches: status.can_update && (() => {
        const batch = element('filament-batches')
        batch.querySelector<HTMLElement>('[data-batch-history-link]')!.hidden = !id
        batch.querySelector<HTMLElement>('[data-batch-message]')!.removeAttribute('data-i18n')
        mountResearchBatches(batch, id)
      }),
      settings: () => { if (status.can_configure) void mountSettings() },
    }
    const buttons = [...document.querySelectorAll<HTMLButtonElement>('[data-tab]')]
    const visited = new Set<string>()
    function show(tab: string) {
      if (tab === 'settings' && !canOpenSettings()) return
      const run = Object.hasOwn(mount, tab) && mount[tab]
      if (!run) return
      for (const button of buttons) {
        if (button.dataset.tab === tab) button.setAttribute('aria-current', 'page')
        else button.removeAttribute('aria-current')
      }
      setGuide(tab)
      const activeView = tab === 'new' ? 'chat' : tab
      for (const view of document.querySelectorAll<HTMLElement>('[data-view]')) view.hidden = view.dataset.view !== activeView
      if (tab === 'chat' || tab === 'new') {
        run()
      } else if (!visited.has(tab)) {
        visited.add(tab)
        run()
      }
    }
    function openSettings() {
      show('settings')
      element('filabrain-settings').querySelector<HTMLInputElement>('[name="model_url"]')!.focus()
    }
    for (const button of buttons) {
      const tab = button.dataset.tab!
      button.hidden = tab === 'settings' ? !canOpenSettings() : !mount[tab]
      button.addEventListener('click', () => show(tab))
    }
    const available = Object.keys(mount).find(tab => tab === 'settings' ? canOpenSettings() : mount[tab])
    if (!available) {
      message.textContent = 'You do not have permission to use FilaBrain.'
      return
    }
    message.textContent = ''
    document.addEventListener('filabrain:chat-mode', event => {
      const researching = (event as CustomEvent<boolean>).detail
      draftOpen = researching
      const tab = researching ? 'new' : status.can_chat ? 'chat' : 'new'
      for (const button of buttons) {
        if (button.dataset.tab === 'settings') button.hidden = !canOpenSettings()
        if (button.dataset.tab === tab) button.setAttribute('aria-current', 'page')
        else button.removeAttribute('aria-current')
      }
      setGuide(tab)
    })
    const tab = params.get('tab') || available
    show(Object.hasOwn(mount, tab) && (tab === 'settings' ? canOpenSettings() : mount[tab]) ? tab : available)
  } catch (error) {
    message.textContent = errorText(error)
  }
}

void main()
