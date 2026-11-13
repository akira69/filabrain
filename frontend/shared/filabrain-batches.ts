import { isAbortError } from './abort'
import { api, ApiError } from './api'
import { CACHE_KEYS, invalidateCache } from './cache'
import { escapeHtml, formatNumericRange } from './extra-fields'
import { researchFieldLabel, researchOptionValue, type ResearchOption } from './filabrain'
import { t } from './i18n'

const route = '/filabrain/batches'
const page = location.pathname
const tr = (key: string, vars?: Record<string, string | number>) => t('researchBatches.' + key, vars)

export type BatchMode = 'fill' | 'standardize'
type Decision = 'pending' | 'approved' | 'skipped'
type ItemStatus = 'queued' | 'running' | 'ready' | 'no_change' | 'needs_match' | 'failed' | 'unavailable' | 'stale' | 'canceled' | 'applied'
type Value = string | number | { min: number | null; max: number | null } | null
type Proposal = {
  id: string
  fields: Record<string, { old: Value; value: Value }>
  sources: { title: string; url?: string | null; quote?: string }[]
  reason: string
  group_key: string | null
}
type Item = {
  id: number
  filament_id: number
  live_filament_id: number | null
  status: ItemStatus
  decision: Decision
  revision: number
  decision_revision: number
  identity: {
    designation?: string
    manufacturer_name?: string
    material_type?: string
    material_subgroup?: string
    manufacturer_color_name?: string
    manufacturer_sku?: string
    diameter_mm?: number
  }
  proposals: Proposal[]
  issues: { code: string; field?: string; source?: string; name?: string; message?: string; reason?: string; url?: string; options?: ResearchOption[] }[]
  approved_ids: string[]
  changed_fields: string[]
  applied_at: string | null
}
type Summary = {
  id: string
  mode: BatchMode
  status: 'active' | 'finished' | 'canceled'
  created_at: string
  finished_at: string | null
  total: number
}
type Batch = Summary & { items: Item[] }
type Binding = { item_id: number; revision: number; decision_revision: number; approved_ids: string[] }
type ApplyResult = { item_id: number; status: 'applied' | 'already_applied' | 'stale' | 'approval_changed' | 'error'; changed_fields: string[] }
type Preview =
  | { action: 'apply'; items: Binding[] }
  | { action: 'group'; key: string; items: Binding[] }
  | { action: 'cancel' }

const fieldName = (field: string) => researchFieldLabel(field === 'manufacturer_name' ? 'manufacturer_id' : field)
const label = (item: Item) => item.identity.designation || tr('record', { id: item.filament_id })
const status = (item: Item) => tr('status.' + (
  item.decision === 'skipped' && !['unavailable', 'canceled'].includes(item.status) ? 'skipped' :
  item.decision === 'approved' && item.status === 'ready' ? 'approved' : item.status))
const researching = (item: Item) => ['queued', 'running'].includes(item.status)
const canRetry = (item: Item) => ['failed', 'needs_match', 'stale'].includes(item.status)
const reviewable = (item: Item) => item.decision === 'pending' && (item.status === 'ready' || canRetry(item))
const binding = (item: Item, approved_ids: string[]): Binding => ({
  item_id: item.id, revision: item.revision, decision_revision: item.decision_revision, approved_ids,
})
const button = (name: string, action: string, primary = false) =>
  `<button type="button" class="fm-btn fm-btn-${primary ? 'primary' : 'outline'}" data-action="${action}">${escapeHtml(tr(name))}</button>`
const link = (url: string, text: string) =>
  `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(text)}</a>`
const valueText = (value: Value) => value == null || value === '' ? '—'
  : typeof value === 'object' ? formatNumericRange(value) || '—' : String(value)

export function batchHref(id: string) {
  return `${page}?tab=batches&id=${encodeURIComponent(id)}`
}

export function createResearchBatch(filament_ids: number[], mode: BatchMode) {
  return api.post<Batch>(route, { filament_ids, mode })
}

function fieldRows(proposal: Proposal) {
  return Object.entries(proposal.fields).map(([field, { old, value }]) => `
    <li class="batch-change">
      <span>${escapeHtml(fieldName(field))}</span>
      <span>${escapeHtml(valueText(old))}</span>
      <span aria-hidden="true">→</span>
      <strong>${escapeHtml(valueText(value))}</strong>
    </li>`).join('')
}

function fieldsList(proposal: Proposal) {
  return `<ul class="batch-changes" aria-label="${escapeHtml(tr('currentToProposed'))}">${fieldRows(proposal)}</ul>`
}

function proposalCard(proposal: Proposal, checked: boolean, editable: boolean) {
  const name = Object.keys(proposal.fields).map(fieldName).join(' + ')
  return `<li class="batch-proposal">
    ${editable ? `<label class="batch-option"><input type="checkbox" class="fm-checkbox" name="proposal" value="${escapeHtml(proposal.id)}" ${checked ? 'checked' : ''} /> ${escapeHtml(name)}</label>` : ''}
    ${fieldsList(proposal)}
    ${proposal.reason ? `<p>${escapeHtml(proposal.reason)}</p>` : ''}
    ${proposal.sources.length ? `<ul class="batch-evidence">${proposal.sources.map(source => `
      <li>${source.url && /^https?:\/\//i.test(source.url) ? link(source.url, source.title) : escapeHtml(source.title)}
        ${source.quote ? `<blockquote>${escapeHtml(source.quote)}</blockquote>` : ''}
      </li>`).join('')}</ul>` : ''}
  </li>`
}

function issueText(issue: Item['issues'][number]) {
  if (issue.message) return issue.message
  if (issue.code === 'needs_match') return tr('issue.needs_match')
  const key = 'filaBrain.' + issue.code
  const description = t(key, { name: issue.name || issue.source || (issue.field ? fieldName(issue.field) : ''), source: issue.source || '' })
  return description === key ? issue.code.replaceAll('_', ' ') : description
}

function groupsFor(items: Item[]) {
  const groups = new Map<string, Binding[]>()
  for (const item of items) {
    if (!item.live_filament_id || item.status !== 'ready' || item.decision !== 'pending') continue
    for (const { id, group_key: key } of item.proposals) {
      if (!key) continue
      if (!groups.has(key)) groups.set(key, [])
      groups.get(key)!.push(binding(item, [id]))
    }
  }
  return new Map([...groups].filter(([, group]) => group.length > 1))
}

export function mountResearchBatches(host: HTMLElement, id: string | null) {
  const history = host.querySelector<HTMLElement>('[data-batch-history]')!
  const workspace = host.querySelector<HTMLElement>('[data-batch-workspace]')!
  const queue = host.querySelector<HTMLElement>('[data-batch-queue]')!
  const card = host.querySelector<HTMLElement>('[data-batch-review]')!
  const progress = host.querySelector<HTMLElement>('[data-batch-progress]')!
  const message = host.querySelector<HTMLElement>('[data-batch-message]')!
  const report = host.querySelector<HTMLElement>('[data-batch-report]')!
  const repeated = host.querySelector<HTMLElement>('[data-batch-groups]')!
  const apply = host.querySelector<HTMLButtonElement>('[data-action="apply"]')!
  const cancel = host.querySelector<HTMLButtonElement>('[data-action="cancel"]')!
  const dialog = host.querySelector<HTMLDialogElement>('dialog')!
  const dialogHeading = dialog.querySelector<HTMLElement>('h2')!
  const dialogBody = dialog.querySelector<HTMLElement>('[data-batch-preview]')!
  const confirm = dialog.querySelector<HTMLButtonElement>('[data-action="confirm"]')!
  const previewMessage = dialog.querySelector<HTMLElement>('[data-batch-preview-message]')!
  const path = route + '/' + encodeURIComponent(id || '')
  let batch: Batch
  let selectedId: number | undefined
  let cardState = ''
  let timer: number | undefined
  let poll: AbortController | undefined
  let busy = false
  let preview: Preview | undefined
  let lastApply: ApplyResult[] = []
  let accessLost = false

  function recordResults(results: ApplyResult[], fallback?: Batch) {
    lastApply = results
    if (results.some(item => item.status === 'applied' || item.status === 'already_applied')) {
      invalidateCache(CACHE_KEYS.FILAMENTS)
      invalidateCache(CACHE_KEYS.FILAMENT_FILTER_OPTIONS)
    }
    if (!fallback) return
    for (const result of results) {
      const item = fallback.items.find(item => item.id === result.item_id)
      if (!item) continue
      if (result.status === 'applied' || result.status === 'already_applied') {
        item.status = 'applied'
        item.changed_fields = result.changed_fields
      } else if (result.status === 'stale') {
        if (result.changed_fields.includes('filament_id')) {
          item.status = 'unavailable'
          item.live_filament_id = null
        } else item.status = 'stale'
        item.changed_fields = result.changed_fields
        item.decision = 'pending'
        item.approved_ids = []
        item.decision_revision++
      }
    }
  }

  function selectedItem() { return batch.items.find(item => item.id === selectedId) }

  function nextReview(from?: number) {
    const index = batch.items.findIndex(item => item.id === from)
    const ordered = [...batch.items.slice(index + 1), ...batch.items.slice(0, index + 1)]
    return (ordered.find(item => reviewable(item) && item.status === 'ready') || ordered.find(reviewable))?.id
  }

  function openItem(itemId: number | undefined, moveFocus = false) {
    selectedId = itemId
    render()
    if (moveFocus) (card.querySelector<HTMLElement>('h2') || apply).focus()
  }

  function renderCard(item: Item | undefined) {
    const working = batch.items.some(researching)
    const editable = !accessLost && batch.status !== 'canceled' && Boolean(item?.live_filament_id)
    const state = JSON.stringify([editable, item ?? working])
    if (state === cardState) return
    cardState = state
    if (!item) {
      card.innerHTML = `<header class="fm-section-header"><h2 tabindex="-1">${escapeHtml(tr('review'))}</h2></header>
        <div class="fm-section-body"><p>${escapeHtml(tr(working ? 'waiting' : 'chooseItem'))}</p></div>`
      return
    }
    const identity = item.identity
    const details = [identity.manufacturer_name, identity.material_type, identity.material_subgroup,
      identity.manufacturer_color_name, identity.manufacturer_sku,
      identity.diameter_mm == null ? '' : identity.diameter_mm + ' mm'].filter(Boolean)
    const editor = item.live_filament_id
      ? link(`/filaments/${item.live_filament_id}?edit=1`, tr('openEditor')) : ''
    const choosing = editable && item.status === 'ready' && item.decision !== 'skipped'
    const canReopen = editable && item.decision === 'skipped' &&
      ['ready', 'no_change', 'failed', 'needs_match', 'stale'].includes(item.status)
    const previous = batch.items.indexOf(item) > 0 ? button('back', 'back') : ''
    card.innerHTML = `
      <header class="fm-section-header"><h2 tabindex="-1">${escapeHtml(label(item))}</h2><span class="batch-status">${escapeHtml(status(item))}</span></header>
      <div class="fm-section-body">
      <p class="batch-muted">${escapeHtml([tr('record', { id: item.filament_id }), ...details].join(' · '))}</p>
      ${editor ? `<p>${editor}</p>` : ''}
      ${item.proposals.length ? `<ul class="batch-proposals">${item.proposals
        .filter(proposal => item.status !== 'applied' || item.approved_ids.includes(proposal.id)).map(proposal =>
        proposalCard(proposal, item.approved_ids.includes(proposal.id), choosing)).join('')}</ul>` : ''}
      ${item.issues.length || item.changed_fields.length && item.status === 'stale' ? `
        <h3>${escapeHtml(tr('issues'))}</h3><ul class="batch-evidence">
          ${item.issues.map(issue => `<li>${escapeHtml(issueText(issue))}${issue.reason ? ` (${escapeHtml(issue.reason)})` : ''}
            ${issue.url && /^https?:\/\//i.test(issue.url) ? link(issue.url, tr('source')) : ''}
            ${issue.options?.length ? `<ul>${issue.options.map(option => `
              <li>${escapeHtml(researchOptionValue(option.value))}
                ${option.sources.map(source => source.url && /^https?:\/\//i.test(source.url)
                  ? link(source.url, source.title) : escapeHtml(source.title)).join(' ')}
              </li>`).join('')}</ul>` : ''}
          </li>`).join('')}
          ${item.status === 'stale' ? item.changed_fields.map(field => `<li>${escapeHtml(fieldName(field))}</li>`).join('') : ''}
        </ul>` : ''}
      ${editable && canRetry(item) && item.decision !== 'skipped' ? `
        <form class="batch-retry">
          <label>${escapeHtml(tr('retrySku'))}<input class="fm-input" name="sku_hint" maxlength="250" /></label>
          <label>${escapeHtml(tr('retryUrl'))}<input class="fm-input" name="product_url" type="url" maxlength="2000" /></label>
          <button type="submit" class="fm-btn fm-btn-outline">${escapeHtml(tr('retry'))}</button>
        </form>` : ''}
      <div class="batch-actions">${previous}
        ${canReopen ? button('reopen', 'reopen') : ''}
        ${editable && item.decision !== 'skipped' && (choosing || canRetry(item)) ? button('skip', 'skip') : ''}
        ${choosing ? button('approve', 'approve', true) : ''}
      </div></div>`
    updateApproveButton()
  }

  function updateApproveButton() {
    const approve = card.querySelector<HTMLButtonElement>('[data-action="approve"]')
    if (approve) approve.disabled = !card.querySelector('[name="proposal"]:checked')
  }

  function approvalSummary(items: Binding[]) {
    return `<ul class="batch-proposals">${items.map(({ item_id, approved_ids }) => {
      const item = batch.items.find(candidate => candidate.id === item_id)!
      return `<li class="batch-proposal"><h3>${escapeHtml(label(item))} <small>${escapeHtml(tr('record', { id: item.filament_id }))}</small></h3>
        ${item.proposals.filter(proposal => approved_ids.includes(proposal.id)).map(fieldsList).join('')}</li>`
    }).join('')}</ul>`
  }

  function renderReport() {
    const categories = [
      ['applied', batch.items.filter(item => item.status === 'applied')],
      ['stale', batch.items.filter(item => item.status === 'stale' ||
        (item.status === 'unavailable' && item.changed_fields.length > 0) ||
        lastApply.some(result => result.item_id === item.id && result.status === 'stale'))],
      ['pending', batch.items.filter(item => item.decision !== 'skipped' && (researching(item) || ['ready', 'needs_match', 'failed'].includes(item.status)))],
      ['approval_changed', batch.items.filter(item => lastApply.some(result => result.item_id === item.id && result.status === 'approval_changed'))],
      ['error', batch.items.filter(item => lastApply.some(result => result.item_id === item.id && result.status === 'error'))],
    ] as const
    report.hidden = !lastApply.length && !categories.slice(0, 2).some(([, items]) => items.length)
    const expanded = new Set(Array.from(report.querySelectorAll<HTMLDetailsElement>('details[open]'), el => el.dataset.result))
    report.innerHTML = report.hidden ? '' : `
      <header class="fm-section-header"><h2 tabindex="-1">${escapeHtml(tr('report'))}</h2></header>
      <div class="fm-section-body">
      ${categories.filter(([key, items]) => items.length || ['applied', 'stale', 'pending'].includes(key))
        .map(([key, items]) => `<details data-result="${key}" ${expanded.has(key) ? 'open' : ''}>
          <summary>${escapeHtml(tr('result.' + key))} (${items.length})</summary>
          <ul>${items.map(item => `<li>
            <button class="batch-text-button" data-item="${item.id}" type="button">${escapeHtml(label(item))}</button>
            ${item.live_filament_id ? link(`/filaments/${item.live_filament_id}?edit=1`, tr('openEditor')) : ''}
          </li>`).join('')}</ul>
        </details>`).join('')}</div>`
  }

  function render() {
    const available = !accessLost && batch.status !== 'canceled'
    const working = batch.items.filter(researching).length
    const ready = batch.items.filter(item => item.status === 'ready' && item.decision === 'pending').length
    const approved = batch.items.filter(item => item.status === 'ready' && item.decision === 'approved')
    progress.textContent = tr('progress', { mode: tr('mode.' + batch.mode), ready, working, total: batch.total })
      + ' · ' + tr('status.' + batch.status)
    apply.hidden = !available || !approved.length
    cancel.hidden = !available || batch.status !== 'active'
    const focusedItem = queue.querySelector<HTMLButtonElement>('button[data-item]:focus')?.dataset.item
    queue.innerHTML = batch.items.map(item => `<li><button type="button" data-item="${item.id}"
      ${item.id === selectedId ? 'aria-current="true"' : ''}>
      <span>${escapeHtml(label(item))}</span><small>${escapeHtml(status(item))}</small>
    </button></li>`).join('')
    if (focusedItem) queue.querySelector<HTMLButtonElement>(`button[data-item="${focusedItem}"]`)?.focus({ preventScroll: true })
    renderCard(selectedItem())
    const groups = available ? groupsFor(batch.items) : new Map<string, Binding[]>()
    repeated.hidden = !groups.size
    repeated.innerHTML = groups.size ? `<header class="fm-section-header"><h2>${escapeHtml(tr('groups'))}</h2></header>
      <div class="fm-section-body">${[...groups].map(([key, items]) => {
      const first = batch.items.find(item => item.id === items[0].item_id)!
      const changes = first.proposals.filter(proposal => items[0].approved_ids.includes(proposal.id)).map(fieldsList).join('')
      return `<div class="batch-group">${changes}<button type="button" class="fm-btn fm-btn-outline" data-group="${escapeHtml(key)}">
        ${escapeHtml(tr('previewGroup', { count: items.length }))}</button></div>`
    }).join('')}</div>` : ''
    renderReport()
  }

  function renderPreview(current: Preview, changed = false) {
    const key = current.action
    dialogHeading.textContent = tr(key === 'apply' ? 'apply' : key === 'group' ? 'approveGroup' : 'cancel')
    dialogBody.innerHTML = 'items' in current ? approvalSummary(current.items)
      : `<p>${escapeHtml(tr('cancelExplanation'))}</p>`
    confirm.textContent = dialogHeading.textContent
    previewMessage.textContent = changed ? tr('groupChanged') : ''
  }

  function showPreview(current: Preview) {
    preview = current
    renderPreview(current)
    dialog.showModal()
  }

  function updateGroupPreview() {
    if (preview?.action !== 'group' || !dialog.open) return
    const items = batch.status === 'canceled' ? undefined : groupsFor(batch.items).get(preview.key)
    if (!items) {
      dialog.close()
      message.textContent = tr('groupUnavailable')
    } else if (JSON.stringify(items) !== JSON.stringify(preview.items)) {
      preview = { ...preview, items }
      renderPreview(preview, true)
      confirm.focus()
    }
  }

  function adopt(detail: Batch, from?: number) {
    batch = detail
    if (from != null || selectedId == null) selectedId = nextReview(from)
    render()
    updateGroupPreview()
  }

  function schedulePoll() {
    window.clearTimeout(timer)
    if (!accessLost && batch?.status === 'active' && batch.items.some(researching)) {
      timer = window.setTimeout(() => { if (!busy) void load() }, 2000)
    }
  }

  function loseAccess(error: ApiError) {
    if (dialog.open) dialog.close()
    accessLost = true
    const results = error.detail?.results
    message.textContent = tr(Array.isArray(results) ? 'applyAccessChanged' : 'accessChanged')
    if (!batch) return
    if (Array.isArray(results)) recordResults(results, batch)
    render()
  }

  async function load() {
    const loading = new AbortController()
    poll?.abort()
    poll = loading
    try {
      // A response started before a decision must not replace the newer detail.
      const detail = await api.get<Batch>(path, { signal: loading.signal, cache: 'no-store' })
      if (loading.signal.aborted) return
      const wasLoading = !batch
      const wasReadOnly = accessLost
      accessLost = false
      if (wasLoading || wasReadOnly || JSON.stringify(batch) !== JSON.stringify(detail)) adopt(detail)
      workspace.hidden = false
      if (wasLoading || wasReadOnly) message.textContent = ''
    } catch (error) {
      if (loading.signal.aborted || isAbortError(error)) return
      if (error instanceof ApiError && [401, 403].includes(error.status)) return loseAccess(error)
      message.textContent = tr('loadFailed')
      if (error instanceof ApiError && error.status === 404) return
    }
    if (!loading.signal.aborted) schedulePoll()
  }

  async function mutate(action: () => Promise<Batch>, from?: number) {
    if (busy || accessLost) return
    const applying = dialog.open && preview?.action === 'apply'
    busy = true
    workspace.inert = true
    confirm.disabled = true
    dialog.querySelector<HTMLButtonElement>('[data-action="close"]')!.disabled = true
    window.clearTimeout(timer)
    poll?.abort()
    message.textContent = ''
    let changed = false
    let saved = false
    try {
      const detail = await action()
      if (dialog.open) dialog.close()
      adopt(detail, from)
      saved = true
    } catch (error) {
      if (error instanceof ApiError && [401, 403].includes(error.status)) {
        loseAccess(error)
      } else if (!isAbortError(error)) {
        changed = error instanceof ApiError && error.status === 409
        if (changed && preview?.action !== 'group' && dialog.open) dialog.close()
        const status = dialog.open ? previewMessage : message
        status.textContent = tr(changed ? 'decisionChanged' : 'saveFailed')
      }
    } finally {
      busy = false
      workspace.inert = false
      confirm.disabled = false
      dialog.querySelector<HTMLButtonElement>('[data-action="close"]')!.disabled = false
    }
    if (applying && (saved || accessLost)) report.querySelector<HTMLElement>('h2')?.focus()
    else if (saved && from != null) card.querySelector<HTMLElement>('h2')?.focus()
    if (changed) await load()
    else schedulePoll()
  }

  function decide(item: Item, decision: Decision, approved: string[] = []) {
    void mutate(() => api.post<Batch>(path + '/decisions', {
      items: [{ ...binding(item, approved), decision }],
    }), decision === 'pending' ? undefined : item.id)
  }

  host.addEventListener('click', event => {
    if (busy || !batch) return
    const control = (event.target as HTMLElement).closest<HTMLButtonElement>('button')
    if (!control) return
    const { action, item: itemId, group } = control.dataset
    const item = selectedItem()
    if (itemId) return openItem(Number(itemId), true)
    if (group) {
      const items = groupsFor(batch.items).get(group)
      if (items) showPreview({ action: 'group', key: group, items })
    }
    if (action === 'apply') {
      const items = batch.items.filter(item => item.status === 'ready' && item.decision === 'approved')
        .map(item => binding(item, item.approved_ids))
      if (items.length) showPreview({ action: 'apply', items })
    }
    if (action === 'cancel') showPreview({ action: 'cancel' })
    if (action === 'close') dialog.close()
    if (action === 'back' && item) openItem(batch.items[batch.items.indexOf(item) - 1]?.id, true)
    if (action === 'skip' && item) decide(item, 'skipped')
    if (action === 'reopen' && item) decide(item, 'pending')
    if (action === 'approve' && item) {
      const approved = Array.from(card.querySelectorAll<HTMLInputElement>('[name="proposal"]:checked'), input => input.value)
      if (approved.length) decide(item, 'approved', approved)
    }
    if (action === 'confirm' && preview) {
      const current = preview
      if (current.action === 'cancel') void mutate(() => api.post<Batch>(path + '/cancel', {}))
      if (current.action === 'group') void mutate(() => api.post<Batch>(path + '/decisions', {
        items: current.items.map(item => ({ ...item, decision: 'approved' })),
      }), current.items[0].item_id)
      if (current.action === 'apply') void mutate(async () => {
        const result = await api.post<{ results: ApplyResult[]; batch: Batch | null }>(path + '/apply', { items: current.items })
        recordResults(result.results, result.batch ? undefined : batch)
        return result.batch ?? batch
      })
    }
  })
  card.addEventListener('change', updateApproveButton)
  card.addEventListener('submit', event => {
    event.preventDefault()
    const form = event.target as HTMLFormElement
    const item = selectedItem()
    if (!item) return
    const fields = new FormData(form)
    void mutate(() => api.post<Batch>(path + `/items/${item.id}/retry`, {
      revision: item.revision, decision_revision: item.decision_revision,
      sku_hint: String(fields.get('sku_hint')).trim() || undefined,
      product_url: String(fields.get('product_url')).trim() || undefined,
    }))
  })
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault() })
  window.addEventListener('pagehide', () => {
    window.clearTimeout(timer)
    poll?.abort()
  })
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return
    if (id) void load()
    else void loadHistory()
  })

  async function loadHistory() {
    try {
      const summaries = await api.get<Summary[]>(route, { cache: 'no-store' })
      history.hidden = false
      history.innerHTML = summaries.length ? `<ul class="batch-history">${summaries.map(summary => `
        <li class="batch-row">
          <a href="${escapeHtml(batchHref(summary.id))}">${escapeHtml(tr('mode.' + summary.mode))}</a>
          <span>${escapeHtml(new Date(summary.created_at).toLocaleString(document.documentElement.lang))}</span>
          <small>${escapeHtml(tr('historyState', { status: tr('status.' + summary.status), total: summary.total }))}</small>
        </li>`).join('')}</ul>` : `<p>${escapeHtml(tr('empty'))}</p>`
      message.textContent = ''
    } catch (error) {
      if (!isAbortError(error)) message.textContent = tr('loadFailed')
    }
  }

  if (id) void load()
  else void loadHistory()
}
