import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
const html = await readFile(new URL('../filabrain/page.html', import.meta.url), 'utf8')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
let calls = 0
const audioUploads = []
let knownWindow = true
let canCreate = true
let microphoneConfig={microphone_enabled:true,microphone_endpoint_id:null,microphone_endpoint_name:null,microphone_revision:'r1'}
const fixture = route => {
  const path = new URL(route.request().url()).pathname
  if (path.startsWith('/plugin-page/')) return route.fulfill({ contentType: 'text/html', body: html })
  if (path.endsWith('/status')) return route.fulfill({ json: { enabled: true, can_chat: true, can_create: canCreate, can_configure: false, chat_model: 'test', ...microphoneConfig } })
  if (path.endsWith('/filabrain/models')) return route.fulfill({ json: { groups: [{ id: 'local', name: 'Test', models: ['test', 'other'] }], default_endpoint_id: 'local', default_model_id: 'test' } })
  if (path.endsWith('/transcribe')) {
    const body=route.request().postDataBuffer()
    audioUploads.push({url:route.request().url(),type:route.request().headers()['content-type'],header:body.toString('ascii',0,4),rate:body.readUInt32LE(24)})
    return route.fulfill({json:{text:'Paramount 3D Enzo Red'}})
  }
  if (path.endsWith('/chat')) {
    calls++
    return route.fulfill({ json: { reply: 'Answer', action: null, usage: { prompt_tokens: 120, completion_tokens: 40, ...(knownWindow ? { context_window: 4096 } : {}) } } })
  }
  if (path.endsWith('/me')) return route.fulfill({ json: { id: 1 } })
  if (path.endsWith('/batches')) return route.fulfill({ json: [] })
  return route.fulfill({ status: 404, json: {} })
}
try {
  const context = await browser.newContext()
  await context.route('**/*', fixture)
  await context.addInitScript(() => {
    window.recognitions = []
    window.SpeechRecognition = class {
      constructor() { window.recognitions.push(this) }
      start() { this.started = true }
      abort() { this.aborted = true; this.onend?.() }
    }
  })
  const page = await context.newPage()
  page.setDefaultTimeout(5000)
  await page.goto('https://fila.test/plugin-page/filabrain')
  const input = page.getByRole('textbox', { name: 'Your question' })
  await input.waitFor()
  const mic = page.getByRole('button', { name: 'Dictate a message', exact: true })
  assert.equal(await page.evaluate(() => window.recognitions.length), 0, 'no automatic recording')
  assert.match(await mic.getAttribute('title'), /may process audio online/)
  await input.fill('Existing draft.')
  await mic.click()
  await page.locator('[data-chat-mic][aria-pressed=true]').waitFor()
  await page.getByRole('button', { name: 'Stop dictation', exact: true }).waitFor()
  await page.evaluate(() => window.recognitions.at(-1).onresult({ results: [[{ transcript: 'Print PETG' }]] }))
  assert.equal(await input.inputValue(), 'Existing draft. Print PETG')
  assert.equal(calls, 0, 'dictation never auto-sends')
  await page.getByRole('button', { name: 'Stop dictation', exact: true }).click()
  await page.evaluate(() => window.recognitions.at(-1).onresult({ results: [[{ transcript: 'late result' }]] }))
  assert.equal(await input.inputValue(), 'Existing draft. Print PETG', 'late results after stop are ignored')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await page.getByText('Answer', { exact: true }).waitFor()
  await page.getByRole('button', { name: 'Context: 3.9% · 160 / 4,096 tokens', exact: true }).waitFor()
  await page.getByRole('button', { name: /^Model for this conversation/ }).click()
  await page.getByRole('menuitemradio', { name: 'other', exact: true }).click()
  assert.equal(await page.locator('[data-chat-usage]').isVisible(), false, 'changing models hides the previous model’s context meter')
  await page.getByRole('button', { name: /^Model for this conversation/ }).click()
  await page.getByRole('menuitemradio', { name: 'test Default', exact: true }).click()

  await page.getByRole('button', { name: 'New chat', exact: true }).click()
  assert.equal(await page.locator('[data-chat-usage]').isVisible(), false)
  await mic.click()
  await page.locator('[data-chat-mic][aria-pressed=true]').waitFor()
  await page.evaluate(() => window.recognitions.at(-1).onerror())
  await page.getByText(/Dictation could not start/).waitFor()
  await input.fill('Draft before recording')
  await mic.click()
  await page.locator('[data-chat-mic][aria-pressed=true]').waitFor()
  await page.getByRole('button', { name: 'New chat', exact: true }).click()
  await page.evaluate(() => window.recognitions.at(-1).onresult({ results: [[{ transcript: 'wrong conversation' }]] }))
  assert.equal(await input.inputValue(), '')
  await mic.click()
  await page.locator('[data-chat-mic][aria-pressed=true]').waitFor()
  await page.getByRole('tab', { name: 'Chat', exact: true }).focus()
  await page.keyboard.press('ArrowRight')
  await page.evaluate(() => window.recognitions.at(-1).onresult({ results: [[{ transcript: 'hidden recording' }]] }))
  assert.equal(await page.evaluate(() => window.recognitions.at(-1).aborted), true)
  await page.getByRole('tab', { name: 'Chat', exact: true }).click()
  assert.equal(await input.inputValue(), '')
  knownWindow = false
  await input.fill('Second question')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await page.getByRole('button', { name: 'Context: 160 tokens', exact: true }).click()
  await page.getByText(/did not report its context limit/).waitFor()
  const beforeDisable = await input.inputValue()
  await mic.click()
  await page.locator('[data-chat-mic][aria-pressed=true]').waitFor()
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('filaman:model-change', { detail: {
    chat_model: 'test', chat_endpoint_id: 'local', microphone_enabled: false,
  } })))
  assert.equal(await mic.isVisible(), false, 'disabling the microphone hides it immediately')
  assert.equal(await page.evaluate(() => window.recognitions.at(-1).aborted), true)
  await page.evaluate(() => window.recognitions.at(-1).onresult({ results: [[{ transcript: 'disabled microphone' }]] }))
  assert.equal(await input.inputValue(), beforeDisable)
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('filaman:model-change', { detail: {
    chat_model: 'test', chat_endpoint_id: 'local', microphone_enabled: true,
  } })))
  assert.equal(await mic.isVisible(), true)
  // A stale browser tab cannot bypass a newly disabled microphone.
  microphoneConfig.microphone_enabled=false
  const beforeFreshCheck=await page.evaluate(()=>window.recognitions.length)
  await mic.click()
  await page.getByText('Microphone dictation is disabled in settings.',{exact:true}).waitFor()
  assert.equal(await page.evaluate(()=>window.recognitions.length),beforeFreshCheck)
  microphoneConfig.microphone_enabled=true
  await page.evaluate(()=>document.dispatchEvent(new CustomEvent('filaman:model-change',{detail:{
    chat_model:'test',chat_endpoint_id:'local',microphone_enabled:true,
  }})))
  // Browser recognition has the same one-minute capture limit.
  await page.evaluate(()=>{
    window.originalTimeout=window.setTimeout
    window.setTimeout=(callback,delay,...args)=>{
      if(delay===60000) window.captureLimit=callback
      return window.originalTimeout(callback,delay,...args)
    }
  })
  await mic.click()
  await page.getByRole('button',{name:'Stop dictation',exact:true}).waitFor()
  await page.evaluate(()=>{window.captureLimit();window.setTimeout=window.originalTimeout})
  assert.equal(await page.evaluate(()=>window.recognitions.at(-1).aborted),true)
  microphoneConfig={microphone_enabled:true,microphone_endpoint_id:'speech',microphone_endpoint_name:'Speech server',microphone_revision:'r1'}
  // Endpoint mode uploads audio for transcription, then leaves editable text unsent.
  await page.evaluate(() => {
    navigator.mediaDevices.getUserMedia=async()=>({getTracks:()=>[{stop(){}}]})
    window.MediaRecorder=class{
      static isTypeSupported(){return true}
      state='inactive'
      start(){this.state='recording'}
      stop(){this.state='inactive';this.ondataavailable?.({data:new Blob(['audio'])});this.onstop?.()}
    }
    window.AudioContext=class{async decodeAudioData(){return {duration:.25}} async close(){}}
    window.OfflineAudioContext=class{
      destination={}
      createBufferSource(){return {connect(){},start(){}}}
      async startRendering(){return {getChannelData:()=>new Float32Array([0,.5,-.5,1,-1])}}
    }
  })
  await mic.click()
  await page.getByText(/microphone source or settings changed/).waitFor()
  assert.equal(await page.evaluate(()=>window.recognitions.length),beforeFreshCheck+1,'changed source requires another click rather than browser fallback')
  await input.fill('Please add')
  const callsBeforeAudio=calls
  await mic.click()
  await page.locator('[data-chat-mic][aria-pressed=true]').waitFor()
  const stopAudio=page.getByRole('button',{name:'Stop recording and transcribe',exact:true})
  await stopAudio.waitFor()
  assert.equal(audioUploads.length,0)
  await stopAudio.click()
  await page.waitForFunction(()=>document.querySelector('[name=message]').value==='Please add Paramount 3D Enzo Red')
  assert.equal(calls,callsBeforeAudio,'audio transcription never submits a chat message')
  assert.deepEqual(audioUploads.map(({type,header,rate})=>({type,header,rate})),[{type:'audio/wav',header:'RIFF',rate:16000}])
  assert.match(audioUploads[0].url,/endpoint_id=speech&revision=r1/)
  assert.equal(await input.isEditable(),true)
  microphoneConfig={microphone_enabled:true,microphone_endpoint_id:null,microphone_endpoint_name:null,microphone_revision:'r1'}
  canCreate = false
  await page.goto('https://fila.test/plugin-page/filabrain')
  await page.getByRole('button', { name: 'Add a filament', exact: true }).click()
  assert.notEqual(await input.inputValue(), '', 'read-only users get the starter prompt without dead navigation')
  await page.goto('http://fila.test/plugin-page/filabrain')
  await input.waitFor()
  await mic.click({ force: true })
  await page.getByText(/Voice input needs HTTPS/).waitFor()
  assert.equal(await page.evaluate(() => window.recognitions.length), 0)
  const unsupported = await browser.newContext()
  await unsupported.route('**/*', fixture)
  await unsupported.addInitScript(() => { window.SpeechRecognition = window.webkitSpeechRecognition = undefined })
  const other = await unsupported.newPage()
  await other.goto('https://fila.test/plugin-page/filabrain')
  await other.getByRole('button', { name: 'Dictate a message', exact: true }).click({ force: true })
  await other.getByText(/not available in this browser/).waitFor()
  await context.close()
  await unsupported.close()
  console.log('Dictation permission gating, draft preservation, cancellation, errors, and measured context usage passed.')
} finally { await browser.close() }
