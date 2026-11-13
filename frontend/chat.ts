import { api } from './shared/api'
import { t } from './shared/i18n'
import { mountModelPicker } from './shared/model-picker'
import { identity, type ResearchDraft } from './research'

const pageLabels = {
  new_filament: 'filaments.newFilament',
  new_spool: 'spools.newSpool',
  filaments: 'nav.filaments',
  research_batches: 'researchBatches.title',
  model_settings: 'filaBrain.settings',
}
type Action = { page: keyof typeof pageLabels; url: string }
type Message = {
  role: 'user' | 'assistant'; content: string; action?: Action; proposal?: ResearchDraft['proposal']; savedDraftId?: string
}
type Conversation = {
  id: string; title: string; messages: Message[]; research?: ResearchDraft
  researching?: true; model?: string; updatedAt?: number
}
type Research = {
  start(): void
  reset(): void
  resume(draft: ResearchDraft): Promise<boolean>
  archive(proposal: ResearchDraft['proposal']): HTMLElement
  followUp(prompt: string): Promise<void>
}

export function mountChat(model?: string | null, openSettings?: () => void, editorScope?: string, researchOnly = editorScope !== undefined) {
  const form = document.querySelector<HTMLFormElement>('[data-chat-form]')!
  const input = form.querySelector('textarea')!
  const submit = form.querySelector<HTMLButtonElement>('[type=submit]')!
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
  let researching = false
  let research: Research | undefined
  let activeAnswer: HTMLElement | undefined
  let forkNextResearchTurn = false
  const modelPicker = mountModelPicker(form, model, openSettings)
  const picker = form.querySelector<HTMLElement>('.chat-model-picker')!
  status.removeAttribute('data-i18n')
  status.textContent = ''
  submit.removeAttribute('data-i18n-aria-label')

  function scrollToEnd() { transcript.scrollTop = transcript.scrollHeight }

  function updateControls() {
    form.hidden = (!researching && !model) || (editorScope !== undefined && !form.contains(picker))
    input.disabled = researching && !current.messages.length && !busyResearch
    input.maxLength = researching ? 400 : 2000
    input.placeholder = t(researching ? 'filaBrain.followUpHint' : 'filaBrain.chatPlaceholder')
    input.setAttribute('aria-label', t(researching ? 'filaBrain.followUp' : 'filaBrain.chatQuestion'))
    submit.disabled = busyResearch || !request && (input.disabled || !input.value.trim())
    const label = t(request ? 'filaBrain.chatStop' : researching ? 'filaBrain.sendFollowUp' : 'filaBrain.chatSend')
    submit.setAttribute('aria-label', label)
    submit.title = label
    submit.toggleAttribute('data-busy', !!request)
    input.readOnly = !!request || busyResearch
    modelPicker.setBusy(!!request || busyResearch)
    status.toggleAttribute('data-chat-running', !!request)
    newChat.disabled = busyResearch || !researching && !request && !current.messages.length && !input.value
    prompts.hidden = researching || form.hidden || !!request || current.messages.length > 0
    setup.hidden = researching || !!model || current.messages.length > 0
    researchInput.hidden = researchResults.hidden = !researching
  }

  function append(message: Message) {
    const paragraph = document.createElement('p')
    paragraph.className = `chat-message chat-${message.role}`
    paragraph.textContent = message.content
    log.append(paragraph)
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
    }
    scrollToEnd()
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
      button.textContent = conversation.title || conversation.messages[0].content.slice(0, 100)
      if (conversation.id === current.id) button.setAttribute('aria-current', 'true')
      button.addEventListener('click', async () => {
        if (busyResearch) return
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
        if (selected.research && lastResult === -1) {
          lastResult = current.messages.length - 1
          current.messages[lastResult] = { ...current.messages[lastResult], proposal: selected.research.proposal }
        }
        modelPicker.reset(selected.model)
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
              if (resumed) placeResearchResults(bubble)
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
    const messages = current.messages.slice(-60)
    const earlierReceipts = current.messages.slice(0, -60).filter(message => message.savedDraftId)
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
    const content = input.value.trim()
    if (!content || busyResearch) return
    if (researching) {
      try { await research?.followUp(content) }
      catch { status.textContent = t('filaBrain.failed') }
      return
    }
    const question: Message = { role: 'user', content }
    const bubble = append(question)
    const controller = request = new AbortController()
    input.value = ''
    status.textContent = t('filaBrain.chatWorking')
    updateControls()
    try {
      const { reply, action } = await api.post<{ reply: string; action: Action | null }>('/filabrain/chat', {
        messages: [...current.messages.slice(-10), question].map(({ role, content }) => ({ role, content })),
        model_id: modelPicker.selected,
      }, { signal: controller.signal })
      if (request !== controller) return
      const answer: Message = { role: 'assistant', content: reply, action: action ?? undefined }
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
    model = (event as CustomEvent<string>).detail
    void modelPicker.setDefault(model)
    updateControls()
  })
  function startResearch() {
    if (busyResearch) return
    if (!researching && (current.messages.length || input.value || request)) reset(true)
    else if (!researching) {
      researching = true
      document.dispatchEvent(new CustomEvent('filabrain:chat-mode', { detail: true }))
    }
    updateControls()
    const field = researchInput.querySelector<HTMLElement>('input[name="designation"]:not([type="hidden"])')
    if (field?.getClientRects().length) field.focus()
  }

  updateControls()
  return {
    get model() { return modelPicker.selected },
    picker,
    startResearch,
    get draftId() { return draftId() },
    get savedDraft() {
      const id = draftId()
      return hasReceipt(current, id) || hasStoredReceipt(id)
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
