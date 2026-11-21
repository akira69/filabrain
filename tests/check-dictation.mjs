import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
const html = await readFile(new URL('../filabrain/page.html', import.meta.url), 'utf8')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
let calls = 0
let knownWindow = true
const fixture = route => {
  const path = new URL(route.request().url()).pathname
  if (path.startsWith('/plugin-page/')) return route.fulfill({ contentType: 'text/html', body: html })
  if (path.endsWith('/status')) return route.fulfill({ json: { enabled: true, can_chat: true, can_configure: false, chat_model: 'test' } })
  if (path.endsWith('/filabrain/models')) return route.fulfill({ json: { groups: [{ id: 'local', name: 'Test', models: ['test', 'other'] }], default_endpoint_id: 'local', default_model_id: 'test' } })
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
  await page.getByRole('button', { name: 'Stop dictation', exact: true }).waitFor()
  await page.evaluate(() => window.recognitions.at(-1).onresult({ results: [[{ transcript: 'Print PETG' }]] }))
  assert.equal(await input.inputValue(), 'Existing draft. Print PETG')
  assert.equal(calls, 0, 'dictation never auto-sends')
  await page.getByRole('button', { name: 'Stop dictation', exact: true }).click()
  await page.evaluate(() => window.recognitions.at(-1).onresult({ results: [[{ transcript: 'late result' }]] }))
  assert.equal(await input.inputValue(), 'Existing draft. Print PETG', 'late results after stop are ignored')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await page.getByText('Answer', { exact: true }).waitFor()
  await page.getByText('Context: 4% · 160 / 4,096 tokens', { exact: true }).waitFor()
  await page.getByRole('button', { name: /^Model for this conversation/ }).click()
  await page.getByRole('menuitemradio', { name: 'other', exact: true }).click()
  assert.equal(await page.locator('[data-chat-usage]').isVisible(), false, 'changing models hides the previous model’s context meter')
  await page.getByRole('button', { name: /^Model for this conversation/ }).click()
  await page.getByRole('menuitemradio', { name: 'test Default', exact: true }).click()

  await page.getByRole('button', { name: 'New chat', exact: true }).click()
  assert.equal(await page.locator('[data-chat-usage]').isVisible(), false)
  await mic.click()
  await page.evaluate(() => window.recognitions.at(-1).onerror())
  await page.getByText(/Dictation could not start/).waitFor()
  await input.fill('Draft before recording')
  await mic.click()
  await page.getByRole('button', { name: 'New chat', exact: true }).click()
  await page.evaluate(() => window.recognitions.at(-1).onresult({ results: [[{ transcript: 'wrong conversation' }]] }))
  assert.equal(await input.inputValue(), '')
  knownWindow = false
  await input.fill('Second question')
  await page.getByRole('button', { name: 'Send message', exact: true }).click()
  await page.getByText('Context: 160 tokens', { exact: true }).waitFor()
  assert.match(await page.locator('[data-chat-usage]').getAttribute('title'), /did not report/)
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
