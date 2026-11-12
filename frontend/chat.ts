import { initI18n, t } from './shared/i18n'
import { researchHeaders } from './shared/filabrain'
import { mountModelPicker } from './shared/model-picker'

export async function mountChat(openSettings?: () => void) {
  await initI18n()
  const api = '/api/v1/filabrain'
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
  type Message = { role: 'user' | 'assistant'; content: string }
  const pageLabels = {
    new_filament: 'filaments.newFilament',
    new_spool: 'spools.newSpool',
    filaments: 'nav.filaments',
    research_batches: 'researchBatches.title',
    model_settings: 'filaBrain.settings',
  }
  let messages: Message[] = []
  let request: AbortController | undefined
  let modelPicker: ReturnType<typeof mountModelPicker> | undefined
  status.removeAttribute('data-i18n')
  submit.removeAttribute('data-i18n-aria-label')

  function updateControls() {
    submit.disabled = !request && !input.value.trim()
    submit.setAttribute('aria-label', t(request ? 'filaBrain.chatStop' : 'filaBrain.chatSend'))
    submit.title = submit.getAttribute('aria-label')!
    submit.toggleAttribute('data-busy', !!request)
    input.readOnly = !!request
    modelPicker?.setBusy(!!request)
    newChat.disabled = !request && !messages.length && !input.value
    prompts.hidden = form.hidden || !!request || messages.length > 0
  }

  function append(message: Message) {
    const paragraph = document.createElement('p')
    paragraph.className = `chat-message chat-${message.role}`
    paragraph.textContent = message.content
    log.append(paragraph)
    transcript.scrollTop = transcript.scrollHeight
    return paragraph
  }

  input.addEventListener('input', updateControls)
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !request) {
      event.preventDefault()
      form.requestSubmit()
    }
  })
  for (const button of prompts.querySelectorAll<HTMLButtonElement>('[data-chat-prompt]')) {
    button.addEventListener('click', () => {
      input.value = t(`filaBrain.${button.dataset.chatPrompt}`)
      updateControls()
      input.focus()
    })
  }
  setupButton.hidden = !openSettings
  if (openSettings) setupButton.addEventListener('click', openSettings)
  setupHint.textContent = t(openSettings ? 'filaBrain.chatSetup' : 'filaBrain.chatAskAdmin')
  newChat.addEventListener('click', () => {
    request?.abort()
    request = undefined
    messages = []
    modelPicker?.reset()
    log.replaceChildren()
    input.value = ''
    status.textContent = ''
    updateControls()
    input.focus()
  })
  form.addEventListener('submit', async event => {
    event.preventDefault()
    if (request) {
      request.abort()
      return
    }
    const content = input.value.trim()
    if (!content) return
    const question: Message = { role: 'user', content }
    const bubble = append(question)
    const controller = request = new AbortController()
    input.value = ''
    status.textContent = t('filaBrain.chatWorking')
    updateControls()
    try {
      const response = await fetch(`${api}/chat`, {
        method: 'POST', credentials: 'include', headers: researchHeaders(), signal: controller.signal,
        body: JSON.stringify({ messages: [...messages.slice(-10), question], model_id: modelPicker?.selected }),
      })
      if (!response.ok) throw new Error()
      const { reply, action } = await response.json() as {
        reply: string
        action: { page: keyof typeof pageLabels; url: string } | null
      }
      if (request !== controller) return
      const answer: Message = { role: 'assistant', content: reply }
      messages.push(question, answer)
      append(answer)
      if (action) {
        const link = document.createElement('a')
        link.className = 'fm-btn fm-btn-outline chat-action'
        link.href = action.page === 'research_batches' ? '?tab=batches'
          : action.page === 'model_settings' ? '?tab=settings' : action.url
        if (!link.getAttribute('href')?.startsWith('?')) link.target = '_blank'
        link.rel = 'noopener noreferrer'
        link.textContent = t(pageLabels[action.page])
        log.append(link)
        transcript.scrollTop = transcript.scrollHeight
      }
      status.textContent = ''
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

  try {
    const response = await fetch(`${api}/status`, { credentials: 'include' })
    if (!response.ok) throw new Error()
    const availability = await response.json()
    if (!availability.can_chat) {
      status.textContent = t('filaBrain.chatUnavailable')
    } else {
      modelPicker = mountModelPicker(form, availability.chat_model, openSettings)
      function showComposer(model?: string) {
        form.hidden = !model
        setup.hidden = !!model
        status.textContent = ''
        updateControls()
      }
      showComposer(availability.chat_model)
      document.addEventListener('filaman:model-change', event => {
        const model = (event as CustomEvent<string>).detail
        void modelPicker?.setDefault(model)
        showComposer(model)
      })
    }
  } catch {
    status.textContent = t('filaBrain.chatFailed')
  }
}
