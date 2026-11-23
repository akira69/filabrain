import { api, ApiError } from './shared/api'
import { getLang, t } from './shared/i18n'
import { mountAgentTasks, type AgentTask } from './agent-tasks'
import { colorContextNote, colorReview, type ColorHost } from './color-assistance'
import type { SpoolResearchInput } from './spool-intake'
import { productCandidate, researchIssueText, researchFieldLabel, type ResearchProposal } from './shared/filabrain'
import { spoolProfileCards, type SpoolProfile } from './shared/spool-profiles'
import { mountModelPicker, type ModelSelection } from './shared/model-picker'
import { identity, type ResearchDraft } from './research'
import { mountDictation, type DictationSettings } from './dictation'
import { mountChatContext, type ChatContextSnapshot } from './chat-context'
import { mountContextUsage, type ChatUsage } from './context-usage'

const pageLabels = {
  new_filament: 'filaments.newFilament',
  new_spool: 'spools.newSpool',
  filaments: 'nav.filaments',
  research_batches: 'researchBatches.title',
  model_settings: 'filaBrain.settings',
}
type Action = { page: keyof typeof pageLabels; url: string } | { page: 'intake' | 'color'; proposal?: ResearchProposal }
type Evidence = {
  id: string; kind: string; title: string; url: string; text?: string; details?: Record<string, unknown>
  scope?: 'grade' | 'variant'
  links?: { title: string; url: string }[]
  remaining_g?: number | null; meets_required?: boolean | null; status?: string
}
type Grounding = {
  evidence: Evidence[]; notices: string[]; search: string | null; required_g: number | null
}
type Coverage = { records_checked?: number; matching_records?: number; page?: number; has_more?: boolean; exhaustive?: boolean; candidates_shown?: number; unverified_source_filament_ids?: number[]; unchecked_source_filament_ids?: number[] }
type Message = {
  role: 'user' | 'assistant'; content: string; action?: Action; proposal?: ResearchDraft['proposal']; savedDraftId?: string
  grounding?: Grounding; spoolProfiles?: SpoolProfile[]; selectedSpoolProfile?: string; confirmedSpoolProfile?: string
  activeSpoolChoice?: true; spoolLookupFilamentId?: number | null; spoolLookupId?: string
  report?: { task: AgentTask; query: string; coverage?: Coverage; complete?: boolean }
  usage?: ChatUsage
  usageModel?: ModelSelection
}
type Conversation = {
  id: string; title: string; messages: Message[]; research?: ResearchDraft
  researching?: true; model?: string | ModelSelection; updatedAt?: number
  draft?: string; context?: ChatContextSnapshot; task?: AgentTask
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
  health: 'taskHealth', stock: 'taskStock', color: 'color', preset: 'chatPreset', catalog: 'chatCatalog', live: 'chatLive',
}
const noticeLabels: Record<string, string> = {
  product_lookup_incomplete: 'taskProductIncomplete', package_weight_unverified: 'taskPackageUnknown', color_lookup_incomplete: 'taskColorIncomplete', spool_lookup_incomplete: 'taskSpoolIncomplete', no_spools: 'chatNoSpools', spools_forbidden: 'chatSpoolsForbidden', printer_required: 'chatPrinterRequired',
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

export function mountChat(statusModel: { chat_model?: string | null; chat_endpoint_id?: string | null; can_add_spool?: boolean; can_read_printers?: boolean; can_read_spools?: boolean; can_read_display?: boolean; microphone_enabled?: boolean } & DictationSettings, openSettings?: () => void, editorScope?: string, researchOnly = editorScope !== undefined, options: { root?: HTMLElement; openResearch?: (input?: SpoolResearchInput) => void; context?: ChatContextSnapshot; colorHost?: ColorHost } = {}) {
  const root = options.root ?? document.querySelector<HTMLElement>('[data-view="chat"]')!
  let model = statusModel.chat_model
  const form = root.querySelector<HTMLFormElement>('[data-chat-form]')!
  const input = form.querySelector('textarea')!
  const dictation = mountDictation(form, input, statusModel)
  const usageMeter = mountContextUsage(form.querySelector<HTMLElement>('[data-chat-usage]')!)
  const chatContext = mountChatContext(form, statusModel, !researchOnly)
  let contextReady = true
  let contextInitialization = Promise.resolve()
  function restoreChatContext(saved?: ChatContextSnapshot) {
    const pending = saved ? chatContext.restore(saved) : chatContext.reset()
    contextReady = false
    contextInitialization = pending
    const ready = () => { if (contextInitialization === pending) { contextReady = true; updateControls() } }
    void pending.then(ready, ready)
    return pending
  }
  if (options.context) void restoreChatContext(options.context)
  const submit = form.querySelector<HTMLButtonElement>('.chat-send')!
  const contextPanel = form.querySelector<HTMLDetailsElement>('[data-chat-context]')!
  const printer = contextPanel.querySelector<HTMLSelectElement>('[data-chat-printer]')!
  const filament = contextPanel.querySelector<HTMLSelectElement>('[data-chat-filament]')!
  const filamentSearch = contextPanel.querySelector<HTMLInputElement>('[data-chat-filament-search]')!
  const nozzle = contextPanel.querySelector<HTMLSelectElement>('[data-chat-nozzle]')!
  const contextStatus = contextPanel.querySelector<HTMLElement>('[data-chat-context-status]')!
  const status = root.querySelector<HTMLElement>('[data-chat-status]')!
  const transcript = root.querySelector<HTMLElement>('[data-chat-transcript]')!
  const log = root.querySelector<HTMLElement>('[data-chat-log]')!
  const prompts = root.querySelector<HTMLElement>('[data-chat-prompts]')!
  const setup = root.querySelector<HTMLElement>('[data-chat-setup]')!
  const setupHint = root.querySelector<HTMLElement>('[data-chat-setup-hint]')!
  const setupButton = root.querySelector<HTMLButtonElement>('[data-chat-setup-button]')!
  const newChat = root.querySelector<HTMLButtonElement>('[data-chat-new]')!
  const history = root.querySelector<HTMLDetailsElement>('[data-chat-history]')!
  const historyList = root.querySelector<HTMLElement>('[data-chat-history-list]')!
  const researchInput = root.querySelector<HTMLElement>('[data-chat-research-input]')!
  const researchResults = root.querySelector<HTMLElement>('[data-chat-research-results]')!
  const bubbles = new Map<Message, HTMLElement>()
  let current: Conversation = { id: '', title: '', messages: [] }
  let saved: Conversation[] = []
  let storageKey: string | undefined
  const historyStorage = () => editorScope === '' ? sessionStorage : localStorage
  let request: AbortController | undefined
  const pending = new Map<string, { conversation: Conversation; controller: AbortController; question: Message; draft: string }>()
  let busyResearch = false
  let staging = false
  let researching = false
  let research: Research | undefined
  let activeAnswer: HTMLElement | undefined
  let forkNextResearchTurn = false
  let evidenceCount = 0
  let intakeChoice: { product_url: string; sku_hint?: string; identity: ResearchProposal['target']['identity'] } | undefined
  const modelPicker = mountModelPicker(form, model, openSettings, statusModel.chat_endpoint_id, updateControls)
  const agent = mountAgentTasks(form, !!statusModel.can_read_spools, updateControls, !!options.colorHost)
  if (options.colorHost) {
    form.before(colorContextNote(options.colorHost))
    input.value = options.colorHost.context.fields.name
  }
  const picker = form.querySelector<HTMLElement>('.chat-model-picker')!
  status.removeAttribute('data-i18n')
  status.textContent = ''
  submit.removeAttribute('data-i18n-aria-label')

  function scrollToEnd() { transcript.scrollTop = transcript.scrollHeight }

  function updateControls() {
    form.hidden = (researchOnly && !researching && !modelPicker.hasOptions) || (editorScope !== undefined && !form.contains(picker))
    const taskActive = !researchOnly && !researching && agent.active
    agent.update(!!request || busyResearch || staging, researchOnly || researching)
    picker.hidden = taskActive
    input.hidden = !researching && !taskActive && !modelPicker.hasOptions
    form.querySelector<HTMLElement>('.chat-toolbar')!.hidden = input.hidden
    input.disabled = !researching && !taskActive && !modelPicker.hasOptions || researching && !current.messages.length && !busyResearch
    input.maxLength = researching ? 400 : taskActive ? 2048 : 2000
    input.placeholder = t(researching ? 'filaBrain.followUpHint' : taskActive ? agent.value === 'color' ? 'colorAssistant.hint' : agent.value === 'intake' ? 'filaBrain.taskIntakeHint' : agent.value === 'job-fit' ? 'filaBrain.taskJobHint' : 'filaBrain.taskFilterHint' : 'filaBrain.chatPlaceholder')
    input.setAttribute('aria-label', t(researching ? 'filaBrain.followUp' : 'filaBrain.chatQuestion'))
    submit.disabled = !contextReady || busyResearch || staging || !request && (input.disabled || !input.value.trim() && !agent.allowsEmpty)
    contextPanel.hidden = researchOnly || researching || input.hidden || taskActive && agent.value !== 'job-fit'
    for (const field of [printer, filament, filamentSearch, nozzle]) field.disabled = !!request
    const label = t(request ? 'filaBrain.chatStop' : researching ? 'filaBrain.sendFollowUp' : 'filaBrain.chatSend')
    submit.setAttribute('aria-label', label)
    submit.title = label
    submit.toggleAttribute('data-busy', !!request)
    input.readOnly = !!request || busyResearch || staging
    dictation.setBusy(input.readOnly)
    const answer = current.messages.at(-1)
    const usedModel = answer?.usageModel
    usageMeter.update(answer?.usage, { visible: !!usedModel && answer?.role === 'assistant' && !researching
      && usedModel?.model_id === modelPicker.selected?.model_id && usedModel?.endpoint_id === modelPicker.selected?.endpoint_id,
      model: usedModel?.model_id })
    const title = current.title || t('filaBrain.chatNew')
    history.querySelector<HTMLElement>('[data-chat-title]')!.textContent = title
    history.querySelector('summary')!.setAttribute('aria-label', t('filaBrain.chatConversation', { name: title }))
    history.querySelector('summary')!.title = title
    modelPicker.setBusy(!!request || busyResearch || staging)
    status.toggleAttribute('data-chat-running', !!request)
    newChat.disabled = busyResearch || staging || !researching && !request && !current.messages.length && !input.value
    for (const button of historyList.querySelectorAll('button')) button.disabled = busyResearch || staging
    prompts.hidden = researching || taskActive || !modelPicker.hasOptions || form.hidden || !!request || current.messages.length > 0
    setup.hidden = researching || taskActive || modelPicker.hasOptions || current.messages.length > 0
    researchInput.hidden = researchResults.hidden = !researching
  }

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
    if (facts.childElementCount === 1) return
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
    if (message.report) {
      const report = message.report
      const coverage = report.coverage
      const line = document.createElement('p')
      line.className = 'fm-field-hint chat-report'
      if (typeof coverage?.records_checked === 'number') line.textContent = t('filaBrain.taskCoverage', {
        page: coverage.page || 1, count: coverage.records_checked, total: coverage.matching_records ?? coverage.records_checked,
      })
      if (coverage?.exhaustive === false) line.textContent += t('filaBrain.taskRankedCandidates')
      if (report.complete === false) line.textContent += ' ' + t('filaBrain.taskPartial')
      log.append(line)
      if (coverage?.has_more && (report.task.task === 'stock' || report.task.task === 'library-health')) {
        const next = document.createElement('button')
        next.type = 'button'
        next.className = 'fm-btn fm-btn-outline chat-action'
        next.textContent = t('filaBrain.taskNextPage')
        next.addEventListener('click', () => {
          if (request) return
          agent.restore({ ...report.task, page: (coverage.page || 1) + 1 })
          input.value = report.query
          updateControls()
          form.requestSubmit()
        })
        log.append(next)
      }
    }
    if ((message.action?.page === 'intake' || message.action?.page === 'color') && message.action.proposal) {
      const proposal = message.action.proposal
      const candidates = proposal.issues.filter(issue => issue.code === 'possible_match')
      for (const issue of candidates) log.append(productCandidate(issue, (url, sku) => {
        if (request) return
        agent.restore({ task: message.action!.page === 'color' ? 'color' : 'intake', grams: '', checkSources: false })
        input.value = url
        intakeChoice = { product_url: url, sku_hint: sku || proposal.discovery_sku, identity: proposal.target.identity }
        updateControls()
        form.requestSubmit()
      }))
      const sourceIssues = proposal.issues.filter(issue => ['unavailable_source', 'search_incomplete', 'no_match', 'ambiguous_match', 'duplicate'].includes(issue.code))
      if (sourceIssues.length) {
        const notes = document.createElement('ul')
        notes.className = 'fm-field-hint chat-research-notes'
        for (const issue of sourceIssues) {
          const note = document.createElement('li')
          note.textContent = researchIssueText(issue)
          if (issue.code === 'duplicate' && issue.filament_id) note.append(' ', evidenceLink(`#${issue.filament_id}`, `/filaments/${issue.filament_id}`))
          notes.append(note)
        }
        log.append(notes)
      }
      if (message.action.page === 'color' && !candidates.length && options.colorHost && proposal.color_previews?.length) {
        log.append(colorReview(proposal.color_previews, options.colorHost, () => !!request || staging, value => { staging = value; updateControls() }))
      }
      const colorResult = message.action.page === 'color'
      const suggestions = colorResult
        ? proposal.suggestions.filter(item => item.field === 'manufacturer_color_name' && item.sources?.length)
        : proposal.suggestions
      if (!candidates.length && suggestions.length) {
        const details = document.createElement('details')
        details.className = 'chat-evidence'
        const summary = document.createElement('summary')
        summary.textContent = t(colorResult ? 'colorAssistant.productEvidence' : 'filaBrain.taskDraftDetails')
        const fields = document.createElement('dl')
        for (const suggestion of suggestions) {
          const title = document.createElement('dt'), value = document.createElement('dd')
          title.textContent = researchFieldLabel(suggestion.field)
          value.textContent = evidenceValue(suggestion.value)
          for (const source of suggestion.sources || []) {
            if (source.url) value.append(' ', evidenceLink(source.title, source.url))
            if (colorResult && source.evidence) {
              const quote = document.createElement('p')
              quote.textContent = source.evidence
              value.append(quote)
            }
          }
          fields.append(title, value)
        }
        details.append(summary, fields)
        log.append(details)
      }
      if (message.action.page !== 'color' && !candidates.length && (proposal.suggestions.length || proposal.target.identity.designation) && options.openResearch) {
        const button = document.createElement('button')
        button.type = 'button'
        button.className = 'fm-btn fm-btn-outline chat-action'
        button.textContent = t('filaBrain.taskReviewDraft')
        button.addEventListener('click', () => {
          if (request) return
          const draft = structuredClone(proposal)
          if (message.selectedSpoolProfile) draft.spool_profile_id = message.selectedSpoolProfile
          options.openResearch!({ proposal: draft })
        })
        log.append(button)
      }
    }
    if (message.action && 'url' in message.action && Object.hasOwn(pageLabels, message.action.page)) {
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
    bubbles.set(message, paragraph)
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
      button.textContent = conversation.title || conversation.messages[0]?.content.slice(0, 100) || conversation.draft || t('filaBrain.chatNew')
      if (conversation.id === current.id) button.setAttribute('aria-current', 'true')
      button.addEventListener('click', async () => {
        if (busyResearch || staging) return
        saveDraft()
        let selected = conversation
        try {
          const latest = readHistory().find(item => item.id === conversation.id)
          if (latest) selected = mergeConversation(conversation, latest)
        } catch { /* Keep the in-memory transcript available. */ }
        const researchChat = !!(selected.researching || selected.research)
        reset(researchChat)
        current = pending.get(selected.id)?.conversation ?? { ...selected, messages: [...selected.messages] }
        request = pending.get(selected.id)?.controller
        agent.restore(current.task)
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
          input.value = request ? '' : current.draft || ''
          status.textContent = request ? t('filaBrain.chatWorking') : ''
          if (!researchChat) current.messages.forEach(message => append(message))
          if (selected.context) await restoreChatContext(selected.context)
          else await restoreChatContext()
          const draft = current.research
          let resumed = false
          if (draft && research) {
            try { resumed = await research.resume(draft) }
            catch { status.textContent = t('filaBrain.researchHistoryReadOnly') }
          }
          if (!resumed) current.research = undefined
          if (researchChat) current.messages.forEach((message, index) => {
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
      Array.isArray(item.messages) && (item.messages.length || typeof item.draft === 'string' && item.draft.trim()) && item.messages.every(
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
    const useStored = localDraftSaved || (!storedDraftSaved && lastUpdated(stored) > lastUpdated(local))
    const [latest, other] = useStored ? [stored, local] : [local, stored]
    const receipts = other.messages.filter(message => message.savedDraftId && !hasReceipt(latest, message.savedDraftId))
    const research = useStored ? (storedDraftSaved ? undefined : stored.research) : local.research
    return !receipts.length && research === latest.research ? latest : { ...latest, research, messages: [...latest.messages, ...receipts] }
  }

  function draftId() {
    return current.research?.saveId ?? current.id
  }

  function newId() {
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
  }

  function remember(conversation = current) {
    if (conversation === current) {
      conversation.draft = input.value
      conversation.context = chatContext.snapshot()
      conversation.model = modelPicker.selected
      conversation.task = agent.snapshot()
    }
    if (!conversation.id) conversation.id = newId()
    if (!conversation.title) conversation.title = (conversation.messages[0]?.content || conversation.draft || t('filaBrain.chatNew')).slice(0, 100)
    const selection = conversation.messages.find(message => message.activeSpoolChoice) ?? conversation.messages.at(-1)
    const lookup = selection?.selectedSpoolProfile ? spoolLookupMessage(conversation.messages, selection) : undefined
    const running = pending.get(conversation.id)
    const complete = running && conversation.messages.at(-1) === running.question ? conversation.messages.slice(0, -1) : conversation.messages
    const messages = chatHistoryWindow(complete, 60, lookup)
    const earlierReceipts = conversation.messages.slice(0, -60).filter(message => message.savedDraftId && !messages.includes(message))
    messages.unshift(...earlierReceipts)
    conversation.updatedAt = Math.max(Date.now(), (conversation.updatedAt ?? 0) + 1,
      (saved.find(item => item.id === conversation.id)?.updatedAt ?? 0) + 1)
    const snapshot = { ...conversation, draft: running ? running.draft : conversation.draft, messages }
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

  function saveDraft() {
    dictation.stop()
    if (!root.getClientRects().length || busyResearch || staging || forkNextResearchTurn) return
    const context = chatContext.snapshot()
    if (input.value === (current.draft ?? '') && JSON.stringify(context) === JSON.stringify(current.context ?? {})
        && JSON.stringify(modelPicker.selected) === JSON.stringify(current.model)
        && JSON.stringify(agent.snapshot()) === JSON.stringify(current.task)) return
    if (!current.messages.length && !input.value.trim()) return
    if (!pending.has(current.id)) {
      try {
        const stored = readHistory().find(item => item.id === current.id)
        if (stored) {
          const previous = current
          current = mergeConversation(current, stored)
          if (current !== previous && !researching) {
            log.replaceChildren()
            bubbles.clear()
            current.messages.forEach(message => append(message))
          }
        }
      } catch { /* Preserve the in-memory draft if storage is unavailable. */ }
    }
    remember()
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
    request = undefined
    current = { id: '', title: '', messages: [] }
    forkNextResearchTurn = false
    researching = useResearch
    research?.reset()
    root.dispatchEvent(new CustomEvent('filabrain:chat-mode', { bubbles: true, detail: useResearch }))
    modelPicker.reset()
    agent.restore()
    intakeChoice = undefined
    log.after(researchResults)
    activeAnswer = undefined
    log.replaceChildren()
    bubbles.clear()
    input.value = ''
    contextPanel.open = false
    void restoreChatContext(options.context)
    status.textContent = ''
    history.open = false
    renderHistory()
    updateControls()
  }

  form.addEventListener('click', event => {
    if (event.target instanceof Element && event.target.closest('[role=menuitemradio]')) updateControls()
  })

  input.addEventListener('input', () => { intakeChoice = undefined; agent.resetPage(); updateControls() })
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !request && !busyResearch) {
      event.preventDefault()
      form.requestSubmit()
    }
  })
  for (const button of prompts.querySelectorAll<HTMLButtonElement>('[data-chat-prompt]')) {
    button.addEventListener('click', () => {
      if (button.dataset.chatPrompt === 'chatPromptAdd' && (research || options.openResearch)) {
        if (research) research.start()
        else options.openResearch!()
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
    saveDraft()
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
    const entered = input.value.trim()
    if (agent.active && entered.length > 400 && !/^https?:\/\/\S+$/i.test(entered)) {
      status.textContent = t('filaBrain.taskQueryTooLong')
      return
    }
    const content = entered || (agent.allowsEmpty ? t(agent.value === 'stock' ? 'filaBrain.taskStock' : 'filaBrain.taskHealth') : '')
    if (!content || busyResearch || staging) return
    const pendingContext = contextInitialization
    await pendingContext
    if (!root.isConnected || request || busyResearch || staging || pendingContext !== contextInitialization || input.value.trim() !== entered) return
    if (researching) {
      try { await research?.followUp(content) }
      catch { status.textContent = t('filaBrain.failed') }
      return
    }
    if (!agent.active && !modelPicker.selected) {
      status.textContent = t('filaBrain.chooseModel')
      return
    }
    const conversation = current
    if (!conversation.id) conversation.id = newId()
    if (!conversation.title) conversation.title = content.slice(0, 100)
    const choiceModel = modelPicker.selected
    const taskRequest = agent.active ? ['intake', 'color'].includes(agent.value) && intakeChoice
      ? { path: agent.value === 'color' ? '/agent/color-intake' : '/agent/intake', body: intakeChoice } : agent.request(entered, chatContext.snapshot()) : undefined
    const taskOptions = agent.snapshot()
    intakeChoice = undefined
    const question: Message = { role: 'user', content }
    modelPicker.preserve(modelPicker.selected)
    const bubble = append(question)
    const controller = request = new AbortController()
    pending.set(conversation.id, { conversation, controller, question, draft: entered })
    input.value = ''
    status.textContent = t('filaBrain.chatWorking')
    updateControls()
    try {
      const choice = conversation.messages.find(message => message.activeSpoolChoice)
      const selection = choice ?? conversation.messages.at(-1)
      const selectedProfileId = selection?.selectedSpoolProfile
      const lookup = selection && selectedProfileId ? spoolLookupMessage(conversation.messages, selection) : undefined
      const lookupFilamentId = lookup?.spoolLookupFilamentId
      const filamentId = filament.value ? Number(filament.value) : null
      const history = chatHistoryWindow(conversation.messages, 10, lookup)
      const lookupMessageIndex = lookup ? history.indexOf(lookup) : -1
      conversation.messages.push(question)
      remember(conversation)
      const { reply, action, proposal, spool_profiles, selected_spool_profile_id, usage, coverage, complete, ...grounding } = await api.post<{
        reply: string; action: Action | null; proposal?: ResearchProposal; spool_profiles?: SpoolProfile[]; selected_spool_profile_id?: string
        usage?: Message['usage']; coverage?: Coverage; complete?: boolean
      } & Partial<Grounding>>('/filabrain' + (taskRequest?.path || '/chat'), taskRequest?.body || {
        messages: [...history, question].map(({ role, content, confirmedSpoolProfile }) => ({
          role, content, ...(confirmedSpoolProfile ? { confirmed_spool_profile_id: confirmedSpoolProfile } : {}),
        })),
        ...choiceModel,
        ...(typeof selectedProfileId === 'string' ? { selected_spool_profile_id: selectedProfileId } : {}),
        ...(lookupMessageIndex >= 0 ? { spool_lookup_message_index: lookupMessageIndex } : {}),
        ...(lookupFilamentId !== undefined ? { spool_lookup_filament_id: lookupFilamentId } : {}),
        ...(printer.value ? { printer_id: Number(printer.value) } : {}),
        ...(filamentId !== null ? { filament_id: filamentId } : {}),
        ...(nozzle.value ? { nozzle_mm: Number(nozzle.value) } : {}),
      }, { signal: controller.signal })
      if (pending.get(conversation.id)?.controller !== controller) return
      if (choice?.activeSpoolChoice && choice.selectedSpoolProfile === selectedProfileId &&
          (selectedProfileId === undefined || selected_spool_profile_id !== selectedProfileId)) {
        choice.activeSpoolChoice = undefined
      }
      const spoolProfiles = Array.isArray(spool_profiles) ? spool_profiles.slice(0, 4) : undefined
      if (selected_spool_profile_id && lookup && !lookup.spoolLookupId) lookup.spoolLookupId = newId()
      const answer: Message = {
        role: 'assistant', content: reply, action: (action?.page === 'intake' || action?.page === 'color') ? { ...action, proposal } : action ?? undefined,
        usage,
        usageModel: taskRequest ? undefined : choiceModel,
        report: taskRequest ? { task: taskOptions, query: entered, coverage, complete } : undefined,
        grounding: Array.isArray(grounding.evidence) ? grounding as Grounding : undefined,
        spoolProfiles,
        spoolLookupFilamentId: spoolProfiles?.length
          ? (selected_spool_profile_id && lookupFilamentId !== undefined ? lookupFilamentId : filamentId)
          : undefined,
        selectedSpoolProfile: selected_spool_profile_id,
        confirmedSpoolProfile: selected_spool_profile_id,
        spoolLookupId: spoolProfiles?.length ? (selected_spool_profile_id && lookup?.spoolLookupId || newId()) : undefined,
      }
      conversation.messages.push(answer)
      pending.delete(conversation.id)
      if (current === conversation) {
        append(answer)
        status.textContent = ''
      }
      remember(conversation)
    } catch (error) {
      if (pending.get(conversation.id)?.controller !== controller) return
      if (conversation.messages.at(-1) === question) conversation.messages.pop()
      conversation.draft = entered
      pending.delete(conversation.id)
      if (current === conversation) {
        bubbles.get(question)?.remove()
        bubbles.delete(question)
        input.value = entered
        status.textContent = controller.signal.aborted ? t('filaBrain.chatStopped') : error instanceof ApiError ? error.message : t('filaBrain.chatFailed')
      }
      remember(conversation)
    } finally {
      if (pending.get(conversation.id)?.controller === controller) pending.delete(conversation.id)
      if (current === conversation) {
        request = undefined
        updateControls()
        if (root.getClientRects().length) input.focus()
      }
      renderHistory()
    }
  })

  document.addEventListener('filaman:model-change', event => {
    const detail = (event as CustomEvent<typeof statusModel>).detail
    dictation.configure(detail)
    model = detail.chat_model
    void modelPicker.setDefault(model, detail.chat_endpoint_id)
    updateControls()
  })
  function startResearch(fresh = false) {
    if (busyResearch || staging) return false
    if (fresh || !researching && (current.messages.length || input.value || request)) reset(true)
    else if (!researching) {
      researching = true
      root.dispatchEvent(new CustomEvent('filabrain:chat-mode', { bubbles: true, detail: true }))
    }
    updateControls()
    const field = researchInput.querySelector<HTMLElement>('input[name="query"]') || researchInput.querySelector<HTMLElement>('input[name="designation"]:not([readonly])')
    if (field?.getClientRects().length) field.focus()
    return true
  }

  updateControls()
  return {
    root,
    saveDraft,
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
        root.dispatchEvent(new CustomEvent('filabrain:chat-mode', { bubbles: true, detail: researching }))
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
