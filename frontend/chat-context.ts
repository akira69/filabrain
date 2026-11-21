import { api, fetchAllPages } from './shared/api'
import { t } from './shared/i18n'

export type ChatContextSnapshot = { printer_id?: number; filament_id?: number; nozzle_mm?: number }
type Capabilities = { can_read_printers?: boolean; can_read_spools?: boolean; can_read_display?: boolean }
type Printer = { id: number; name: string; is_active: boolean }
type Filament = { id: number; designation: string; manufacturer?: { name: string } }
type Slot = { slot: number; empty: boolean; active: boolean; spool_id?: number | null; extruder?: string | null }
type DisplayPrinter = { id: number; connected?: boolean; active?: { ams_id: number; slot: number } | null;
  ams?: { ams_id: number; slots: Slot[] }[] }

export function mountChatContext(form: HTMLFormElement, capabilities: Capabilities, enabled = true) {
  const panel = form.querySelector<HTMLDetailsElement>('[data-chat-context]')!
  const printer = panel.querySelector<HTMLSelectElement>('[data-chat-printer]')!
  const filament = panel.querySelector<HTMLSelectElement>('[data-chat-filament]')!
  const search = panel.querySelector<HTMLInputElement>('[data-chat-filament-search]')!
  const nozzle = panel.querySelector<HTMLSelectElement>('[data-chat-nozzle]')!
  const status = panel.querySelector<HTMLElement>('[data-chat-context-status]')!
  const suggestion = document.createElement('p')
  suggestion.className = 'fm-field-hint'
  suggestion.hidden = true
  status.after(suggestion)
  let printers: Printer[] = []
  let initialFilaments: Filament[] = []
  let displayPrinters: DisplayPrinter[] = []
  let printerTouched = false
  let revision = 0
  let timer = 0
  let query: AbortController | undefined
  let spool: AbortController | undefined
  let restoring: AbortController | undefined
  const label = (item: Filament) => [item.manufacturer?.name, item.designation].filter(Boolean).join(' · ')
  const options = (select: HTMLSelectElement, items: [number, string][], preserve = true) => {
    const selected = preserve ? select.selectedOptions[0] : undefined
    const value = selected?.value
    select.replaceChildren(new Option(t('filaBrain.chatNoSelection'), ''))
    for (const [id, name] of items) select.add(new Option(name, String(id)))
    if (value && !items.some(([id]) => String(id) === value)) select.add(selected!)
    select.value = value ?? ''
  }
  const setFilaments = (items: Filament[], preserve = true) => options(filament, items.map(item => [item.id, label(item)]), preserve)
  const clearSuggestion = () => {
    spool?.abort()
    suggestion.replaceChildren()
    suggestion.hidden = true
  }
  const cancel = () => {
    revision++
    window.clearTimeout(timer)
    query?.abort()
    restoring?.abort()
    clearSuggestion()
  }
  async function suggest() {
    clearSuggestion()
    if (!enabled || !capabilities.can_read_printers || !capabilities.can_read_spools || !capabilities.can_read_display) return
    const selected = printer.value
    const state = displayPrinters.find(item => String(item.id) === selected)
    if (state?.connected !== true || !Array.isArray(state.ams)) return
    const slots = state.ams.flatMap(unit => Array.isArray(unit.slots)
      ? unit.slots.map(slot => ({ ...slot, ams_id: unit.ams_id })) : [])
    const active = slots.filter(slot => slot.active && !slot.empty)
    // Two loaded hotends can refer to different materials even while one is feeding.
    if (active.length !== 1 || new Set(slots.filter(slot => !slot.empty && slot.extruder).map(slot => slot.extruder)).size > 1) return
    const loaded = active[0]
    if (!Number.isInteger(loaded.spool_id) || !loaded.spool_id || loaded.spool_id < 1) return
    if (state.active && (state.active.ams_id !== loaded.ams_id || state.active.slot !== loaded.slot)) return
    const generation = revision
    const controller = spool = new AbortController()
    try {
      const item = await api.get<{ filament_id: number; filament?: Filament }>(`/spools/${loaded.spool_id}`, { signal: controller.signal, cache: 'no-store' })
      if (controller.signal.aborted || generation !== revision || printer.value !== selected || !item.filament || item.filament.id !== item.filament_id) return
      const note = document.createElement('span')
      note.textContent = t('filaBrain.chatContextSuggestion', { printer: printers.find(item => String(item.id) === selected)?.name ?? '' }) + ' '
      const use = document.createElement('button')
      use.type = 'button'
      use.className = 'fm-btn fm-btn-secondary fm-btn-sm'
      use.dataset.chatUseLoadedFilament = ''
      use.textContent = t('filaBrain.chatUseLoadedFilament', { filament: label(item.filament) })
      use.addEventListener('click', () => {
        if (generation !== revision || printer.value !== selected || filament.disabled) return
        if (![...filament.options].some(option => option.value === String(item.filament_id))) filament.add(new Option(label(item.filament!), String(item.filament_id)))
        filament.value = String(item.filament_id)
        filament.dispatchEvent(new Event('change', { bubbles: true }))
      })
      suggestion.replaceChildren(note, use)
      suggestion.hidden = false
    } catch { /* Optional live data must not block the saved choices. */ }
  }
  function defaultPrinter() {
    const active = printers.filter(item => item.is_active)
    if (!printerTouched && active.length === 1) printer.value = String(active[0].id)
  }
  options(printer, [])
  setFilaments([])
  panel.querySelector<HTMLElement>('[data-chat-printer-field]')!.hidden = !capabilities.can_read_printers
  const initial = enabled ? Promise.all([
    capabilities.can_read_printers ? fetchAllPages<Printer>('/api/v1/printers').then(({ items }) => {
      printers = items
      options(printer, items.map(item => [item.id, item.is_active ? item.name : t('filaBrain.chatPrinterInactive', { name: item.name })]))
      defaultPrinter()
      void suggest()
    }).catch(() => { status.textContent = t('filaBrain.chatContextUnavailable') }) : Promise.resolve(),
    api.get<{ items: Filament[] }>('/filaments?page_size=25', { cache: 'no-store' }).then(({ items }) => {
      initialFilaments = items
      if (!search.value) setFilaments(items)
    }).catch(() => { status.textContent = t('filaBrain.chatContextUnavailable') }),
  ]).then(() => {}) : Promise.resolve()
  if (enabled && capabilities.can_read_printers && capabilities.can_read_display && capabilities.can_read_spools) {
    void api.get<{ printers: DisplayPrinter[] }>('/display?fields=slots', { cache: 'no-store' }).then(data => {
      displayPrinters = Array.isArray(data.printers) ? data.printers : []
      void suggest()
    }).catch(() => {})
  }
  printer.addEventListener('change', () => {
    printerTouched = true
    cancel()
    void suggest()
  })
  for (const field of [filament, nozzle]) field.addEventListener('change', cancel)
  search.addEventListener('keydown', event => { if (event.key === 'Enter') event.preventDefault() })
  search.addEventListener('input', () => {
    cancel()
    if (!enabled) return
    const term = search.value.trim()
    if (!term) {
      setFilaments(initialFilaments)
      return
    }
    const generation = revision
    timer = window.setTimeout(async () => {
      const controller = query = new AbortController()
      try {
        const params = new URLSearchParams({ search: term, page_size: '25' })
        const { items } = await api.get<{ items: Filament[] }>(`/filaments?${params}`, { signal: controller.signal, cache: 'no-store' })
        if (generation !== revision || controller.signal.aborted) return
        status.textContent = ''
        setFilaments(items)
      } catch {
        if (generation === revision && !controller.signal.aborted) status.textContent = t('filaBrain.chatContextUnavailable')
      }
    }, 200)
  })
  return {
    snapshot(): ChatContextSnapshot {
      return {
        ...(printer.value ? { printer_id: Number(printer.value) } : {}),
        ...(filament.value ? { filament_id: Number(filament.value) } : {}),
        ...(nozzle.value ? { nozzle_mm: Number(nozzle.value) } : {}),
      }
    },
    async reset() {
      cancel()
      const generation = revision
      printerTouched = false
      printer.value = filament.value = nozzle.value = search.value = ''
      status.textContent = ''
      setFilaments(initialFilaments, false)
      await initial
      if (generation !== revision) return
      defaultPrinter()
      void suggest()
    },
    async restore(saved: ChatContextSnapshot = {}) {
      cancel()
      const generation = revision
      printerTouched = true
      printer.value = filament.value = nozzle.value = search.value = ''
      status.textContent = ''
      await initial
      if (generation !== revision || !enabled) return
      if (capabilities.can_read_printers && Number.isInteger(saved.printer_id)) printer.value = String(saved.printer_id)
      if (typeof saved.nozzle_mm === 'number') nozzle.value = String(saved.nozzle_mm)
      setFilaments(initialFilaments, false)
      if (Number.isInteger(saved.filament_id) && saved.filament_id! > 0) {
        const value = String(saved.filament_id)
        if (![...filament.options].some(option => option.value === value)) {
          const controller = restoring = new AbortController()
          try {
            const item = await api.get<Filament>(`/filaments/${saved.filament_id}`, { signal: controller.signal, cache: 'no-store' })
            if (generation !== revision || controller.signal.aborted) return
            if (item.id === saved.filament_id) filament.add(new Option(label(item), value))
          } catch {
            if (generation === revision && !controller.signal.aborted) status.textContent = t('filaBrain.chatContextUnavailable')
          }
        }
        if (generation !== revision) return
        filament.value = value
      }
      void suggest()
    },
  }
}
