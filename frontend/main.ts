import { mountChat } from './chat'
import { mountDocs } from './docs'
import { mountWorkspaceTabs } from './tabs'
import { mountSettings } from './settings'
import { mountSpoolIntake, type SpoolResearchInput } from './spool-intake'
import { mountResearch, type EditorContext, type SelectionContext } from './research'
import { ApiError, api } from './shared/api'
import { batchHref, createResearchBatch, listResearchRuns, mountResearchBatches, type BatchMode, type ExpectedFilament } from './shared/filabrain-batches'
import { initI18n, t } from './shared/i18n'
import { mountTheme } from './theme'

export type Status = {
  enabled: boolean
  can_chat: boolean
  microphone_enabled?: boolean
  microphone_endpoint_id?: string | null
  microphone_endpoint_name?: string | null
  microphone_revision?: string
  chat_model?: string | null
  chat_endpoint_id?: string | null
  can_create: boolean
  can_add_spool?: boolean
  can_read_spools?: boolean
  can_read_display?: boolean
  can_read_printers?: boolean
  can_update: boolean
  can_update_manufacturer?: boolean
  can_create_manufacturer?: boolean
  can_create_color?: boolean
  can_configure: boolean
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
    element('library-selected').textContent = t('filaBrain.librarySelected', { count: selected.size })
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
    const libraryToolbar = picker.parentElement!.querySelector<HTMLElement>('.library-toolbar')!
    const outsidePicker = [element('plugin-header'), libraryToolbar, element('filabrain-docs')]
    function setColorOverlay(open: boolean) {
      const root = picker.contentDocument?.documentElement
      if (open && !picker.hasAttribute('data-color-overlay') && root) {
        const rect = picker.getBoundingClientRect()
        for (const key of ['left', 'top', 'width', 'height'] as const) {
          root.style.setProperty(`--filabrain-picker-${key}`, `${rect[key]}px`)
        }
      }
      root?.toggleAttribute('data-filabrain-color-overlay', open)
      picker.toggleAttribute('data-color-overlay', open)
      for (const node of outsidePicker) node.inert = open
    }
    let colorObserver: MutationObserver | undefined
    let viewportWidth = window.innerWidth
    window.addEventListener('resize', () => {
      if (viewportWidth === window.innerWidth) return
      viewportWidth = window.innerWidth
      picker.contentDocument?.querySelector<HTMLButtonElement>('th.col-colors .fm-header-filter-trigger[aria-expanded="true"]')?.click()
    })
    picker.addEventListener('load', () => {
      colorObserver?.disconnect()
      setColorOverlay(false)
      if (picker.contentDocument?.querySelector('[data-filament-picker="1"]')) {
        message.textContent = ''
        const doc = picker.contentDocument
        const surface = doc.createElement('style')
        surface.textContent = `
          html, body, #fm-page { background: var(--bg-elevated); }
          html, body { height: 100%; overflow: hidden; }
          #fm-page { height: 100%; min-height: 0; }
          #fm-page > .fm-main { box-sizing: border-box; height: 100%; min-height: 0;
            display: flex; flex-direction: column; overflow: hidden; padding: 0 6px !important; }
          .fm-main > :not(.fm-inventory-table) { flex-shrink: 0; }
          .fm-inventory-table { flex: 1; min-height: 0; overflow: auto; margin-bottom: 4px; }
          /* Column resizing sets position inline; vertical header locking must take precedence. */
          .fm-inventory-table .fm-table th { position: sticky !important; top: 0; z-index: 4; background: var(--bg-soft); }
          .fm-inventory-table .fm-table th[data-column-pin] { z-index: 5; }
          html[data-filabrain-color-overlay], html[data-filabrain-color-overlay] body { background: transparent; }
          html[data-filabrain-color-overlay] #fm-page { position: absolute;
            left: var(--filabrain-picker-left); top: var(--filabrain-picker-top);
            width: var(--filabrain-picker-width); height: var(--filabrain-picker-height); }
        `
        doc.head.append(surface)
        const table = doc.querySelector('.fm-inventory-table')
        const more = doc.querySelector('.fm-inventory-more')
        if (table && more) table.append(more)
        const count = doc.getElementById('picker-selection-count')
        if (count) count.hidden = true
        picker.hidden = false
        const pickerPage = doc.getElementById('fm-page')
        const colorTrigger = () => doc.querySelector<HTMLButtonElement>('th.col-colors .fm-header-filter-trigger')
        function closeColorOverlay() {
          const trigger = colorTrigger()
          if (trigger?.getAttribute('aria-expanded') === 'true') trigger.click()
        }
        colorObserver = new MutationObserver(changes => {
          if (changes.some(change => (change.target as Element).matches('.fm-header-filter-panel-color'))) {
            const panel = doc.querySelector('.fm-header-filter-panel-color.open')
            if (panel && !panel.querySelector('[data-picker-close-colors]')) {
              const close = doc.createElement('button')
              close.type = 'button'
              close.className = 'fm-btn fm-btn-outline fm-header-filter-action'
              close.textContent = t('common.close')
              close.dataset.pickerCloseColors = ''
              close.style.margin = '0 auto 6px 0'
              close.style.display = 'flex'
              close.addEventListener('click', closeColorOverlay)
              panel.prepend(close)
            }
            const wasOpen = picker.hasAttribute('data-color-overlay')
            setColorOverlay(!!panel)
            if (pickerPage) pickerPage.inert = !!panel
            if (!panel && wasOpen) colorTrigger()?.focus({ preventScroll: true })
          }
        })
        colorObserver.observe(doc.body, { subtree: true, attributes: true, attributeFilter: ['class'] })
        doc.addEventListener('keydown', event => {
          if (event.key === 'Escape' && picker.hasAttribute('data-color-overlay')) {
            closeColorOverlay()
          }
        })
      } else {
        picker.remove()
        element('library-fallback').hidden = false
        void load()
      }
    })
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

function scopeResearchIds(root: HTMLElement, prefix: string) {
  const nodes = [root, ...root.querySelectorAll<HTMLElement>('*')]
  const ids = new Map<string, string>()
  for (const node of nodes) if (node.id) {
    node.dataset.researchId = node.dataset.researchId || node.id
    ids.set(node.id, prefix + node.id)
    node.id = ids.get(node.id)!
  }
  for (const node of nodes) for (const attribute of ['for', 'aria-labelledby', 'aria-describedby', 'aria-controls']) {
    const value = node.getAttribute(attribute)
    if (value) node.setAttribute(attribute, value.split(' ').map(id => ids.get(id) ?? id).join(' '))
  }
}

async function main() {
  mountTheme()
  await initI18n()
  const chatTemplate = document.querySelector<HTMLElement>('[data-view="chat"] .research-chat')!.cloneNode(true) as HTMLElement
  for (const [headingId, selector] of [
    ['research-chat-title', '[data-tab="chat"] svg'], ['research-heading', '[data-tab="new"] svg'],
    ['spool-intake-heading', '[data-tab="spool"] svg'], ['library-heading', '[data-tab="library"] svg'],
    ['batch-title', '[data-tab="batches"] svg'], ['model-settings-title', '[data-tab="settings"] svg'],
    ['docs-title', '[data-open-docs] svg'],
  ]) {
    const heading = document.getElementById(headingId)
    const icon = document.querySelector(selector)
    if (heading && icon) {
      const svg = icon.cloneNode(true) as SVGElement
      svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
      heading.classList.add('page-heading')
      heading.style.setProperty('--heading-icon', `url("data:image/svg+xml,${encodeURIComponent(svg.outerHTML)}")`)
    }
  }
  const params = new URLSearchParams(location.search)
  const id = params.get('id')
  const hostMode = window.parent === window ? null : params.get('filament_host')
  const message = element('plugin-status')
  try {
    const status = await api.get<Status>('/filabrain/status', { cache: 'no-store' })
    document.addEventListener('filaman:model-change', event => {
      Object.assign(status, (event as CustomEvent<Pick<Status, 'chat_model' | 'chat_endpoint_id'>>).detail)
    })
    if (!status.enabled) {
      message.textContent = 'FilaBrain is disabled.'
      return
    }
    const setGuide = mountDocs()
    let editorContext: EditorContext | undefined
    if (hostMode === 'editor' || hostMode === 'selection') {
      const context = await hostRequest<EditorContext | SelectionContext>('filaman:filament-context')
      if (hostMode === 'editor' && (context.mode === 'create' || context.mode === 'edit')) {
        if (context.mode === 'create' ? !status.can_create : !status.can_update) throw new Error('Filament access is required.')
        editorContext = context
        const source = document.querySelector<HTMLElement>('[data-view="new"]')!
        const view = source.cloneNode(true) as HTMLElement
        view.dataset.view = 'editor'
        scopeResearchIds(view, 'editor-')
        view.querySelector('[data-research-id="research-heading"]')!.parentElement!.classList.add('workspace-title-only')
        source.before(view)
        const researchTab = document.querySelector<HTMLButtonElement>('[data-tab="new"]')!.cloneNode(true) as HTMLButtonElement
        researchTab.dataset.tab = 'editor'
        for (const label of researchTab.querySelectorAll<HTMLElement>('span')) {
          label.dataset.i18n = 'filaBrain.navResearch'
          label.textContent = t('filaBrain.navResearch')
        }
        document.querySelector('[data-tab="new"]')!.before(researchTab)
      } else if (hostMode === 'selection' && context.mode === 'selection' && status.can_update) {
        element('plugin-header').hidden = true
        document.querySelector<HTMLElement>('[data-view="library"]')!.hidden = false
        mountLibrary(context)
        message.textContent = ''
        return
      } else throw new Error('This FilaMan selection is unavailable.')
    }
    document.addEventListener('filaman:model-change', event => {
      const detail = (event as CustomEvent<Status>).detail
      if (typeof detail.microphone_enabled === 'boolean') status.microphone_enabled = detail.microphone_enabled
      if (detail.microphone_endpoint_id !== undefined) status.microphone_endpoint_id = detail.microphone_endpoint_id
      if (detail.microphone_endpoint_name !== undefined) status.microphone_endpoint_name = detail.microphone_endpoint_name
      if (detail.microphone_revision !== undefined) status.microphone_revision = detail.microphone_revision
    })
    const header = element('plugin-header')
    const nav = header.querySelector('nav')!
    let chat: ReturnType<typeof mountChat> | undefined
    const researchViews = new Map<string, { chat: ReturnType<typeof mountChat>; start: ReturnType<typeof mountResearch> }>()
    let stopSpoolIntake: (() => void) | undefined
    function openConversation() {
      const view = document.querySelector<HTMLElement>('[data-view="chat"]')!
      view.hidden = false
      if (!chat) {
        chat = mountChat(status, status.can_configure ? openSettings : undefined, undefined, false,
          { root: view, openResearch: status.can_create ? () => show('new') : undefined,
            context: editorContext?.filament_id ? { filament_id: editorContext.filament_id } : undefined })
        workspace.attachChat('chat', view)
      }
    }
    function openResearch(tab = 'new') {
      const view = document.querySelector<HTMLElement>(`[data-view="${tab}"]`)!
      const context = tab === 'editor' ? editorContext : undefined
      view.hidden = false
      if (!researchViews.has(tab)) {
        const clone = chatTemplate.cloneNode(true) as HTMLElement
        scopeResearchIds(clone, tab + '-chat-')
        clone.querySelector('form')!.id = 'filabrain-' + tab + '-chat-form'
        view.append(clone)
        const chat = mountChat(status, status.can_configure ? openSettings : undefined,
          context ? context.filament_id ? 'editor:' + context.filament_id : '' : undefined, true, { root: view })
        const start = mountResearch(status, chat, () => setGuide(tab), context, view)
        researchViews.set(tab, { chat, start })
        workspace.attachChat(tab, view)
      }
      return researchViews.get(tab)!
    }
    const mount: Record<string, (() => void) | false> = {
      chat: status.can_chat && (() => { openConversation(); chat?.showChat() }),
      new: status.can_create && (() => { openResearch().start() }),
      editor: !!editorContext && (() => { openResearch('editor').start() }),
      spool: !!status.can_add_spool && (() => {
        const researchFromSpool = status.can_create ? (input: SpoolResearchInput) => {
          show('new', input)
        } : undefined
        stopSpoolIntake = mountSpoolIntake(
          document.querySelector<HTMLElement>('[data-view="spool"]')!, !!status.can_read_spools, researchFromSpool,
        )
      }),
      library: status.can_update && (() => mountLibrary()),
      batches: status.can_update && (() => {
        const batch = element('filament-batches')
        batch.querySelector<HTMLElement>('[data-batch-history-link]')!.hidden = !id
        batch.querySelector<HTMLElement>('[data-batch-message]')!.removeAttribute('data-i18n')
        mountResearchBatches(batch, id)
      }),
      settings: status.can_configure && (() => { void mountSettings() }),
    }
    const buttons = [...document.querySelectorAll<HTMLButtonElement>('[data-tab]')]
    const visited = new Set<string>()
    function show(tab: string, spoolInput?: SpoolResearchInput) {
      const run = Object.hasOwn(mount, tab) && mount[tab]
      if (!run) return
      if (tab !== 'spool') stopSpoolIntake?.()
      for (const button of buttons) {
        if (button.dataset.tab === tab) button.setAttribute('aria-current', 'page')
        else button.removeAttribute('aria-current')
      }
      setGuide(tab)
      chat?.saveDraft()
      for (const research of researchViews.values()) research.chat.saveDraft()
      workspace.select(tab)
      const activeView = tab
      for (const view of document.querySelectorAll<HTMLElement>('[data-view]')) view.hidden = view.dataset.view !== activeView
      if (tab === 'new' && spoolInput) {
        openResearch().start(spoolInput)
      } else if (tab === 'chat' || tab === 'new' || tab === 'editor') {
        run()
      } else if (!visited.has(tab)) {
        visited.add(tab)
        run()
      }
    }
    const workspace = mountWorkspaceTabs(header, show)
    function openSettings() {
      show('settings')
      const form = element('filabrain-settings')
      const url = form.querySelector<HTMLInputElement>('[name="model_url"]')!
      if (url.matches(':disabled')) form.querySelector<HTMLButtonElement>('#research-add-endpoint')!.focus()
      else url.focus()
    }
    for (const button of buttons) {
      const tab = button.dataset.tab!
      button.hidden = !mount[tab] || (tab === 'batches' && params.get('tab') !== 'batches')
      button.addEventListener('click', () => show(tab))
    }
    nav.hidden = !nav.querySelector('button:not([hidden])')
    const available = Object.keys(mount).find(tab => mount[tab])
    if (!available) {
      message.textContent = 'You do not have permission to use FilaBrain.'
      return
    }
    message.textContent = ''
    document.addEventListener('filabrain:chat-mode', event => {
      const researching = (event as CustomEvent<boolean>).detail
      const tab = researching ? (event.target instanceof HTMLElement ? event.target.dataset.view : undefined) || 'new' : status.can_chat ? 'chat' : 'new'
      for (const button of buttons) {
        if (button.dataset.tab === tab) button.setAttribute('aria-current', 'page')
        else button.removeAttribute('aria-current')
      }
      workspace.select(tab)
      setGuide(tab)
    })
    const tab = params.get('tab') || (editorContext ? 'editor' : available)
    show(Object.hasOwn(mount, tab) && mount[tab] ? tab : available)
    if (mount.batches && tab !== 'batches') {
      const runsButton = nav.querySelector<HTMLButtonElement>('[data-tab="batches"]')!
      function refreshRuns() {
        void listResearchRuns().then(runs => { runsButton.hidden = runs.length === 0 }, () => {})
      }
      refreshRuns()
      window.addEventListener('pageshow', event => { if (event.persisted) refreshRuns() })
    }
  } catch (error) {
    message.textContent = errorText(error)
  }
}

void main()
