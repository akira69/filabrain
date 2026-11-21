import { researchHeaders } from './shared/filabrain'

type Source = { endpoint_id: string; revision: string }
type Phase = 'idle' | 'starting' | 'recording' | 'transcribing'
type Callbacks = { text(text: string): void; status(key: string): void; state(phase: Phase): void }

function wav(samples: Float32Array) {
  const bytes = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(bytes)
  const tag = (offset: number, value: string) => [...value].forEach((character, index) => view.setUint8(offset + index, character.charCodeAt(0)))
  tag(0, 'RIFF'); view.setUint32(4, bytes.byteLength - 8, true); tag(8, 'WAVE'); tag(12, 'fmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true)
  tag(36, 'data'); view.setUint32(40, samples.length * 2, true)
  samples.forEach((value, index) => {
    const sample = Math.max(-1, Math.min(1, value))
    view.setInt16(44 + index * 2, sample * (sample < 0 ? 32768 : 32767), true)
  })
  return new Blob([bytes], { type: 'audio/wav' })
}

export function createAudioTranscription(callbacks: Callbacks) {
  let revision = 0
  let phase: Phase = 'idle'
  let stream: MediaStream | undefined
  let recorder: MediaRecorder | undefined
  let controller: AbortController | undefined
  let timer = 0
  function state(value: Phase) { phase = value; callbacks.state(value) }
  function releaseTracks() {
    const previous = stream
    stream = undefined
    previous?.getTracks().forEach(track => track.stop())
  }
  function cancel() {
    revision++
    clearTimeout(timer)
    controller?.abort()
    controller = undefined
    const previous = recorder
    recorder = undefined
    releaseTracks()
    if (previous && previous.state !== 'inactive') previous.stop()
    state('idle')
    callbacks.status('')
  }
  function finish() {
    if (phase !== 'recording' || !recorder) return
    clearTimeout(timer)
    state('transcribing')
    callbacks.status('audioTranscribing')
    const previous = recorder
    recorder = undefined
    previous.stop()
    releaseTracks()
  }
  async function start(source: Source) {
    cancel()
    const generation = revision
    state('starting')
    callbacks.status('audioStarting')
    try {
      const granted = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (generation !== revision) { granted.getTracks().forEach(track => track.stop()); return }
      stream = granted
      const type = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm'].find(type => MediaRecorder.isTypeSupported(type))
      const recording = recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined)
      const chunks: Blob[] = []
      let size = 0
      recording.ondataavailable = event => {
        if (generation !== revision || !event.data.size) return
        size += event.data.size
        if (size > 8 * 1024 * 1024) { cancel(); callbacks.status('audioTooLarge'); return }
        chunks.push(event.data)
      }
      recording.onerror = () => {
        if (generation === revision) { cancel(); callbacks.status('audioFailed') }
      }
      recording.onstop = async () => {
        if (generation !== revision) return
        clearTimeout(timer)
        recorder = undefined
        releaseTracks()
        state('transcribing')
        callbacks.status('audioTranscribing')
        let decoder: AudioContext | undefined
        try {
          decoder = new AudioContext()
          const decoded = await decoder.decodeAudioData(await new Blob(chunks, { type: recording.mimeType }).arrayBuffer())
          if (generation !== revision) return
          if (!Number.isFinite(decoded.duration) || decoded.duration <= 0 || decoded.duration > 61) throw new Error('Invalid audio duration')
          const offline = new OfflineAudioContext(1, Math.max(1, Math.ceil(Math.min(decoded.duration, 60) * 16000)), 16000)
          const input = offline.createBufferSource()
          input.buffer = decoded
          input.connect(offline.destination)
          input.start()
          const rendered = await offline.startRendering()
          if (generation !== revision) return
          const body = wav(rendered.getChannelData(0))
          await decoder.close()
          decoder = undefined
          if (generation !== revision) return
          const request = controller = new AbortController()
          timer = window.setTimeout(() => {
            if (generation === revision) { cancel(); callbacks.status('audioFailed') }
          }, 130000)
          const query = new URLSearchParams({ endpoint_id: source.endpoint_id, revision: source.revision })
          const response = await fetch('/api/v1/filabrain/transcribe?' + query, {
            method: 'POST', credentials: 'include', signal: request.signal,
            headers: { ...researchHeaders(), 'Content-Type': 'audio/wav' }, body,
          })
          if (generation !== revision) return
          if (!response.ok) {
            cancel()
            callbacks.status(response.status === 409 ? 'audioSettingsChanged' : 'audioFailed')
            return
          }
          const data = await response.json()
          if (generation !== revision) return
          if (typeof data.text !== 'string' || !data.text.trim() || data.text.length > 32000) throw new Error('Invalid transcript')
          clearTimeout(timer)
          controller = undefined
          state('idle')
          callbacks.status('')
          callbacks.text(data.text.trim())
        } catch {
          if (generation === revision) { cancel(); callbacks.status('audioFailed') }
        } finally {
          await decoder?.close().catch(() => {})
        }
      }
      recording.start(1000)
      state('recording')
      callbacks.status('audioRecording')
      timer = window.setTimeout(finish, 60000)
    } catch {
      if (generation === revision) { cancel(); callbacks.status('audioPermissionError') }
    }
  }
  return { start, finish, cancel }
}
