import assert from 'node:assert/strict'
import { readFile, mkdir } from 'node:fs/promises'
const html = await readFile(new URL('../filabrain/page.html', import.meta.url), 'utf8')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
const requests = []
let canCreate = true
let release, started
const held = new Promise(resolve => release = resolve)
const running = new Promise(resolve => started = resolve)
const proposal = { target: { mode: 'create', identity: {} }, suggestions: [
  { field: 'designation', value: 'Intake PLA', sources: [{ title: 'Maker', url: 'https://maker.example/pla', evidence: 'PLA' }] },
  { field: 'material_type', value: 'PLA', sources: [] },
  { field: 'diameter_mm', value: 1.75, sources: [] },
], issues: [] }
const rich = { target: { mode: 'create', identity: {} }, suggestions: [
 ...proposal.suggestions, { field: 'manufacturer_id', value: 1, sources: [] },
 { field: 'manufacturer_sku', value: 'C123', sources: [] },
 { field: 'manufacturer_color_name', value: 'Enzo Red', sources: [] },
 { field: 'raw_material_weight_g', value: 1000, sources: [] },
], issues: [{ code: 'manufacturer_color_reference', name: 'RAL 3020', reference_code: 'RAL 3020', url: 'https://maker.example/real' }],
 color_previews: [{ code: 'RAL 3020', name: 'Traffic red', hex_code: '#AB1519', source: 'https://www.ral-farben.de/red', manufacturer_source: 'https://maker.example/real', matches: [{ id: 7, name: 'Traffic red', hex_code: '#AB1519' }] }],
 intake_fields: { manufacturer_name: 'Paramount 3D', manufacturer_id: 1, designation: 'Intake PLA', material_type: 'PLA', manufacturer_sku: 'C123', raw_material_weight_g: 1000 },
 spool_profiles: [{ id: 'filamandb-735', title: 'Paramount design', source: 'FilaManDB', url: 'https://db.filaman.app/api/v1/spool-profiles/735', empty_weight_g: 170, width_mm: 66, net_weight_g: 1000 }] }
try {
 const context = await browser.newContext()
 await context.route('https://fila.test/**', async route => {
  const path = new URL(route.request().url()).pathname
  if (path.startsWith('/plugin-page/')) return route.fulfill({ contentType: 'text/html', body: html })
  if (path.endsWith('/status')) return route.fulfill({ json: { enabled: true, can_chat: true, can_create: canCreate, can_create_color: canCreate, can_update: canCreate, can_read_spools: true, can_configure: true } })
  if (path.endsWith('/models')) return route.fulfill({ json: { groups: [], default_endpoint_id: null, default_model_id: null } })
  if (path.endsWith('/me')) return route.fulfill({ json: { id: 1 } })
  if (path.endsWith('/batches')) return route.fulfill({ json: [] })
  if (path.includes('/agent/')) {
   const body = route.request().postDataJSON(); requests.push({ path, body })
   if (body.query === 'slow') { started(); await held }
   if (body.query === 'denied') return route.fulfill({ status: 403, json: { detail: 'Spool access denied' } })
   if (body.query === 'outage') return route.fulfill({ json: { reply: 'No verified fields yet.', action: { page: 'intake' }, proposal: {
    target: { mode: 'create', identity: {} }, suggestions: [], issues: [{ code: 'unavailable_source', source: 'Manufacturer', reason: 'HTTP 403 (access denied)' }, { code: 'search_incomplete' }],
   }, evidence: [], notices: ['product_lookup_incomplete'], complete: false } })
   if (body.query === 'discover') return route.fulfill({ json: { reply: 'Choose the matching product', action: { page: 'intake' }, proposal: {
    target: { mode: 'create', identity: {} }, suggestions: [], discovery_sku: 'C123', issues: [{ code: 'possible_match', name: 'Paramount red candidate', product_url: 'https://maker.example/real', source: 'Maker', identity: { sku: 'C123' } }],
   }, evidence: [], notices: [], requires_product_choice: true } })
   if (body.product_url === 'https://maker.example/real') return route.fulfill({ json: { reply: 'Review the chosen product', action: { page: 'intake' }, proposal: rich, evidence: [], notices: [], spool_profiles: rich.spool_profiles } })
   return route.fulfill({ json: { reply: 'Reviewed ' + (body.query || 'library'), action: path.endsWith('/intake') ? { page: 'intake' } : null,
    proposal, evidence: [{ kind: 'stock', id: 'stock-1', title: 'PETG roll', url: '/filaments/1', text: 'Unknown weight needs measurement.' }], notices: [],
    coverage: { records_checked: 1, matching_records: 3, page: body.page || 1, has_more: (body.page || 1) < 3 }, complete: false } })
  }
  if (path.endsWith('/proposals') || route.request().method() !== 'GET') throw new Error('Unexpected research or write: ' + path)
  if (path.endsWith('/colors/7')) return route.fulfill({ json: { id: 7, name: 'Traffic red', hex_code: '#AB1519' } })
  if (path.endsWith('/manufacturers')) return route.fulfill({ json: { items: [{ id: 1, name: 'Paramount 3D' }], total: 1 } })
  if ( path.endsWith('/colors') || path.endsWith('/filaments')) return route.fulfill({ json: { items: [], total: 0 } })
  return route.fulfill({ status: 404, json: {} })
 })
 const page = await context.newPage(); page.setDefaultTimeout(5000)
 await page.setViewportSize({ width: 390, height: 844 })
 await page.goto('https://fila.test/plugin-page/filabrain')
 const chat = page.locator('[data-view=chat]')
 const task = chat.getByRole('combobox', { name: 'Task', exact: true })
 await task.selectOption('job-fit')
 const input = chat.locator('textarea')
 await input.fill('outdoor PETG bracket')
 await chat.getByRole('spinbutton', { name: 'Required filament (g)' }).fill('280')
 await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByText('Reviewed outdoor PETG bracket', { exact: true }).waitFor()
 assert.equal(await chat.locator('[data-chat-usage]').isVisible(), false, 'Read tasks do not claim model context usage')
 assert.equal(requests[0].body.required_g, 280)
 assert.equal(requests[0].path, '/api/v1/filabrain/agent/job-fit')
 await chat.getByText('Unknown weight needs measurement.').waitFor()
 await task.selectOption('stock'); await input.fill('PETG')
 await chat.getByRole('spinbutton', { name: 'Target per filament (g)' }).fill('2000')
 await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByText('Reviewed PETG', { exact: true }).waitFor()
 assert.equal(requests.at(-1).body.target_g, 2000)
 if (process.env.SCREENSHOTS_DIR) {
  await mkdir(process.env.SCREENSHOTS_DIR, { recursive: true })
  for (const width of [390, 1280]) {
   await page.setViewportSize({ width, height: 900 })
   await page.screenshot({ path: process.env.SCREENSHOTS_DIR + `/chat-tasks-${width}.png` })
  }
  await page.setViewportSize({ width: 390, height: 844 })
 }

 await page.getByRole('button', { name: 'New chat', exact: true }).click()
 await task.selectOption('library-health')
 await input.fill('slow'); await chat.getByRole('button', { name: 'Send message', exact: true }).click(); await running
 await page.getByRole('button', { name: 'New chat', exact: true }).click()
 await task.selectOption('intake'); await input.fill('SKU_42')
 await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByText('Reviewed SKU_42', { exact: true }).waitFor(); release()
 assert.equal(await chat.getByText('Reviewed slow', { exact: true }).count(), 0)
 await chat.getByRole('button', { name: 'Review filament draft', exact: true }).click()
 const research = page.locator('[data-view=new]')
 await research.getByText('Intake PLA', { exact: false }).first().waitFor()
 assert.equal(requests.filter(r => r.path.endsWith('/intake')).length, 1)
 assert.equal(await research.locator('[data-chat-task]:visible').count(), 0)
 await page.getByRole('tab', { name: 'Chat', exact: true }).click()
 await page.locator('[data-chat-history]:visible summary').click()
 await page.locator('[data-chat-history-list]:visible').getByRole('button', { name: 'outdoor PETG bracket' }).click()
 assert.equal(await task.inputValue(), 'stock')
 assert.equal(await chat.getByRole('spinbutton', { name: 'Target per filament (g)' }).inputValue(), '2000')
 await task.selectOption('library-health'); await input.fill('denied')
 await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByText('Spool access denied', { exact: true }).waitFor()
 assert.equal(await input.inputValue(), 'denied')
 for (const width of [320, 390, 1280]) {
  await page.setViewportSize({ width, height: 900 })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'overflow at ' + width)
 }
 await page.getByRole('button', { name: 'New chat', exact: true }).click()
 await task.selectOption('intake'); await input.fill('outage')
 await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByText(/HTTP 403 \(access denied\)/).waitFor()
 assert.equal(await chat.getByRole('button', { name: 'Review filament draft', exact: true }).count(), 0)
 // Continue the same filter/options through bounded pages and preserve report coverage.
 await task.selectOption('stock'); await input.fill('PETG')
 await chat.getByRole('spinbutton', { name: 'Target per filament (g)' }).fill('2000')
 await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByRole('button', { name: 'Next page', exact: true }).last().click()
 await page.waitForFunction(() => !document.querySelector('[data-chat-running]'))
 assert.equal(requests.at(-1).body.page, 2)
 assert.equal(requests.at(-1).body.query, 'PETG')
 assert.equal(requests.at(-1).body.target_g, 2000)
 await chat.getByText(/Page 2/).last().waitFor()
 // A read-only user can inspect and select candidates without opening creation.
 canCreate = false
 const reader = await context.newPage(); await reader.goto('https://fila.test/plugin-page/filabrain')
 const readChat = reader.locator('[data-view=chat]')
 await readChat.getByRole('combobox', { name: 'Task', exact: true }).selectOption('intake')
 await readChat.locator('textarea').fill('discover')
 await readChat.getByRole('button', { name: 'Send message', exact: true }).click()
 await readChat.getByText('Paramount red candidate', { exact: true }).waitFor()
 await readChat.getByRole('button', { name: 'Research this page', exact: true }).click()
 await readChat.getByText('Review the chosen product', { exact: true }).waitFor()
 assert.equal(await readChat.getByRole('button', { name: 'Review filament draft', exact: true }).count(), 0)
 await reader.close()
 // The full intake handoff reuses publisher data and retains the spool through accepting identity fields.
 canCreate = true
 await page.getByRole('button', { name: 'New chat', exact: true }).click()
 await task.selectOption('intake'); await input.fill('discover')
 await chat.getByRole('button', { name: 'Send message', exact: true }).click()
 await chat.getByRole('button', { name: 'Research this page', exact: true }).click()
 await chat.getByText('Review the chosen product', { exact: true }).waitFor()
 await chat.getByRole('button', { name: 'Review filament draft', exact: true }).click()
 await research.locator('[data-draft-swatches]').getByText('#AB1519', { exact: true }).waitFor()
 for (const box of await research.locator('.proposal-row input[type=checkbox]').all()) await box.check()
 await research.getByRole('button', { name: 'This matches: Paramount design', exact: true }).click()
 await research.locator('[name=swatch_choice]').selectOption('reuse:7')
 await research.getByRole('button', { name: 'Use swatch', exact: true }).click()
 await research.getByRole('button', { name: 'Save new filament', exact: true }).click()
 const confirm = research.getByRole('dialog')
 await confirm.filter({ has: page.locator('#create-preview') }).waitFor()
 assert.match(await research.locator('#create-preview').innerText(), /170/)
 assert.match(await research.locator('#create-preview').innerText(), /Traffic red/)
 await research.locator('#create-cancel').click()
 console.log('Agent tasks passed: no-model reads, grams, evidence, late/history/error safety, intake handoff, responsive layout; no writes.')
} finally { release?.(); await browser.close() }
