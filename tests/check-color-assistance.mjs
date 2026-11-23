import assert from 'node:assert/strict'
import { readFile, mkdir } from 'node:fs/promises'
const html = await readFile(new URL('../filabrain/page.html', import.meta.url), 'utf8')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
const context = await browser.newContext()
const requests = [], stages = [], opens = []
const preview = { code: 'RAL 3020', name: 'Traffic red', canonical_name: 'Traffic red (RAL 3020)', hex_code: '#AB1519', source: 'https://www.ral-farben.de/red', manufacturer_source: 'https://maker.example/red', matches: [{ id: 7, name: 'Existing red', hex_code: '#AB1519' }] }
let draft = { name: 'My red', hex_code: '#FF000080' }
let rejectStage = false
let releaseOpen
const openGate = new Promise(resolve => releaseOpen = resolve)
try {
 await context.route('https://fila.test/**', async route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/host') return route.fulfill({ contentType: 'text/html', body: '<iframe title="FilaBrain" src="/plugin-page/filabrain?color_host=editor" style="border:0;width:100%;height:95vh"></iframe>' })
  if (path.startsWith('/plugin-page/')) return route.fulfill({ contentType: 'text/html', body: html })
  if (path.endsWith('/status')) return route.fulfill({ json: { enabled: true, can_chat: true, can_create: true, can_update: true, can_configure: false } })
  if (path.endsWith('/models')) return route.fulfill({ json: { groups: [], default_endpoint_id: null, default_model_id: null } })
  if (path.endsWith('/me')) return route.fulfill({ json: { id: 1 } })
  if (path.endsWith('/agent/color-intake')) {
   const body = route.request().postDataJSON(); requests.push(body)
   return route.fulfill({ json: { reply: 'Review the color.', action: { page: 'color' }, proposal: { target: { mode: 'create', identity: {} }, suggestions: body.query === 'catalog' ? [{ field: 'manufacturer_color_name', value: 'Red (RAL 3020)', sources: [{ title: 'Open Filament Database', url: 'https://catalog.example/product-red', evidence: 'Product Red: RAL 3020 <sample>' }] }, { field: 'diameter_mm', value: 1.75, sources: [] }] : [], issues: body.query === 'discover' ? [{ code: 'possible_match', name: 'Red product', product_url: 'https://maker.example/red' }] : [], color_previews: body.query === 'discover' ? [] : [{ ...preview, manufacturer_source: body.query === 'catalog' ? null : preview.manufacturer_source, hex_code: body.query === 'unknown' ? null : preview.hex_code }] }, evidence: [], notices: [] } })
  }
  if (route.request().method() !== 'GET') throw Error('Unexpected write: ' + path)
  return route.fulfill({ json: { items: [], total: 0 } })
 })
 const page = await context.newPage(); page.setDefaultTimeout(7000)
 await page.exposeFunction('colorBridge', async message => {
  if (message.type === 'filaman:color-context') return { ...message, mode: 'edit', color_id: 5, usage_count: 3, fields: draft, capabilities: { color_staging: true } }
  if (message.type === 'filaman:color-stage') {
   stages.push(message)
   if (rejectStage) return { ...message, staged: [], skipped: ['name', 'hex_code'] }
   draft = { name: message.fields.name, hex_code: message.fields.hex_code.slice(0, 7) + draft.hex_code.slice(7) }
   return { ...message, staged: ['name', 'hex_code'], skipped: [], fields: draft }
  }
  if (message.type === 'filaman:color-open') { opens.push(message); await openGate; return { ...message, opened: false } }
 })
 await page.addInitScript(() => {
  if (parent !== window) return
  window.addEventListener('message', async event => {
   if (event.origin !== location.origin || !event.data?.type?.startsWith('filaman:color-')) return
   const reply = await window.colorBridge(event.data)
   event.source.postMessage(reply, event.origin)
   if (event.data.type === 'filaman:color-open') document.documentElement.dataset.colorOpened = 'true'
  })
 })
 await page.goto('https://fila.test/host')
 const frame = page.frameLocator('iframe')
 const chat = frame.locator('[data-view=chat]')
 const task = chat.getByRole('combobox', { name: 'Task', exact: true })
 await task.waitFor()
 assert.equal(await task.inputValue(), 'color')
 assert.deepEqual(await task.locator('option').evaluateAll(options => options.map(option => option.value)), ['chat', 'color'])
 const input = chat.locator('textarea')
 assert.equal(await input.inputValue(), 'My red')
 await chat.getByText(/3 filaments/).first().waitFor()
 const longUrl = 'https://maker.example/red?variant=' + 'x'.repeat(500)
 await input.fill(''); await input.focus(); await page.keyboard.insertText(longUrl)
 assert.equal(await input.inputValue(), longUrl, 'Product URLs must not be truncated')
 await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByText('Traffic red (RAL 3020)', { exact: true }).waitFor()
 assert.equal(requests.at(-1).product_url, longUrl)
 await frame.getByRole('button', { name: 'New chat', exact: true }).click()
 await input.fill('x'.repeat(401)); await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByText(/Use up to 400 characters/).waitFor()
 assert.equal(requests.length, 1, 'Overlong clues must not be sent')
 await input.fill('RAL 3020'); await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByText('Traffic red (RAL 3020)', { exact: true }).waitFor()
 assert.equal(stages.length, 0, 'Research never automatically stages')
 assert.equal(await chat.locator('[data-chat-evidence]').count(), 0, 'No empty sources section')
 await chat.getByRole('button', { name: 'Use in color form', exact: true }).click()
 await chat.getByText('Staged in the color form. Review and click Save in FilaMan.', { exact: true }).waitFor()
 assert.deepEqual(stages[0].expected, { name: 'My red', hex_code: '#FF000080' })
 assert.deepEqual(stages[0].fields, { name: 'Traffic red (RAL 3020)', hex_code: '#AB1519' })
 assert.equal(draft.hex_code, '#AB151980')
 await chat.getByRole('button', { name: 'Open existing swatch: Existing red', exact: true }).click()
 await page.waitForTimeout(3700)
 assert.equal(await chat.getByText(/editor did not respond/).count(), 0, 'Host confirmation may take human time')
 releaseOpen()
 await page.waitForFunction(() => document.documentElement.dataset.colorOpened === 'true')
 assert.equal(opens.length, 1)
 // Explicit candidate choice stays on the color-only service.
 await input.fill('discover'); await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByRole('button', { name: 'Research this page', exact: true }).click()
 await chat.getByText('Traffic red (RAL 3020)', { exact: true }).last().waitFor()
 assert.equal(requests.at(-1).product_url, 'https://maker.example/red')
 // Refused stale changes stay unsaved and can be researched again.
 rejectStage = true
 await chat.getByRole('button', { name: 'Use in color form', exact: true }).last().click()
 await chat.getByText(/color form changed/).last().waitFor()
 await input.fill('unknown'); await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByText(/Publisher screen preview unavailable/).waitFor()
 assert.equal(await chat.getByRole('button', { name: 'Use in color form', exact: true }).last().isDisabled(), true)
 await input.fill('catalog'); await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 const productEvidence = chat.locator('details').filter({ has: page.getByText('Product / catalog evidence', { exact: true }) })
 await productEvidence.locator('summary').click()
 assert.equal(await productEvidence.getByRole('link', { name: 'Open Filament Database', exact: true }).getAttribute('href'), 'https://catalog.example/product-red')
 await productEvidence.getByText('Product Red: RAL 3020 <sample>', { exact: true }).waitFor()
 assert.equal(await productEvidence.locator('sample').count(), 0, 'Evidence must remain escaped text')
 assert.equal(await productEvidence.locator('dt').count(), 1, 'Color evidence must not repeat unrelated filament fields')
 assert.equal(await chat.locator('.color-assistant-review').last().getByRole('link', { name: 'Manufacturer', exact: true }).count(), 0, 'Catalog evidence is not a manufacturer assertion')
 for (const width of [320, 390, 768, 1280]) {
  await page.setViewportSize({ width, height: 900 })
  assert.equal(await frame.locator('html').evaluate(node => node.scrollWidth <= innerWidth), true, 'overflow at ' + width)
  if (process.env.SCREENSHOTS_DIR) {
   await mkdir(process.env.SCREENSHOTS_DIR, { recursive: true })
   await page.screenshot({ path: process.env.SCREENSHOTS_DIR + `/color-assistance-${width}.png` })
  }
 }
 await task.selectOption('chat')
 assert.equal(await task.inputValue(), 'chat', 'General chat remains available')
 // Ordinary sidebar use never exposes a host-only staging task.
 const ordinary = await context.newPage()
 await ordinary.goto('https://fila.test/plugin-page/filabrain')
 assert.equal(await ordinary.locator('[data-view=chat] [data-chat-task] option[value=color]').count(), 0)
 console.log('PASS color assistance: exact preview, source links, explicit staging, alpha, reuse, stale/unknown, product choice, Chat and responsive layout')
} finally { releaseOpen?.(); await browser.close() }
