import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

const { build } = await import(process.env.ESBUILD_MODULE || 'esbuild')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const bundle = await build({ entryPoints: [fileURLToPath(new URL('../frontend/chat-context.ts', import.meta.url))], bundle: true,
  format: 'iife', globalName: 'ChatContext', write: false, target: ['es2022'] })
const html = `<form><details data-chat-context><summary>Context</summary><label data-chat-printer-field>Printer<select data-chat-printer></select></label>
  <input data-chat-filament-search><select data-chat-filament></select><select data-chat-nozzle><option value="">None</option><option value="0.4">0.4</option></select>
  <p data-chat-context-status></p></details></form><script>${bundle.outputFiles[0].text}</script>`
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
const filament = id => ({ id, designation: `PLA ${id}`, manufacturer: { name: 'Maker' } })
const activeSlot = (ams_id = 0, slot = 1, spool_id = 7, extra = {}) => ({ ams_id, slot, active: true, empty: false, spool_id, ...extra })
try {
  let printers, display, displayStatus, held, requests, capabilities
  const context = await browser.newContext()
  await context.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: html })
    requests.push(url.pathname + url.search)
    if (url.pathname === '/api/v1/printers') return route.fulfill({ json: { items: printers, total: printers.length } })
    if (url.pathname === '/api/v1/display') return route.fulfill({ status: displayStatus, json: display })
    if (url.pathname === '/api/v1/spools/7') return route.fulfill({ json: { id: 7, filament_id: 70, filament: filament(70) } })
    if (url.pathname === '/api/v1/filaments/99') {
      if (held) await held.promise
      return route.fulfill({ json: filament(99) }).catch(() => {})
    }
    if (url.pathname === '/api/v1/filaments') {
      if (url.searchParams.get('search') === 'late') {
        if (held) await held.promise
        return route.fulfill({ json: { items: [filament(99)], total: 1 } }).catch(() => {})
      }
      return route.fulfill({ json: { items: url.searchParams.get('search') ? [filament(88)] : Array.from({ length: 25 }, (_, i) => filament(i + 1)), total: 90 } })
    }
    return route.fulfill({ status: 404, json: {} })
  })
  const page = await context.newPage()
  page.setDefaultTimeout(5000)
  const printer = page.locator('[data-chat-printer]')
  const select = page.locator('[data-chat-filament]')
  async function mount(options = {}) {
    printers = options.printers ?? [{ id: 1, name: 'Only printer', is_active: true }, { id: 2, name: 'Archived printer', is_active: false }]
    display = options.display ?? { schema_version: 3, printers: [{ id: 1, connected: true, active: { ams_id: 0, slot: 1 }, ams: [{ ams_id: 0, slots: [activeSlot()] }] }] }
    displayStatus = options.displayStatus ?? 200
    capabilities = { can_read_printers: true, can_read_spools: true, can_read_display: true, ...options.capabilities }
    held = undefined
    requests = []
    await page.goto('https://fila.test/')
    await page.evaluate(({ capabilities, enabled }) => { window.context = ChatContext.mountChatContext(document.querySelector('form'), capabilities, enabled) }, { capabilities, enabled: options.enabled ?? true })
  }
  await mount()
  await page.waitForFunction(() => document.querySelector('[data-chat-filament]').options.length === 26)
  assert.equal(await page.locator('details').getAttribute('open'), null, 'choices preload before opening the caret')
  assert.equal(await printer.inputValue(), '1', 'one active printer is the default')
  assert.equal(await printer.locator('option').count(), 3, 'inactive printers remain available')
  assert.equal(await select.inputValue(), '', 'loaded filament is not silently selected')
  await page.locator('[data-chat-context]').evaluate(el => { el.open = true })
  await page.locator('[data-chat-use-loaded-filament]').click()
  assert.equal(await select.inputValue(), '70')
  await page.locator('[data-chat-filament-search]').fill('beyond first page')
  await page.waitForFunction(() => [...document.querySelector('[data-chat-filament]').options].some(o => o.value === '88'))
  assert.equal(await select.inputValue(), '70', 'search preserves the selected filament')
  await select.selectOption('88')
  await page.evaluate(() => window.context.restore({ printer_id: 2, filament_id: 99, nozzle_mm: 0.4 }))
  assert.deepEqual(await page.evaluate(() => window.context.snapshot()), { printer_id: 2, filament_id: 99, nozzle_mm: 0.4 })
  await page.evaluate(() => window.context.reset())
  assert.deepEqual(await page.evaluate(() => window.context.snapshot()), { printer_id: 1 })
  await page.evaluate(() => window.context.restore({ printer_id: 2, filament_id: 4 }))
  assert.deepEqual(await page.evaluate(() => window.context.snapshot()), { printer_id: 2, filament_id: 4 }, 'inactive saved printers can be restored')

  await mount({ display: { printers: [{ id: 1, connected: true, active: { ams_id: 1, slot: 1 }, ams: [
    { ams_id: 0, slots: [{ ...activeSlot(), active: false }] }, { ams_id: 1, slots: [activeSlot(1)] },
  ] }] } })
  await page.waitForFunction(() => document.querySelector('[data-chat-use-loaded-filament]'))
  assert.equal(await select.inputValue(), '', 'a uniquely active slot in another AMS is only suggested')

  await mount({ printers: [{ id: 1, name: 'A', is_active: true }, { id: 2, name: 'B', is_active: true }] })
  await page.waitForFunction(() => document.querySelector('[data-chat-printer]').options.length === 3)
  assert.equal(await printer.inputValue(), '', 'multiple printers need an explicit choice')
  assert.equal(await page.locator('[data-chat-use-loaded-filament]').count(), 0)
  for (const options of [
    { displayStatus: 404 }, { displayStatus: 403 }, { capabilities: { can_read_display: false } },
    { display: { printers: [{ id: 1, connected: false, ams: [{ ams_id: 0, slots: [activeSlot()] }] }] } },
    { display: { printers: [{ id: 1, connected: true, active: { ams_id: 0, slot: 1 }, ams: [{ ams_id: 0, slots: [activeSlot()] }, { ams_id: 1, slots: [activeSlot(1)] }] }] } },
    { display: { printers: [{ id: 1, connected: true, ams: [{ ams_id: 0, slots: [activeSlot(0, 1, 7, { extruder: 'left' }), activeSlot(0, 2, 8, { extruder: 'right' })] }] }] } },
    { capabilities: { can_read_spools: false } },
  ]) {
    await mount(options)
    await page.waitForFunction(() => document.querySelector('[data-chat-filament]').options.length === 26)
    await page.waitForTimeout(80)
    assert.equal(await page.locator('[data-chat-use-loaded-filament]').count(), 0, 'unavailable, forbidden, disconnected, and ambiguous feeds do not suggest a filament')
    if (options.capabilities?.can_read_display === false) assert.ok(!requests.some(path => path.includes('/display')))
    if (options.capabilities?.can_read_spools === false) assert.ok(!requests.some(path => path.includes('/spools/')))
  }
  await mount({ capabilities: { can_read_printers: false } })
  await page.waitForFunction(() => document.querySelector('[data-chat-filament]').options.length === 26)
  assert.ok(!requests.some(path => path.includes('/printers') || path.includes('/display')))
  assert.equal(await printer.inputValue(), '')
  assert.equal(await page.locator('[data-chat-printer-field]').isVisible(), false)
  await mount({ enabled: false })
  await page.waitForTimeout(80)
  assert.deepEqual(requests, [], 'research-only mode does not fetch context')

  await mount()
  await page.waitForFunction(() => document.querySelector('[data-chat-filament]').options.length === 26)
  await page.locator('details').evaluate(el => { el.open = true })
  held = { promise: null, release: null }
  held.promise = new Promise(resolve => { held.release = resolve })
  await page.locator('[data-chat-filament-search]').fill('late')
  await page.waitForTimeout(250)
  await select.selectOption('2')
  held.release()
  await page.waitForTimeout(100)
  assert.equal(await select.inputValue(), '2', 'late search cannot replace a manual choice')
  assert.equal(await select.locator('option[value="99"]').count(), 0, 'late search is discarded after manual choice')
  held = { promise: null, release: null }
  held.promise = new Promise(resolve => { held.release = resolve })
  await page.evaluate(() => { window.restoring = window.context.restore({ filament_id: 99 }) })
  await select.selectOption('3')
  held.release()
  await page.evaluate(() => window.restoring)
  assert.equal(await select.inputValue(), '3', 'late restore cannot replace a manual choice')
  await context.close()
  console.log('Context preload, search, permission fallbacks, loaded-spool ambiguity, reset, restore and late-response checks passed.')
} finally { await browser.close() }
