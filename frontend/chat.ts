import { api } from './shared/api'
import { t } from './shared/i18n'
import { mountModelPicker } from './shared/model-picker'
import type { ResearchDraft } from './research'

const pageLabels = {
  new_filament: 'filaments.newFilament',
  new_spool: 'spools.newSpool',
  filaments: 'nav.filaments',
  research_batches: 'researchBatches.title',
  model_settings: 'filaBrain.settings',
}
type Action = { page: keyof typeof pageLabels; url: string }
type Message = { role: 'user' | 'assistant'; content: string; action?: Action }
type Conversation = { id: string; title: string; messages: Message[]; research?: ResearchDraft }
type Research = {
  start(): void
  reset(): void
  resume(draft: ResearchDraft): Promise<void>
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
  let request: AbortController | undefined
  let busyResearch = false
  let researching = false
  let research: Research | undefined
  const modelPicker = mountModelPicker(form, model, openSettings)
  status.removeAttribute('data-i18n')
  status.textContent = ''
  submit.removeAttribute('data-i18n-aria-label')

  function scrollToEnd() { transcript.scrollTop = transcript.scrollHeight }

  function updateControls() {
    form.hidden = !researching && !model
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
    scrollToEnd()
    return paragraph
  }

  function renderHistory() {
    historyList.replaceChildren()
    const conversations = saved.filter(item => !item.research || research)
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
        reset(!!conversation.research)
        current = { ...conversation, messages: [...conversation.messages] }
        busyResearch = true
        updateControls()
        try {
          if (conversation.research) {
            research?.start()
            await research?.resume(conversation.research)
          }
          current.messages.forEach(append)
        } catch {
          status.textContent = t('filaBrain.failed')
        } finally {
          busyResearch = false
          renderHistory()
          updateControls()
          scrollToEnd()
          history.querySelector('summary')?.focus()
        }
      })
      historyList.append(button)
    }
  }

  function readHistory(): Conversation[] {
    if (!storageKey) return []
    const stored: unknown = JSON.parse(localStorage.getItem(storageKey) || '[]')
    return Array.isArray(stored) ? stored.filter(item => typeof item?.id === 'string' &&
      Array.isArray(item.messages) && item.messages.length && item.messages.every(
        (message: Message) => (message?.role === 'user' || message?.role === 'assistant') &&
          typeof message.content === 'string')) : []
  }

  function remember() {
    if (!current.id) {
      current.id = String(Date.now()) + '-' + Math.random().toString(36).slice(2)
      current.title = current.messages[0].content.slice(0, 100)
    }
    const snapshot = { ...current, messages: current.messages.slice(-60) }
    saved = [snapshot, ...saved.filter(item => item.id !== snapshot.id)].slice(0, 20)
    if (storageKey) {
      try {
        const byId = new Map([...saved, ...readHistory()].map(item => [item.id, item]))
        saved = [snapshot, ...[...byId.values()].filter(item => item.id !== snapshot.id)].slice(0, 20)
        localStorage.setItem(storageKey, JSON.stringify(saved))
      } catch { status.textContent = t('filaBrain.chatHistoryFailed') }
    }
    renderHistory()
  }

  if (editorScope === '') {
    renderHistory()
    history.hidden = false
  } else {
    void api.get<{ id: number }>('/me', { cache: 'no-store' }).then(({ id }) => {
      if (!Number.isSafeInteger(id) || id < 1) return
      storageKey = `filabrain:chat:${id}${editorScope ? ':' + editorScope : ''}`
      try { saved = [...saved, ...readHistory()].slice(0, 20) }
      catch { /* The current conversation remains available in memory. */ }
      if (current.messages.length) remember()
      renderHistory()
      history.hidden = false
    }).catch(() => {})
  }

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
    researching = useResearch
    research?.reset()
    document.dispatchEvent(new CustomEvent('filabrain:chat-mode', { detail: useResearch }))
    modelPicker.reset()
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
      updateControls()
    }
    const field = researchInput.querySelector<HTMLElement>('input[name="designation"]:not([type="hidden"])')
    if (field?.getClientRects().length) field.focus()
  }

  updateControls()
  return {
    get model() { return modelPicker.selected },
    picker: form.querySelector<HTMLElement>('.chat-model-picker')!,
    startResearch,
    setResearch(value: Research) { research = value; renderHistory() },
    showChat() {
      if (busyResearch) {
        document.dispatchEvent(new CustomEvent('filabrain:chat-mode', { detail: researching }))
      } else if (researching) reset(false)
    },
    async researchTurn(content: string, run: () => Promise<{ reply: string; draft?: ResearchDraft }>) {
      if (busyResearch) return
      const question: Message = { role: 'user', content }
      append(question)
      const note = append({ role: 'assistant', content: t('filaBrain.searching') })
      note.className = 'chat-message research-note'
      busyResearch = true
      status.textContent = ''
      updateControls()
      try {
        const { reply, draft } = await run()
        note.textContent = reply
        if (draft) {
          const answer: Message = { role: 'assistant', content: reply }
          current.messages.push(question, answer)
          current.research = draft
          if (input.value.trim() === content) input.value = ''
          remember()
        } else {
          note.className = 'chat-message chat-assistant'
        }
      } finally {
        busyResearch = false
        updateControls()
        scrollToEnd()
      }
    },
  }
}

export type Chat = ReturnType<typeof mountChat>
