import { getLang, t } from './shared/i18n'

type Recognition = {
  lang: string; continuous: boolean; interimResults: boolean
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null
  onerror: (() => void) | null
  onend: (() => void) | null
  start(): void; abort(): void
}

export function mountDictation(form: HTMLFormElement, input: HTMLTextAreaElement) {
  const button = form.querySelector<HTMLButtonElement>('[data-chat-mic]')!
  const status = form.querySelector<HTMLElement>('[data-chat-dictation-status]')!
  const host = window as Window & {
    SpeechRecognition?: new () => Recognition
    webkitSpeechRecognition?: new () => Recognition
  }
  const Speech = host.SpeechRecognition || host.webkitSpeechRecognition
  let active: Recognition | undefined

  function stop() {
    const previous = active
    active = undefined
    previous?.abort()
    button.setAttribute('aria-pressed', 'false')
    button.setAttribute('aria-label', t('filaBrain.dictate'))
    if (previous) status.textContent = ''
  }

  button.title = t(!isSecureContext ? 'filaBrain.dictateSecure' : !Speech ? 'filaBrain.dictateUnavailable' : 'filaBrain.dictatePrivacy')
  button.setAttribute('aria-disabled', String(!isSecureContext || !Speech))
  button.addEventListener('click', () => {
    if (active) { stop(); return }
    if (!isSecureContext || !Speech) {
      status.textContent = t(!isSecureContext ? 'filaBrain.dictateSecure' : 'filaBrain.dictateUnavailable')
      return
    }
    if (input.disabled || input.readOnly) return
    const session = new Speech()
    const before = input.value.trimEnd()
    active = session
    session.lang = document.documentElement.lang || getLang()
    session.continuous = false
    session.interimResults = true
    session.onresult = event => {
      if (active !== session) return
      const words = Array.from(event.results, result => result[0].transcript).join(' ')
      input.value = [before, words].filter(Boolean).join(' ').slice(0, input.maxLength)
      input.dispatchEvent(new Event('input', { bubbles: true }))
    }
    session.onerror = () => {
      if (active !== session) return
      stop()
      status.textContent = t('filaBrain.dictateError')
    }
    session.onend = () => {
      if (active !== session) return
      active = undefined
      button.setAttribute('aria-pressed', 'false')
      button.setAttribute('aria-label', t('filaBrain.dictate'))
      status.textContent = ''
    }
    try {
      session.start()
      button.setAttribute('aria-pressed', 'true')
      button.setAttribute('aria-label', t('filaBrain.dictateStop'))
      status.textContent = t('filaBrain.dictateListening')
    } catch {
      stop()
      status.textContent = t('filaBrain.dictateError')
    }
  })
  input.addEventListener('input', event => { if (event.isTrusted) stop() })
  form.addEventListener('submit', stop, { capture: true })
  document.addEventListener('click', event => {
    if (event.target instanceof Element && event.target.closest('[data-tab], [data-chat-new], [data-chat-prompt], [data-chat-history-list] button')) stop()
  }, { capture: true })
  window.addEventListener('pagehide', stop)
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop() })
  return { stop, setBusy(busy: boolean) { button.disabled = busy || input.disabled; if (busy || input.disabled) stop() } }
}
