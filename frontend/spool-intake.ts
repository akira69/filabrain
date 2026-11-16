import { api } from './shared/api'
import { t } from './shared/i18n'

export type SpoolResearchInput = {
  manufacturer?: string
  designation?: string
  material?: string
  sku?: string
  productUrl?: string
}

type Device = { id: number; name: string }
type Reader = { reader_id: string; name?: string }
type ReportedScan = { reader_id?: string; seq: number; uid?: string; spool_id?: number; spool_label?: string }
type ScaleScan = { status: string; timestamp?: string; tag_data?: Record<string, unknown> }
type Candidate = {
  id: number
  designation: string
  manufacturer?: { name: string }
  manufacturer_color_name?: string
  material_type?: string
  material_subgroup?: string
  finish_type?: string
  diameter_mm?: number
  raw_material_weight_g?: number
  colors?: { color?: { hex_code?: string } }[]
}

const label = (key: string, vars?: Record<string, string>) => t('spoolIntake.' + key, vars)
const clean = (value: unknown, limit = 200) => typeof value === 'string' ? value.trim().slice(0, limit) : ''
const positiveId = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const hex = (value: unknown) => {
  const text = clean(value).replace(/^#/, '')
  return /^[0-9a-f]{6}([0-9a-f]{2})?$/i.test(text) ? text.slice(0, 6).toUpperCase() : ''
}

function tagHints(data: ScaleScan['tag_data']): SpoolResearchInput & { color?: string } {
  const material = clean(data?.type, 50)
  const color = hex(data?.color_hex ?? data?.color)
  return {
    manufacturer: clean(data?.brand),
    material,
    color,
    sku: clean(data?.sku ?? data?.article_number),
    designation: clean(data?.designation ?? data?.name) ||
      [material, clean(data?.subtype), color && '#' + color].filter(Boolean).join(' '),
  }
}

function typedHints(value: string): SpoolResearchInput {
  if (/^https?:\/\//i.test(value)) return { productUrl: value }
  if (/^(?=.*\d)[\w.-]+$/.test(value)) return { sku: value }
  return { designation: value }
}

export function mountSpoolIntake(
  pane: HTMLElement,
  canScan: boolean,
  startResearch?: (input: SpoolResearchInput) => void,
) {
  const form = pane.querySelector<HTMLFormElement>('#spool-lookup')!
  const query = pane.querySelector<HTMLInputElement>('#spool-query')!
  const lookup = form.querySelector<HTMLButtonElement>('button[type=submit]')!
  const reader = pane.querySelector<HTMLSelectElement>('#spool-reader')!
  const scan = pane.querySelector<HTMLButtonElement>('#spool-read-tag')!
  const refresh = pane.querySelector<HTMLButtonElement>('#spool-refresh-readers')!
  const cancel = pane.querySelector<HTMLButtonElement>('#spool-cancel-scan')!
  const scanChoice = pane.querySelector<HTMLElement>('#spool-scan-choice')!
  const newSpool = pane.querySelector<HTMLButtonElement>('#spool-continue-new')!
  const picker = pane.querySelector<HTMLAnchorElement>('#spool-picker')!
  const research = pane.querySelector<HTMLButtonElement>('#spool-research-new')!
  const message = pane.querySelector<HTMLElement>('#spool-intake-status')!
  const details = pane.querySelector<HTMLElement>('#spool-read-details')!
  const results = pane.querySelector<HTMLElement>('#spool-intake-results')!
  const devices = new Map<string, Device>()
  let operation: AbortController | undefined
  let scanned: ReturnType<typeof tagHints> | undefined

  function setBusy(busy: boolean) {
    scan.disabled = busy || !reader.value
    scan.textContent = label(devices.has(reader.value) ? 'readDetails' : 'checkTag')
    refresh.disabled = busy
    reader.disabled = busy || !reader.options.length
    cancel.hidden = !busy
  }

  function stop() {
    operation?.abort()
    operation = undefined
    setBusy(false)
  }

  function updateResearch() {
    research.hidden = !scanChoice.hidden || !startResearch || !(query.value.trim() || scanned?.designation || scanned?.sku)
  }

  function showScanChoice(show: boolean) {
    scanChoice.hidden = !show
    form.hidden = show
    picker.hidden = show
    updateResearch()
  }

  function link(text: string, href: string) {
    const anchor = document.createElement('a')
    anchor.className = 'fm-btn fm-btn-outline'
    anchor.href = href
    anchor.textContent = text
    return anchor
  }

  async function loadReaders() {
    const selected = reader.value
    refresh.disabled = true
    const [active, registered] = await Promise.allSettled([
      api.get<Device[]>('/devices/active', { cache: 'no-store' }),
      api.get<Reader[]>('/tag/readers', { cache: 'no-store' }),
    ])
    const readers = new Map<string, string>()
    if (registered.status === 'fulfilled') {
      for (const item of registered.value) {
        if (item.reader_id) readers.set('report:' + item.reader_id, label('reportedReader', { name: item.name || item.reader_id }))
      }
    }
    devices.clear()
    if (active.status === 'fulfilled') {
      for (const item of active.value) {
        if (!positiveId(item.id) || !item.name) continue
        const id = 'device-' + item.id
        devices.set(id, item)
        readers.set(id, label('activeReader', { name: item.name }))
      }
    }
    reader.replaceChildren(...[...readers].map(([id, name]) => new Option(name, id)))
    if (readers.has(selected)) reader.value = selected
    setBusy(!!operation)
    if (!scanChoice.hidden) return
    if (!readers.size) message.textContent = label(active.status === 'rejected' && registered.status === 'rejected'
      ? 'readersUnavailable' : 'noReaders')
    else if (!operation) message.textContent = ''
  }

  async function find(input: string, hints: typeof scanned, signal: AbortSignal) {
    results.replaceChildren()
    if (!input && !hints?.material && !hints?.manufacturer) {
      message.textContent = label('chooseManual')
      return
    }
    const options = { cache: 'no-store' as const, signal }
    let candidates: { items: Candidate[]; total: number }
    if (hints) {
      const params = new URLSearchParams({ page_size: '200' })
      if (hints.manufacturer) params.set('search', hints.manufacturer)
      if (hints.material) params.set('type', hints.material)
      candidates = await api.get<typeof candidates>(`/filaments?${params}`, options)
    } else {
      const typed = typedHints(input)
      const exact = typed.productUrl || typed.sku
      const search = input.slice(0, 200)
      const searchLibrary = (params: URLSearchParams) =>
        api.get<typeof candidates>(`/filabrain/spool-filaments?${params}`, options)
      candidates = await searchLibrary(new URLSearchParams(exact ? { exact } : { search }))
      if (typed.sku && !candidates.items.length && !signal.aborted) {
        candidates = await searchLibrary(new URLSearchParams({ search }))
      }
    }
    if (signal.aborted) return
    const { items, total } = candidates
    const matches = items.filter(item => positiveId(item.id) && (!hints?.color ||
      item.colors?.some(color => hex(color.color?.hex_code) === hints.color)))
    for (const item of matches.slice(0, 12)) {
      const row = document.createElement('li')
      const summary = document.createElement('span')
      summary.textContent = [
        item.designation, item.manufacturer?.name, item.material_type, item.material_subgroup,
        item.finish_type, item.manufacturer_color_name,
        item.diameter_mm && `${item.diameter_mm} mm`,
        item.raw_material_weight_g && `${item.raw_material_weight_g} g`,
      ].filter(Boolean).join(' · ')
      row.append(summary, link(label('useFilament'), `/spools/new?filament_id=${item.id}`))
      results.append(row)
    }
    message.textContent = label(matches.length ? 'checkMatch' : 'noMatches')
    if (total > items.length || matches.length > 12) message.textContent += ' ' + label('moreMatches')
  }

  const scanRequest = (path: string, signal: AbortSignal) =>
    api.get<ScaleScan>(path, { cache: 'no-store', signal })

  scan.addEventListener('click', async () => {
    if (!reader.value || operation) return
    const device = devices.get(reader.value)
    const devicePath = device && `/devices/${device.id}`
    const readerId = device ? reader.value : reader.value.slice('report:'.length)
    const reportedPath = '/tag/last-scan?' + new URLSearchParams({ reader_id: readerId })
    const controller = new AbortController()
    const { signal } = controller
    operation = controller
    setBusy(true)
    scanned = undefined
    details.textContent = ''
    results.replaceChildren()
    showScanChoice(false)
    message.textContent = label('waiting')
    const timer = window.setTimeout(() => controller.abort(), 32_000)
    const readReport = () => api.get<ReportedScan>(reportedPath, { cache: 'no-store', signal })
    try {
      const [baseline, previous] = await Promise.all([
        readReport().catch(() => null),
        devicePath ? scanRequest(devicePath + '/tag-scan-result', signal).catch(() => null) : null,
      ])
      if (signal.aborted) return
      let requested = false
      if (devicePath && previous) {
        requested = await api.post(devicePath + '/request-tag-scan', {}, { signal }).then(() => true, () => false)
        if (!requested && !signal.aborted) message.textContent = label('placeTag')
      }
      if (!baseline && !requested) throw new Error(label('scanUnavailable'))
      let unknown = false
      while (!signal.aborted) {
        const [reported, data] = await Promise.all([
          baseline ? readReport().catch(() => null) : null,
          requested ? scanRequest(devicePath + '/tag-scan-result', signal).catch(() => null) : null,
        ])
        if (signal.aborted) break
        if (reported?.reader_id === readerId && reported.seq > (baseline?.seq ?? 0)) {
          if (positiveId(reported.spool_id)) {
            const row = document.createElement('li')
            row.append(link(label('openSpool'), `/spools/${reported.spool_id}`))
            results.append(row)
            message.textContent = label('linkedTag', { name: reported.spool_label || String(reported.spool_id) })
            return
          }
          if (reported.uid && !unknown) {
            unknown = true
            if (requested) message.textContent = label('waitingForDetails')
          }
        }
        if (data?.timestamp && Number.isFinite(Date.parse(data.timestamp)) &&
            (!previous?.timestamp || Date.parse(data.timestamp) > Date.parse(previous.timestamp))) {
          if (data.status === 'success' && data.tag_data) {
            scanned = tagHints(data.tag_data)
            const identity = [scanned.manufacturer, scanned.designation, scanned.sku].filter(Boolean).join(' · ')
            details.textContent = identity ? label('tagDetails', { identity }) : ''
            // The active scan has no UID tying its details or Spoolman ID to a FilaMan spool.
            const spoolId = data.tag_data.sm_id
            message.textContent = positiveId(spoolId) ? label('verifyTagId', { id: String(spoolId) }) : label('verifyTag')
            showScanChoice(true)
            return
          }
          if (data.status === 'error') {
            requested = false
            if (!baseline) throw new Error(label('scanUnavailable'))
          }
        }
        if (unknown && !requested) {
          message.textContent = label('unlinkedTag')
          return
        }
        await new Promise(resolve => window.setTimeout(resolve, 650))
      }
      if (operation === controller && unknown) message.textContent = label('unlinkedTag')
    } catch (error) {
      if (!signal.aborted) message.textContent = error instanceof Error ? error.message : label('scanUnavailable')
    } finally {
      window.clearTimeout(timer)
      if (operation === controller) {
        operation = undefined
        setBusy(false)
        if (signal.aborted && message.textContent !== label('unlinkedTag')) message.textContent = label('scanTimeout')
      }
    }
  })

  reader.addEventListener('change', () => setBusy(!!operation))
  refresh.addEventListener('click', () => { void loadReaders() })
  cancel.addEventListener('click', () => {
    stop()
    message.textContent = label('scanCanceled')
    scan.focus()
  })

  query.addEventListener('input', () => {
    stop()
    scanned = undefined
    details.textContent = ''
    lookup.disabled = !query.value.trim()
    results.replaceChildren()
    message.textContent = ''
    updateResearch()
  })

  async function runLookup(input: string, hints?: typeof scanned) {
    stop()
    const controller = new AbortController()
    operation = controller
    setBusy(true)
    message.textContent = label('searching')
    try {
      await find(input, hints, controller.signal)
    } catch (error) {
      if (!controller.signal.aborted) message.textContent = error instanceof Error ? error.message : label('noMatches')
    } finally {
      if (operation === controller) stop()
    }
  }

  form.addEventListener('submit', event => {
    event.preventDefault()
    if (lookup.disabled || form.hidden) return
    scanned = undefined
    details.textContent = ''
    updateResearch()
    void runLookup(query.value.trim())
  })

  newSpool.addEventListener('click', () => {
    showScanChoice(false)
    void runLookup('', scanned)
    message.focus()
  })

  research.addEventListener('click', () => {
    if (startResearch) startResearch(scanned ?? typedHints(query.value.trim()))
  })
  pane.querySelector<HTMLElement>('#spool-reader-controls')!.hidden = !canScan
  if (canScan) void loadReaders()
  window.addEventListener('pagehide', stop)
  return () => {
    if (!operation) return
    stop()
    message.textContent = ''
  }
}
