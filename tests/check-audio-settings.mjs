import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const { build } = await import(process.env.ESBUILD_MODULE || 'esbuild')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const root = fileURLToPath(new URL('..', import.meta.url))
const bundle = await build({ stdin: { contents: "import { mountSettings } from './frontend/settings'; mountSettings(); document.addEventListener('filaman:model-change', e => window.lastModelChange = e.detail)", resolveDir: root, loader: 'ts' }, bundle: true, write: false, format: 'iife', target: 'es2022' })
const fragment = await readFile(new URL('../frontend/settings.html', import.meta.url), 'utf8')
const css = await readFile(new URL('../frontend/settings.css', import.meta.url), 'utf8')
const html = `<!doctype html><html><head><style>[hidden]{display:none!important} ${css}</style></head><body>${fragment}<script>${bundle.outputFiles[0].text.replace(/<\/script/gi, '<\\/script')}</script></body></html>`
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
const origin = 'https://fila.test'
const chat = { id: 'chat', name: 'Local chat', model_url: 'http://ollama:11434', model_id: 'chat-model', has_key: false, models: ['chat-model'], hidden_models: [] }
const audio = { id: 'audio', name: 'Speech server', model_url: 'https://speech.test/v1', model_id: '', chat_enabled: false, audio_enabled: true, audio_model_id: 'whisper-1', has_key: true, models: [], hidden_models: [] }
let config = { model_endpoints: [chat, audio], default_endpoint_id: 'chat', model_id: chat.model_id, model_revision: 'revision-1', microphone_enabled: true, microphone_endpoint_id: null, search_url: '', chat_live_sources: true, supplier_sites: [], supplier_download_mib: 8 }
const saves = []
try {
  const page = await browser.newPage()
  page.setDefaultTimeout(3000)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route(origin + '/**', async route => {
    if (new URL(route.request().url()).pathname !== '/api/v1/filabrain/settings') return route.fulfill({ contentType: 'text/html', body: html })
    if (route.request().method() === 'PUT') {
      const payload = route.request().postDataJSON()
      saves.push(payload)
      config = { ...config, ...payload, model_revision: `revision-${saves.length + 1}` }
      if (payload.model_endpoints) config.model_endpoints = payload.model_endpoints.map(({ api_key, clear_key, ...entry }) => ({ ...entry, has_key: !clear_key && (!!api_key || config.model_endpoints.find(old => old.id === entry.id)?.has_key) }))
      config.model_id = config.model_endpoints.find(entry => entry.id === config.default_endpoint_id)?.model_id ?? ''
    }
    return route.fulfill({ json: config })
  })
  const source = page.locator('[name=microphone_endpoint_id]')
  const endpoint = page.locator('#research-model-endpoint')
  const save = page.locator('button[type=submit]')
  async function open() {
    await page.goto(origin)
    await page.locator('#research-model-endpoint:not(:disabled)').waitFor()
    assert.equal(await save.isDisabled(), true, 'loading settings does not make Save dirty')
  }
  async function saveChanges() {
    const before = saves.length
    await save.click()
    await page.waitForFunction(() => document.querySelector('button[type=submit]').disabled)
    await page.waitForFunction(() => !document.querySelector('[name=microphone_enabled]').disabled)
    assert.equal(saves.length, before + 1)
    assert.equal(await save.isDisabled(), true, 'saving clears dirty state')
    return saves.at(-1)
  }
  await open()
  assert.deepEqual(await source.locator('option').evaluateAll(options => options.map(option => option.value)), ['', 'audio'])
  await endpoint.selectOption('audio')
  assert.equal(await page.locator('[name=chat_enabled]').isChecked(), false)
  assert.equal(await page.locator('[name=audio_model_id]').inputValue(), 'whisper-1')
  assert.equal(await page.locator('#research-default-endpoint').isVisible(), false)
  assert.equal(await page.locator('#research-model-visibility').isVisible(), false)
  assert.equal(await page.locator('[name=model_id]').isVisible(), false)
  await page.locator('[name=audio_enabled]').uncheck()
  await save.click()
  assert.equal(saves.length, 0, 'an endpoint must retain at least one capability')
  await page.locator('[name=audio_enabled]').check()
  await page.locator('[name=audio_model_id]').fill('')
  await save.click()
  assert.equal(saves.length, 0, 'an audio endpoint needs its own transcription model')
  await page.locator('[name=audio_model_id]').fill('whisper-1')
  assert.equal(await save.isDisabled(), true)
  await source.selectOption('audio')
  assert.equal(await save.isEnabled(), true)
  let payload = await saveChanges()
  assert.equal(payload.microphone_endpoint_id, 'audio')
  assert.equal(payload.microphone_enabled, undefined, 'unchanged microphone enable is omitted')
  assert.equal(payload.model_revision, 'revision-1', 'intentional source changes include the loaded revision')
  assert.equal(payload.model_endpoints, undefined, 'source-only changes leave endpoints untouched')
  assert.deepEqual(await page.evaluate(() => [window.lastModelChange.microphone_endpoint_id, window.lastModelChange.microphone_endpoint_name, window.lastModelChange.microphone_revision]), ['audio', 'Speech server', 'revision-2'])

  // A shared server keeps independent chat and transcription model IDs.
  await page.locator('[name=chat_enabled]').check()
  await page.locator('.research-manual-model').evaluate(el => { el.open = true })
  await page.locator('[name=model_id]').fill('speech-server-chat')
  assert.equal(await page.locator('[name=audio_model_id]').inputValue(), 'whisper-1')
  await page.locator('#research-default-endpoint').click()
  payload = await saveChanges()
  assert.equal(payload.default_endpoint_id, 'audio')
  assert.equal(payload.model_endpoints.find(entry => entry.id === 'audio').model_id, 'speech-server-chat')
  assert.equal(payload.model_endpoints.find(entry => entry.id === 'audio').audio_model_id, 'whisper-1')
  await page.locator('[name=chat_enabled]').uncheck()
  payload = await saveChanges()
  assert.equal(payload.default_endpoint_id, 'chat', 'disabling Chat moves its default to a chat-capable endpoint')
  assert.equal(payload.microphone_endpoint_id, undefined, 'Chat changes omit the unchanged audio source')
  assert.equal(payload.microphone_enabled, undefined, 'Chat changes omit the unchanged microphone enable')
  assert.equal(config.microphone_endpoint_id, 'audio', 'Chat changes retain the audio source')

  // Disabling or removing the selected source must not choose browser service.
  await page.locator('[name=audio_enabled]').uncheck()
  assert.equal(await source.inputValue(), 'audio')
  assert.equal(await source.evaluate(el => el.validity.valid), false)
  const beforeInvalidSave = saves.length
  await save.click()
  assert.equal(saves.length, beforeInvalidSave)
  await page.locator('[name=audio_enabled]').check()
  assert.equal(await source.evaluate(el => el.validity.valid), true)
  assert.equal(await save.isDisabled(), true, 'reverting capabilities restores clean Save')
  await page.locator('#research-remove-endpoint').click()
  assert.equal(await source.inputValue(), 'audio')
  assert.equal(await source.evaluate(el => el.validity.valid), false)
  await source.selectOption('')
  payload = await saveChanges()
  assert.equal(payload.microphone_endpoint_id, null)
  assert.equal(payload.default_endpoint_id, 'chat', 'removal chooses another chat endpoint')

  // Add retains the name-first workflow; audio-only installations have no chat default.
  await page.locator('#research-add-endpoint').click()
  assert.equal(await page.locator('[name=endpoint_name]').evaluate(el => el === document.activeElement), true)
  await page.locator('[name=endpoint_name]').fill('Audio only')
  await page.locator('[name=model_url]').fill('https://audio.test/v1')
  await page.locator('[name=chat_enabled]').uncheck()
  await page.locator('[name=audio_enabled]').check()
  await page.locator('[name=audio_model_id]').fill('transcribe-model')
  payload = await saveChanges()
  const added = payload.model_endpoints.find(entry => entry.name === 'Audio only')
  assert.equal(added.model_id, '')
  assert.equal(added.chat_enabled, false)
  assert.equal(payload.default_endpoint_id, 'chat')
  await endpoint.selectOption('chat')
  await page.locator('#research-remove-endpoint').click()
  payload = await saveChanges()
  assert.equal(payload.default_endpoint_id, null)
  assert.equal(payload.model_endpoints.length, 1)
  assert.equal(await page.locator('#research-default-endpoint').isVisible(), false)

  // A persisted stale source stays explicit even before any endpoint edits.
  config.microphone_endpoint_id = 'removed'
  await open()
  assert.equal(await source.inputValue(), 'removed')
  assert.equal(await source.evaluate(el => el.validity.valid), false)
  await source.selectOption('')
  await saveChanges()

  // Existing deployment placeholders can still save source preferences unchanged.
  config = { ...config, model_endpoints: [{ ...chat, model_id: '', model_url: '' }], default_endpoint_id: 'chat' }
  await open()
  await page.locator('[name=microphone_enabled]').uncheck()
  payload = await saveChanges()
  assert.equal(payload.model_endpoints, undefined)
  assert.equal(payload.microphone_enabled, false)

  // Another administrator's microphone choice survives an unrelated stale save.
  config = { ...config, model_endpoints: [chat, audio], default_endpoint_id: 'chat', microphone_enabled: true,
    microphone_endpoint_id: null, model_revision: 'loaded-admin-revision', search_url: '' }
  await open()
  config = { ...config, microphone_enabled: false, microphone_endpoint_id: 'audio', model_revision: 'other-admin-revision' }
  await page.locator('#research-enable-search').click()
  await page.locator('[name=search_url]').fill('https://search.test')
  payload = await saveChanges()
  assert.equal(payload.microphone_enabled, undefined)
  assert.equal(payload.microphone_endpoint_id, undefined)
  assert.equal(payload.model_revision, undefined, 'unrelated source changes need no model revision')
  assert.equal(config.microphone_enabled, false)
  assert.equal(config.microphone_endpoint_id, 'audio')
  assert.equal(await page.locator('[name=microphone_enabled]').isChecked(), false, 'the response adopts the latest microphone preference')
  assert.equal(await source.inputValue(), 'audio', 'the response adopts the private endpoint choice')
  assert.deepEqual(await page.evaluate(() => [window.lastModelChange.microphone_enabled, window.lastModelChange.microphone_endpoint_id]), [false, 'audio'])
  const latestRevision = config.model_revision
  await page.locator('[name=microphone_enabled]').check()
  payload = await saveChanges()
  assert.equal(payload.microphone_enabled, true)
  assert.equal(payload.microphone_endpoint_id, undefined)
  assert.equal(payload.model_revision, latestRevision, 'intentional enable changes include the latest loaded revision')
  assert.deepEqual(errors, [])
  console.log('Settings chat/audio capabilities, defaults, explicit source selection, save events, and dirty Save passed.')
} finally {
  await browser.close()
}
