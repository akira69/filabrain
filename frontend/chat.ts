import { api, fetchAllPages } from './shared/api'
import { getLang, t } from './shared/i18n'
import { researchFieldLabel } from './shared/filabrain'
import { spoolProfileCards, type SpoolProfile } from './shared/spool-profiles'
import { mountModelPicker, type ModelSelection } from './shared/model-picker'
import { identity, type ResearchDraft } from './research'

const pageLabels = {
  new_filament: 'filaments.newFilament',
  new_spool: 'spools.newSpool',
  filaments: 'nav.filaments',
  research_batches: 'researchBatches.title',
  model_settings: 'filaBrain.settings',
}
type Action = { page: keyof typeof pageLabels; url: string }
type Evidence = {
  id: string; kind: string; title: string; url: string; text?: string; details?: Record<string, unknown>
  scope?: 'grade' | 'variant'
  links?: { title: string; url: string }[]
  remaining_g?: number | null; meets_required?: boolean | null; status?: string
}
type Grounding = {
  evidence: Evidence[]; notices: string[]; search: string | null; required_g: number | null
}
type Message = {
  role: 'user' | 'assistant'; content: string; action?: Action; proposal?: ResearchDraft['proposal']; savedDraftId?: string
  grounding?: Grounding; spoolProfiles?: SpoolProfile[]; selectedSpoolProfile?: string; confirmedSpoolProfile?: string
  activeSpoolChoice?: true; spoolLookupFilamentId?: number | null; spoolLookupId?: string
}
type Conversation = {
  id: string; title: string; messages: Message[]; research?: ResearchDraft
  researching?: true; model?: string | ModelSelection; updatedAt?: number
}
type Research = {
  start(): void
  reset(): void
  resume(draft: ResearchDraft): Promise<boolean>
  archive(proposal: ResearchDraft['proposal']): HTMLElement
  followUp(prompt: string): Promise<void>
}

function spoolLookupMessage(messages: Message[], selection: Message): Message {
  const profile = selection.confirmedSpoolProfile
  if (!profile) return selection
  if (selection.spoolLookupId) {
    return messages.find(message => message.spoolLookupId === selection.spoolLookupId && !message.confirmedSpoolProfile)
      ?? selection
  }
  // Older saved conversations only recorded the consecutive confirmations.
  let index = messages.indexOf(selection)
  while (index >= 3 && messages[index].confirmedSpoolProfile === profile) index -= 2
  const lookup = messages[index]
  return lookup?.spoolProfiles?.some(card => card.id === profile) ? lookup : selection
}

function chatHistoryWindow(messages: Message[], limit: number, spoolChoice?: Message) {
  const end = spoolChoice ? messages.indexOf(spoolChoice) + 1 : 0
  // Keep the original lookup and up to two refinements when the choice predates the chat window.
  const start = Math.max(0, end - 6)
  if (!end || start >= messages.length - limit) return messages.slice(-limit)
  const tailStart = Math.max(end, messages.length - (limit - (end - start)))
  return [...messages.slice(start, end), ...messages.slice(tailStart)]
}

const evidenceLabels: Record<string, string> = {
  spool: 'chatInventory', filament: 'chatSavedFilament', printer: 'chatSavedPrinter',
  saved_profile: 'chatSavedProfile', saved_calibration: 'chatSavedCalibration',
  preset: 'chatPreset', catalog: 'chatCatalog', live: 'chatLive',
}
const noticeLabels: Record<string, string> = {
  no_spools: 'chatNoSpools', spools_forbidden: 'chatSpoolsForbidden', printer_required: 'chatPrinterRequired',
  filament_required: 'chatFilamentRequired', nozzle_required: 'chatNozzleRequired',
  no_exact_profile: 'chatNoExactProfile', missing_evidence: 'chatMissingEvidence',
}
const searchLabels: Record<string, string> = {
  disabled: 'chatSearchDisabled', no_terms: 'chatSearchNoTerms', no_match: 'chatSearchNoMatch',
  failed: 'chatSearchFailed', complete: 'chatSearchComplete',
}
const profileFields: Record<string, string> = {
  nozzle_diameter_mm: 'nozzle_mm', nozzle_temp_c: 'extruder_temp_range_c',
  nozzle_temperature_c: 'extruder_temp_range_c', bed_temp_c: 'bed_temp_range_c',
  bed_temperature_c: 'bed_temp_range_c', net_weight_g: 'raw_material_weight_g',
  spool_weight_g: 'default_spool_weight_g',
  bambu_k_value: 'pressure_advance_k', bambu_bed_temp: 'bed_temp_range_c',
}


function evidenceValue(value: unknown): string {
  if (typeof value === 'number') return new Intl.NumberFormat(getLang(), { maximumFractionDigits: 6 }).format(value)
  if (typeof value === 'string' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map(evidenceValue).filter(Boolean).join(', ')
  if (!value || typeof value !== 'object') return ''
  if ('min' in value || 'max' in value) {
    const { min, max } = value as { min?: unknown; max?: unknown }
    return [...new Set([min, max].map(evidenceValue).filter(Boolean))].join(' – ')
  }
  return Object.entries(value).map(([key, item]) => `${researchFieldLabel(key)} · ${evidenceValue(item)}`).join('; ')
}

function evidenceHref(url: string) {
  if (/^\/(?:spools|filaments|printers)\/\d+$/.test(url)) return url
  if (!/^https?:\/\//i.test(url) || /[\s\\]/.test(url)) return ''
  try {
    const parsed = new URL(url)
    return parsed.username || parsed.password ? '' : parsed.href
  } catch { return '' }
}

function evidenceLink(title: string, url: string) {
  const href = evidenceHref(url)
  const element = href ? document.createElement('a') : document.createElement('strong')
  if (element instanceof HTMLAnchorElement) {
    element.href = href
    element.target = '_blank'
    element.rel = 'noopener noreferrer'
  }
  element.textContent = title
  return element
}

export function mountChat(statusModel: { chat_model?: string | null; chat_endpoint_id?: string | null; can_add_spool?: boolean; can_read_printers?: boolean }, openSettings?: () => void, editorScope?: string, researchOnly = editorScope !== undefined) {
  let model = statusModel.chat_model
  const form = document.querySelector<HTMLFormElement>('[data-chat-form]')!
  const input = form.querySelector('textarea')!
  const submit = form.querySelector<HTMLButtonElement>('.chat-send')!
  const webSearch = form.querySelector<HTMLButtonElement>('[data-chat-web-search]')!
  const contextPanel = form.querySelector<HTMLDetailsElement>('[data-chat-context]')!
  const printer = contextPanel.querySelector<HTMLSelectElement>('[data-chat-printer]')!
  const filament = contextPanel.querySelector<HTMLSelectElement>('[data-chat-filament]')!
  const filamentSearch = contextPanel.querySelector<HTMLInputElement>('[data-chat-filament-search]')!
  const nozzle = contextPanel.querySelector<HTMLSelectElement>('[data-chat-nozzle]')!
  const contextStatus = contextPanel.querySelector<HTMLElement>('[data-chat-context-status]')!
  const status = document.querySelector<HTMLElement>('[data-chat-status]')!
  const transcript = document.querySelector<HTMLElement>('[data-chat-transcript]')!
  const log = document.querySelector<HTMLElement>('[data-chat-log]')!
  const prompts = document.querySelector<HTMLElement>('[data-chat-prompts]')!
  const setup = document.querySelector<HTMLElement>('[data-chat-setup]')!
  const setupHint = document.querySelector<HTMLElement>('[data-chat-setup-hint]')!
  const setupButton = document.querySelector<HTMLButtonElement>('[data-chat-setup-button]')!
  const newChat = document.querySelector<HTMLButtonElement>('[data-chat-new]')!
  const history = document.querySelector<HTMLDetailsElement>('[data-chat-history]')!
  const historyList = document.querySelector<HTMLElement>('[data-chat-history-list]')!
  const researchInput = document.querySelector<HTMLElement>('[data-chat-research-input]')!
  const researchResults = document.querySelector<HTMLElement>('[data-chat-research-results]')!
  let current: Conversation = { id: '', title: '', messages: [] }
  let saved: Conversation[] = []
  let storageKey: string | undefined
  const historyStorage = () => editorScope === '' ? sessionStorage : localStorage
  let request: AbortController | undefined
  let busyResearch = false
  let staging = false
  let researching = false
  let research: Research | undefined
  let activeAnswer: HTMLElement | undefined
  let forkNextResearchTurn = false
  let evidenceCount = 0
  let filamentQuery = 0
  let queryTimer: number | undefined
  let printersLoaded = false
  const modelPicker = mountModelPicker(form, model, openSettings, statusModel.chat_endpoint_id, updateControls)
  const picker = form.querySelector<HTMLElement>('.chat-model-picker')!
  status.removeAttribute('data-i18n')
  status.textContent = ''
  submit.removeAttribute('data-i18n-aria-label')

  function scrollToEnd() { transcript.scrollTop = transcript.scrollHeight }

  function lastQuestion() {
    return current.messages.at(-2)?.content ?? ''
  }

  function updateControls() {
    form.hidden = (!researching && !modelPicker.hasOptions) || (editorScope !== undefined && !form.contains(picker))
    input.disabled = researching && !current.messages.length && !busyResearch
    input.maxLength = researching ? 400 : 2000
    input.placeholder = t(researching ? 'filaBrain.followUpHint' : 'filaBrain.chatPlaceholder')
    input.setAttribute('aria-label', t(researching ? 'filaBrain.followUp' : 'filaBrain.chatQuestion'))
    submit.disabled = busyResearch || staging || !request && (input.disabled || !input.value.trim())
    webSearch.hidden = contextPanel.hidden = researchOnly || researching
    webSearch.disabled = !!request || busyResearch || staging || !modelPicker.selected || (!input.value.trim() && !lastQuestion())
    for (const field of [printer, filament, filamentSearch, nozzle]) field.disabled = !!request
    const label = t(request ? 'filaBrain.chatStop' : researching ? 'filaBrain.sendFollowUp' : 'filaBrain.chatSend')
    submit.setAttribute('aria-label', label)
    submit.title = label
    submit.toggleAttribute('data-busy', !!request)
    input.readOnly = !!request || busyResearch || staging
    modelPicker.setBusy(!!request || busyResearch || staging)
    status.toggleAttribute('data-chat-running', !!request)
    newChat.disabled = busyResearch || staging || !researching && !request && !current.messages.length && !input.value
    for (const button of historyList.querySelectorAll('button')) button.disabled = busyResearch || staging
    prompts.hidden = researching || form.hidden || !!request || current.messages.length > 0
    setup.hidden = researching || modelPicker.hasOptions || current.messages.length > 0
    researchInput.hidden = researchResults.hidden = !researching
  }

  const setOptions = (select: HTMLSelectElement, options: [number, string][]) => {
    const selected = select.selectedOptions[0]
    const selectedId = Number(selected?.value)
    select.replaceChildren(new Option(t('filaBrain.chatNoSelection'), ''))
    if (selectedId && !options.some(([id]) => id === selectedId)) select.append(selected)
    for (const [id, name] of options) select.add(new Option(name, String(id)))
    if (selectedId) select.value = String(selectedId)
  }
  setOptions(printer, [])
  setOptions(filament, [])
  contextPanel.querySelector<HTMLElement>('[data-chat-printer-field]')!.hidden = !statusModel.can_read_printers
  contextPanel.addEventListener('toggle', async () => {
    if (!contextPanel.open || printersLoaded || !statusModel.can_read_printers) return
    printersLoaded = true
    try {
      const { items } = await fetchAllPages<{ id: number; name: string; is_active: boolean }>('/api/v1/printers')
      setOptions(printer, items.filter(item => item.is_active).map(item => [item.id, item.name]))
    } catch {
      printersLoaded = false
      contextStatus.textContent = t('filaBrain.chatContextUnavailable')
    }
  })
  filamentSearch.addEventListener('keydown', event => {
    if (event.key === 'Enter') event.preventDefault()
  })
  filamentSearch.addEventListener('input', () => {
    const generation = ++filamentQuery
    const search = filamentSearch.value.trim()
    window.clearTimeout(queryTimer)
    if (!search) {
      setOptions(filament, [])
      return
    }
    queryTimer = window.setTimeout(async () => {
      const query = new URLSearchParams({ search, page_size: '25' })
      try {
        const { items } = await api.get<{ items: { id: number; designation: string; manufacturer?: { name: string } }[] }>(
          `/filaments?${query}`, { cache: 'no-store' },
        )
        if (generation !== filamentQuery) return
        contextStatus.textContent = ''
        setOptions(filament, items.map(item => [item.id, [item.manufacturer?.name, item.designation].filter(Boolean).join(' · ')]))
      } catch {
        if (generation === filamentQuery) contextStatus.textContent = t('filaBrain.chatContextUnavailable')
      }
    }, 200)
  })

  function appendEvidence(paragraph: HTMLParagraphElement, content: string, grounding: Grounding) {
    const facts = document.createElement('details')
    facts.className = 'chat-evidence'
    facts.setAttribute('data-chat-evidence', '')
    facts.open = true
    const summary = document.createElement('summary')
    summary.textContent = t('filaBrain.chatEvidence')
    facts.append(summary)
    const note = (text: string, parent: HTMLElement = facts) => {
      const line = document.createElement('p')
      line.className = 'fm-field-hint'
      line.textContent = text
      parent.append(line)
    }
    if (typeof grounding.required_g === 'number') note(`${t('filaBrain.chatRequiredWeight')} · ${evidenceValue(grounding.required_g)} g`)
    for (const notice of grounding.notices) {
      if (Object.hasOwn(noticeLabels, notice)) note(t(`filaBrain.${noticeLabels[notice]}`))
    }
    if (grounding.search && Object.hasOwn(searchLabels, grounding.search)) note(t(`filaBrain.${searchLabels[grounding.search]}`))
    const list = document.createElement('ul')
    const anchors = new Map<string, string>()
    const turn = ++evidenceCount
    for (const [index, row] of grounding.evidence.entries()) {
      if (!row || typeof row.id !== 'string' || typeof row.title !== 'string' || typeof row.url !== 'string') continue
      const item = document.createElement('li')
      item.id = `chat-source-${turn}-${index}`
      anchors.set(row.id, item.id)
      const label = row.kind === 'live' && row.scope === 'grade'
        ? 'chatLiveGrade' : Object.hasOwn(evidenceLabels, row.kind) ? evidenceLabels[row.kind] : 'chatGuidance'
      note(t(`filaBrain.${label}`), item)
      item.append(evidenceLink(row.title, row.url))
      if (row.kind === 'spool') {
        const weight = typeof row.remaining_g === 'number'
          ? `${evidenceValue(row.remaining_g)} g` : t('filaBrain.chatUnknownWeight')
        const threshold = typeof row.meets_required === 'boolean'
          ? t(row.meets_required ? 'filaBrain.chatMeetsWeight' : 'filaBrain.chatBelowWeight') : ''
        note([row.status, weight, threshold].filter(Boolean).join(' · '), item)
      }
      if (row.text) note(row.text, item)
      for (const [key, value] of Object.entries(row.details ?? {})) {
        const detail = evidenceValue(value)
        if (!detail) continue
        const field = profileFields[key] ?? key
        const label = field === 'nozzle_mm' ? t('filaBrain.chatNozzle') : researchFieldLabel(field)
        note(`${label} · ${detail}${field === 'nozzle_mm' ? ' mm' : ''}`, item)
      }
      if (Array.isArray(row.links)) {
        for (const link of row.links) {
          if (typeof link?.title !== 'string' || typeof link.url !== 'string') continue
          const line = document.createElement('p')
          line.append(evidenceLink(link.title, link.url))
          item.append(line)
        }
      }
      list.append(item)
    }
    if (list.childElementCount) facts.append(list)
    paragraph.replaceChildren()
    let offset = 0
    for (const match of content.matchAll(/\[([^\[\]\r\n]{1,128})\]/g)) {
      const id = anchors.get(match[1])
      if (!id) continue
      paragraph.append(content.slice(offset, match.index))
      const citation = document.createElement('a')
      citation.href = '#' + id
      citation.textContent = match[0]
      citation.addEventListener('click', () => { facts.open = true })
      paragraph.append(citation)
      offset = match.index! + match[0].length
    }
    paragraph.append(content.slice(offset))
    log.append(facts)
  }

  function append(message: Message) {
    const paragraph = document.createElement('p')
    paragraph.className = `chat-message chat-${message.role}`
    paragraph.textContent = message.content
    log.append(paragraph)
    if (message.role === 'assistant' && Array.isArray(message.grounding?.evidence) && Array.isArray(message.grounding.notices)) {
      appendEvidence(paragraph, message.content, message.grounding)
    }
    if (message.role === 'assistant' && Array.isArray(message.spoolProfiles)) {
      const cards = spoolProfileCards(message.spoolProfiles, message.selectedSpoolProfile, profile => {
        const activeChoice = current.messages.find(item => item.activeSpoolChoice)
        if (activeChoice) activeChoice.activeSpoolChoice = undefined
        const lookup = spoolLookupMessage(current.messages, message)
        lookup.activeSpoolChoice = true
        lookup.selectedSpoolProfile = message.selectedSpoolProfile = profile?.id
        remember()
      })
      if (cards) log.append(cards)
    }
    if (message.action && Object.hasOwn(pageLabels, message.action.page)) {
      const { page, url } = message.action
      if (typeof url === 'string' && url.startsWith('/') && !url.startsWith('//') && !url.includes('\\')) {
        const link = document.createElement('a')
        link.className = 'fm-btn fm-btn-outline chat-action'
        link.href = page === 'research_batches' ? '?tab=batches' : page === 'model_settings' ? '?tab=settings' : url
        if (!link.getAttribute('href')?.startsWith('?')) link.target = '_blank'
        link.rel = 'noopener noreferrer'
        link.textContent = t(pageLabels[page])
        log.append(link)
      }
    }
    const savedDraftId = message.savedDraftId
    if (typeof savedDraftId === 'string' && /^[a-zA-Z0-9-]{1,64}$/.test(savedDraftId)) {
      const link = document.createElement('a')
      link.className = 'chat-action'
      link.href = `/api/v1/filabrain/saved-filaments/${savedDraftId}`
      link.target = '_blank'
      link.rel = 'noopener noreferrer'
      link.textContent = t('filaBrain.openSavedFilament')
      log.append(link)
      if (statusModel.can_add_spool && editorScope === undefined) {
        const spool = link.cloneNode() as HTMLAnchorElement
        spool.href += '?add_spool=1'
        spool.textContent = t('spoolIntake.title')
        log.append(spool)
      }
    }
    if (message.grounding || message.spoolProfiles?.length) transcript.scrollTop = paragraph.offsetTop - 8
    else scrollToEnd()
    return paragraph
  }

  function placeResearchResults(answer: HTMLElement) {
    answer.after(researchResults)
    activeAnswer = answer
  }

  function renderHistory() {
    historyList.replaceChildren()
    const conversations = saved.filter(item => item.researching || item.research ? research : !researchOnly)
    if (!conversations.length) {
      const empty = document.createElement('p')
      empty.className = 'fm-field-hint'
      empty.textContent = t('filaBrain.chatHistoryEmpty')
      historyList.append(empty)
    }
    for (const conversation of conversations) {
      const button = document.createElement('button')
      button.type = 'button'
      button.disabled = busyResearch || staging
      button.textContent = conversation.title || conversation.messages[0].content.slice(0, 100)
      if (conversation.id === current.id) button.setAttribute('aria-current', 'true')
      button.addEventListener('click', async () => {
        if (busyResearch || staging) return
        let selected = conversation
        try {
          const latest = readHistory().find(item => item.id === conversation.id)
          if (latest) selected = mergeConversation(conversation, latest)
        } catch { /* Keep the in-memory transcript available. */ }
        const researchChat = !!(selected.researching || selected.research)
        reset(researchChat)
        current = { ...selected, messages: [...selected.messages] }
        let lastResult = -1
        current.messages.forEach((message, index) => { if (message.proposal) lastResult = index })
        if (selected.research) {
          if (lastResult === -1) lastResult = current.messages.length - 1
          current.messages[lastResult] = { ...current.messages[lastResult], proposal: selected.research.proposal }
        }
        modelPicker.reset(selected.model, true)
        busyResearch = true
        updateControls()
        try {
          if (researchChat) research?.start()
          const draft = current.research
          let resumed = false
          if (draft && research) {
            try { resumed = await research.resume(draft) }
            catch { status.textContent = t('filaBrain.researchHistoryReadOnly') }
          }
          if (!resumed) current.research = undefined
          current.messages.forEach((message, index) => {
            const bubble = append(message)
            if (draft && index === lastResult) {
              if (resumed) {
                placeResearchResults(bubble)
                if (draft.staged && research) {
                  bubble.after(research.archive(draft.proposal))
                  activeAnswer = undefined
                }
              }
              else {
                bubble.after(researchResults)
                if (research) bubble.after(research.archive(draft.proposal))
              }
            } else if (message.proposal && research) {
              bubble.after(research.archive(message.proposal))
            }
          })
        } catch {
          status.textContent = t('filaBrain.failed')
        } finally {
          busyResearch = false
          renderHistory()
          updateControls()
          if (researchChat) {
            const lastQuestion = log.querySelectorAll<HTMLElement>('.chat-user')
            transcript.scrollTop = (lastQuestion[lastQuestion.length - 1]?.offsetTop ?? 0) - 8
          } else scrollToEnd()
          history.querySelector('summary')?.focus()
        }
      })
      historyList.append(button)
    }
  }

  function readHistory(): Conversation[] {
    if (!storageKey) return []
    const stored: unknown = JSON.parse(historyStorage().getItem(storageKey) || '[]')
    return Array.isArray(stored) ? stored.filter(item => typeof item?.id === 'string' &&
      Array.isArray(item.messages) && item.messages.length && item.messages.every(
        (message: Message) => (message?.role === 'user' || message?.role === 'assistant') &&
          typeof message.content === 'string')) : []
  }

  function hasReceipt(conversation: Conversation, draftId: string) {
    return conversation.messages.some(message => message.savedDraftId === draftId)
  }

  function hasStoredReceipt(id: string) {
    try {
      return readHistory().some(item => item.id === current.id && hasReceipt(item, id))
    } catch {
      return false
    }
  }

  function mergeConversation(local: Conversation, stored: Conversation): Conversation {
    const localDraftSaved = local.research && hasReceipt(stored, local.research.saveId ?? local.id)
    const storedDraftSaved = stored.research && hasReceipt(local, stored.research.saveId ?? stored.id)
    const useStored = localDraftSaved || (!storedDraftSaved && lastUpdated(stored) >= lastUpdated(local))
    const [latest, other] = useStored ? [stored, local] : [local, stored]
    const receipts = other.messages.filter(message => message.savedDraftId && !hasReceipt(latest, message.savedDraftId))
    const research = useStored ? (storedDraftSaved ? undefined : stored.research) : local.research
    return { ...latest, research, messages: [...latest.messages, ...receipts] }
  }

  function draftId() {
    return current.research?.saveId ?? current.id
  }

  function newId() {
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
  }

  function remember() {
    if (!current.id) {
      current.id = newId()
      current.title = current.messages[0].content.slice(0, 100)
    }
    const selection = current.messages.find(message => message.activeSpoolChoice) ?? current.messages.at(-1)
    const lookup = selection?.selectedSpoolProfile ? spoolLookupMessage(current.messages, selection) : undefined
    const messages = chatHistoryWindow(current.messages, 60, lookup)
    const earlierReceipts = current.messages.slice(0, -60).filter(message => message.savedDraftId && !messages.includes(message))
    messages.unshift(...earlierReceipts)
    const snapshot = { ...current, model: modelPicker.selected, updatedAt: Date.now(), messages }
    saved = [snapshot, ...saved.filter(item => item.id !== snapshot.id)].slice(0, 20)
    if (storageKey) {
      try {
        const byId = new Map<string, Conversation>()
        for (const item of [...saved, ...readHistory()]) {
          const previous = byId.get(item.id)
          byId.set(item.id, previous ? mergeConversation(previous, item) : item)
        }
        saved = [...byId.values()].sort((a, b) => lastUpdated(b) - lastUpdated(a)).slice(0, 20)
        historyStorage().setItem(storageKey, JSON.stringify(saved))
      } catch { status.textContent = t('filaBrain.chatHistoryFailed') }
    }
    renderHistory()
  }

  function lastUpdated(item: Conversation) {
    return item.updatedAt ?? (Number(item.id.split('-')[0]) || 0)
  }

  void api.get<{ id: number }>('/me', { cache: 'no-store' }).then(({ id }) => {
    if (!Number.isSafeInteger(id) || id < 1) return
    storageKey = `filabrain:chat:${id}${editorScope === '' ? ':editor:new' : editorScope ? ':' + editorScope : ''}`
    try { saved = [...saved, ...readHistory()].slice(0, 20) }
    catch { /* The current conversation remains available in memory. */ }
    if (current.messages.length) remember()
    renderHistory()
    history.hidden = false
  }).catch(() => {})

  document.addEventListener('click', event => {
    if (event.target instanceof Node && !history.contains(event.target)) history.open = false
  })
  history.addEventListener('keydown', event => {
    if (event.key === 'Escape' && history.open) {
      history.open = false
      history.querySelector('summary')?.focus()
    }
  })

  function reset(useResearch: boolean) {
    request?.abort()
    request = undefined
    current = { id: '', title: '', messages: [] }
    forkNextResearchTurn = false
    researching = useResearch
    research?.reset()
    document.dispatchEvent(new CustomEvent('filabrain:chat-mode', { detail: useResearch }))
    modelPicker.reset()
    log.after(researchResults)
    activeAnswer = undefined
    log.replaceChildren()
    input.value = ''
    contextPanel.open = false
    filamentQuery++
    filamentSearch.value = ''
    filament.value = printer.value = nozzle.value = ''
    setOptions(filament, [])
    contextStatus.textContent = ''
    status.textContent = ''
    history.open = false
    renderHistory()
    updateControls()
  }

  input.addEventListener('input', updateControls)
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !request && !busyResearch) {
      event.preventDefault()
      form.requestSubmit()
    }
  })
  for (const button of prompts.querySelectorAll<HTMLButtonElement>('[data-chat-prompt]')) {
    button.addEventListener('click', () => {
      if (button.dataset.chatPrompt === 'chatPromptAdd' && research) {
        research.start()
        return
      }
      input.value = t(`filaBrain.${button.dataset.chatPrompt}`)
      updateControls()
      input.focus()
    })
  }
  setupButton.hidden = !openSettings
  if (openSettings) setupButton.addEventListener('click', openSettings)
  setupHint.textContent = t(openSettings ? 'filaBrain.chatSetup' : 'filaBrain.chatAskAdmin')
  newChat.addEventListener('click', () => {
    reset(researchOnly)
    if (researching) researchInput.querySelector<HTMLElement>('input, select')?.focus()
    else input.focus()
  })
  form.addEventListener('submit', async event => {
    event.preventDefault()
    if (request) {
      request.abort()
      return
    }
    const searchWeb = event.submitter === webSearch && !researching
    const content = input.value.trim() || (searchWeb ? lastQuestion() : '')
    if (!content || busyResearch || staging) return
    if (researching) {
      try { await research?.followUp(content) }
      catch { status.textContent = t('filaBrain.failed') }
      return
    }
    if (!modelPicker.selected) {
      status.textContent = t('filaBrain.chooseModel')
      return
    }
    const question: Message = { role: 'user', content }
    modelPicker.preserve(modelPicker.selected)
    const bubble = append(question)
    const controller = request = new AbortController()
    input.value = ''
    status.textContent = t('filaBrain.chatWorking')
    updateControls()
    try {
      const choice = current.messages.find(message => message.activeSpoolChoice)
      const selection = choice ?? current.messages.at(-1)
      const selectedProfileId = selection?.selectedSpoolProfile
      const lookup = selection && selectedProfileId ? spoolLookupMessage(current.messages, selection) : undefined
      const lookupFilamentId = lookup?.spoolLookupFilamentId
      const filamentId = filament.value ? Number(filament.value) : null
      const history = chatHistoryWindow(current.messages, 10, lookup)
      const lookupMessageIndex = lookup ? history.indexOf(lookup) : -1
      const { reply, action, spool_profiles, selected_spool_profile_id, ...grounding } = await api.post<{
        reply: string; action: Action | null; spool_profiles?: SpoolProfile[]; selected_spool_profile_id?: string
      } & Partial<Grounding>>('/filabrain/chat', {
        messages: [...history, question].map(({ role, content, confirmedSpoolProfile }) => ({
          role, content, ...(confirmedSpoolProfile ? { confirmed_spool_profile_id: confirmedSpoolProfile } : {}),
        })),
        ...modelPicker.selected,
        ...(typeof selectedProfileId === 'string' ? { selected_spool_profile_id: selectedProfileId } : {}),
        ...(lookupMessageIndex >= 0 ? { spool_lookup_message_index: lookupMessageIndex } : {}),
        ...(lookupFilamentId !== undefined ? { spool_lookup_filament_id: lookupFilamentId } : {}),
        ...(searchWeb ? { web_search: true } : {}),
        ...(printer.value ? { printer_id: Number(printer.value) } : {}),
        ...(filamentId !== null ? { filament_id: filamentId } : {}),
        ...(nozzle.value ? { nozzle_mm: Number(nozzle.value) } : {}),
      }, { signal: controller.signal })
      if (request !== controller) return
      if (choice?.activeSpoolChoice && choice.selectedSpoolProfile === selectedProfileId &&
          (selectedProfileId === undefined || selected_spool_profile_id !== selectedProfileId)) {
        choice.activeSpoolChoice = undefined
      }
      const spoolProfiles = Array.isArray(spool_profiles) ? spool_profiles.slice(0, 4) : undefined
      if (selected_spool_profile_id && lookup && !lookup.spoolLookupId) lookup.spoolLookupId = newId()
      const answer: Message = {
        role: 'assistant', content: reply, action: action ?? undefined,
        grounding: Array.isArray(grounding.evidence) ? grounding as Grounding : undefined,
        spoolProfiles,
        spoolLookupFilamentId: spoolProfiles?.length
          ? (selected_spool_profile_id && lookupFilamentId !== undefined ? lookupFilamentId : filamentId)
          : undefined,
        selectedSpoolProfile: selected_spool_profile_id,
        confirmedSpoolProfile: selected_spool_profile_id,
        spoolLookupId: spoolProfiles?.length ? (selected_spool_profile_id && lookup?.spoolLookupId || newId()) : undefined,
      }
      current.messages.push(question, answer)
      append(answer)
      status.textContent = ''
      remember()
    } catch {
      if (request !== controller) return
      bubble.remove()
      input.value = content
      status.textContent = t(controller.signal.aborted ? 'filaBrain.chatStopped' : 'filaBrain.chatFailed')
    } finally {
      if (request === controller) {
        request = undefined
        updateControls()
        input.focus()
      }
    }
  })

  document.addEventListener('filaman:model-change', event => {
    const detail = (event as CustomEvent<typeof statusModel>).detail
    model = detail.chat_model
    void modelPicker.setDefault(model, detail.chat_endpoint_id)
    updateControls()
  })
  function startResearch(fresh = false) {
    if (busyResearch || staging) return false
    if (fresh || !researching && (current.messages.length || input.value || request)) reset(true)
    else if (!researching) {
      researching = true
      document.dispatchEvent(new CustomEvent('filabrain:chat-mode', { detail: true }))
    }
    updateControls()
    const field = researchInput.querySelector<HTMLElement>('input[name="designation"]:not([type="hidden"])')
    if (field?.getClientRects().length) field.focus()
    return true
  }

  updateControls()
  return {
    get model() { return modelPicker.selected },
    get needsModelSelection() { return modelPicker.needsSelection },
    preserveModel() { modelPicker.preserve(modelPicker.selected) },
    picker,
    startResearch,
    get draftId() { return draftId() },
    get savedDraft() {
      const id = draftId()
      return hasReceipt(current, id) || hasStoredReceipt(id)
    },
    setStaging(value: boolean) {
      staging = value
      updateControls()
    },
    archiveResearch() {
      if (!current.research || !research) return
      activeAnswer?.after(research.archive(current.research.proposal))
      current.research = { ...current.research, staged: true, selection: [] }
      activeAnswer = undefined
      remember()
    },
    completeResearch(id: string, alreadySaved = false) {
      if (draftId() !== id || hasReceipt(current, id)) return
      if (current.research && activeAnswer && research) {
        activeAnswer.after(research.archive(current.research.proposal))
      }
      current.research = undefined
      activeAnswer = undefined
      const message: Message = { role: 'assistant', content: t(alreadySaved ? 'filaBrain.draftAlreadySaved' : 'filaBrain.filamentSaved'), savedDraftId: id }
      current.messages.push(message)
      append(message)
      // The stale tab can show its confirmation without replacing the newer draft in History.
      forkNextResearchTurn = hasStoredReceipt(id)
      if (!forkNextResearchTurn) remember()
    },
    saveSelection(selection: ResearchDraft['selection']) {
      if (!current.research) return
      current.research = { ...current.research, selection }
      remember()
    },
    setResearch(value: Research) { research = value; renderHistory() },
    showChat() {
      if (busyResearch) {
        document.dispatchEvent(new CustomEvent('filabrain:chat-mode', { detail: researching }))
      } else if (researching) reset(false)
    },
    async researchTurn(content: string, run: () => Promise<{ reply: string; draft?: ResearchDraft }>) {
      if (busyResearch) return false
      if (forkNextResearchTurn) {
        current.id = newId()
        current.title = content.slice(0, 100)
        forkNextResearchTurn = false
      }
      const question: Message = { role: 'user', content }
      const questionBubble = append(question)
      const note = append({ role: 'assistant', content: t('filaBrain.searching') })
      note.className = 'chat-message research-note'
      note.setAttribute('data-chat-running', '')
      busyResearch = true
      status.textContent = ''
      updateControls()
      try {
        const { reply, draft } = await run()
        note.textContent = reply
        note.className = 'chat-message chat-assistant'
        const previous = current.research
        if (draft && previous && activeAnswer && research) activeAnswer.after(research.archive(previous.proposal))
        current.messages.push(question, { role: 'assistant', content: reply, proposal: draft?.proposal })
        if (draft) {
          const sameProduct = previous && previous.fields.product_url === draft.fields.product_url &&
            previous.fields.sku_hint === draft.fields.sku_hint &&
            previous.context.fields.manufacturer_id === draft.context.fields.manufacturer_id &&
            previous.context.fields.color_id === draft.context.fields.color_id &&
            JSON.stringify(identity(previous.context.fields)) === JSON.stringify(identity(draft.context.fields))
          const saveId = sameProduct ? (previous.saveId ?? current.id) : newId()
          current.research = { ...draft, saveId }
          placeResearchResults(note)
          if (input.value.trim() === content) input.value = ''
        }
        current.researching = true
        remember()
        return true
      } finally {
        note.removeAttribute('data-chat-running')
        busyResearch = false
        updateControls()
        transcript.scrollTop = questionBubble.offsetTop - 8
      }
    },
  }
}

export type Chat = ReturnType<typeof mountChat>
