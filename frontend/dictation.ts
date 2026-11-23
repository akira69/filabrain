import { getLang, t } from './shared/i18n'
import { api } from './shared/api'
import { createAudioTranscription } from './audio-transcription'

export type DictationSettings = {
  microphone_enabled?: boolean
  microphone_endpoint_id?: string | null
  microphone_endpoint_name?: string | null
  microphone_revision?: string
}

type Recognition = {
  lang: string; continuous: boolean; interimResults: boolean
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null
  onerror: (() => void) | null
  onend: (() => void) | null
  start(): void; abort(): void
}

export function mountDictation(form: HTMLFormElement, input: HTMLTextAreaElement, config: DictationSettings = {}) {
  const button = form.querySelector<HTMLButtonElement>('[data-chat-mic]')!
  const status = form.querySelector<HTMLElement>('[data-chat-dictation-status]')!
  const host = window as Window & {
    SpeechRecognition?: new () => Recognition
    webkitSpeechRecognition?: new () => Recognition
  }
  const Speech = host.SpeechRecognition || host.webkitSpeechRecognition
  let active: Recognition | undefined
  let enabled = true
  let endpointId: string | null = null
  let endpointName = ''
  let revision = ''
  let phase = 'idle'
  let audioBefore = ''
  let checking = false
  let verification = 0
  let verifyRequest: AbortController | undefined
  let browserTimer = 0
  const audio = createAudioTranscription({
    text(text) {
      const draft = [audioBefore, text].filter(Boolean).join(' ')
      input.value = draft.slice(0, input.maxLength)
      input.dispatchEvent(new Event('input', { bubbles: true }))
      if (draft.length > input.maxLength) status.textContent = t('filaBrain.audioTrimmed')
    },
    status(key) { status.textContent = key ? t('filaBrain.' + key) : '' },
    state(value) { phase = value; refresh() },
  })
  function supported() {
    return endpointId ? !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder && window.AudioContext && window.OfflineAudioContext) : !!Speech
  }
  function refresh() {
    button.hidden = !enabled
    const listening = !!active || phase === 'starting' || phase === 'recording'
    button.setAttribute('aria-pressed', String(listening))
    button.setAttribute('aria-label', t(checking ? 'filaBrain.audioCancel' : phase === 'recording' ? 'filaBrain.audioStop'
      : phase !== 'idle' ? 'filaBrain.audioCancel' : active ? 'filaBrain.dictateStop' : 'filaBrain.dictate'))
    button.setAttribute('aria-disabled', String(!isSecureContext || !supported()))
    button.title = t(!isSecureContext ? 'filaBrain.dictateSecure' : !supported() ? 'filaBrain.dictateUnavailable'
      : endpointId ? 'filaBrain.dictateEndpointPrivacy' : 'filaBrain.dictatePrivacy', { name: endpointName })
  }
  function configure(value: DictationSettings) {
    const nextEnabled = value.microphone_enabled ?? enabled
    const nextEndpoint = value.microphone_endpoint_id === undefined ? endpointId : value.microphone_endpoint_id
    const nextRevision = value.microphone_revision ?? revision
    if (nextEnabled !== enabled || nextEndpoint !== endpointId || nextRevision !== revision) stop()
    enabled = nextEnabled
    endpointId = nextEndpoint
    revision = nextRevision
    if (value.microphone_endpoint_name !== undefined) endpointName = value.microphone_endpoint_name ?? ''
    refresh()
  }

  function stop() {
    verification++
    verifyRequest?.abort()
    verifyRequest = undefined
    checking = false
    clearTimeout(browserTimer)
    const previous = active
    active = undefined
    previous?.abort()
    audio.cancel()
    refresh()
    if (previous) status.textContent = ''
  }

  button.addEventListener('click', async () => {
    if (!enabled) return
    if (phase === 'recording') { audio.finish(); return }
    if (phase !== 'idle' || checking) { stop(); return }
    if (active) { stop(); return }
    if (!isSecureContext) { status.textContent = t('filaBrain.dictateSecure'); return }
    if (input.disabled || input.readOnly) return
    const generation = ++verification
    const controller = verifyRequest = new AbortController()
    checking = true
    status.textContent = t('filaBrain.audioChecking')
    refresh()
    try {
      const current = await api.get<DictationSettings & { enabled: boolean }>('/filabrain/status', { signal: controller.signal, cache: 'no-store' })
      if (generation !== verification) return
      const changed = (current.microphone_endpoint_id !== undefined && current.microphone_endpoint_id !== endpointId)
        || (current.microphone_revision !== undefined && current.microphone_revision !== revision)
      checking = false
      verifyRequest = undefined
      configure(current.enabled === false ? { ...current, microphone_enabled: false } : current)
      if (!enabled) { status.textContent = t('filaBrain.audioDisabled'); return }
      if (changed) { status.textContent = t('filaBrain.audioSettingsChanged'); return }
    } catch {
      if (generation === verification) { stop(); status.textContent = t('filaBrain.audioVerifyFailed') }
      return
    }
    if (!supported()) { status.textContent = t('filaBrain.dictateUnavailable'); return }
    status.textContent = ''
    if (endpointId) {
      if (!revision) { status.textContent = t('filaBrain.audioSettingsChanged'); return }
      audioBefore = input.value.trimEnd()
      void audio.start({ endpoint_id: endpointId, revision })
      return
    }
    const session = new Speech!()
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
      clearTimeout(browserTimer)
      refresh()
      status.textContent = ''
    }
    try {
      session.start()
      browserTimer = window.setTimeout(stop, 60000)
      refresh()
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
  document.addEventListener('change', event => {
    if (event.target instanceof Element && event.target.matches('[data-filament-target]')) stop()
  })
  window.addEventListener('pagehide', stop)
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop() })
  configure(config)
  return { stop, configure, setBusy(busy: boolean) { button.disabled = busy || input.disabled; if (busy || input.disabled) stop() } }
}
