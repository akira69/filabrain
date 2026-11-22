import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const { build } = await import(process.env.ESBUILD_MODULE || 'esbuild')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const root = fileURLToPath(new URL('../', import.meta.url))
const read = name => readFile(new URL('../frontend/' + name, import.meta.url), 'utf8')
const result = await build({ entryPoints: [root + 'frontend/main.ts'], bundle: true, format: 'iife', write: false, target: ['es2022'] })
const css = (await Promise.all(['page', 'chat', 'settings', 'batches', 'docs', 'context-usage', 'tabs'].map(name => read(name + '.css')))).join('\n')
const fragments = Object.fromEntries(await Promise.all(['chat', 'settings', 'batches', 'docs', 'docs-button', 'model-picker'].map(async name => [name, await read(name + '.html')])))
let html = (await read('page.html')).replace('<!-- styles -->', () => css)
for (const [name, fragment] of Object.entries(fragments)) html = html.replaceAll(`<!-- ${name} -->`, () => fragment)
const mark = (await readFile(new URL('../filabrain/icon.svg', import.meta.url), 'utf8')).replace('<svg ', '<svg class="filabrain-mark" aria-hidden="true" ')
html = html.replaceAll('<!-- mark -->', () => mark).replace('<!-- script -->', () => result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script'))
const draft = await build({ stdin: { contents: `import { mountSwatchReview } from './frontend/shared/swatch-review'; window.mountDraft = review => mountSwatchReview(document.querySelector('section'), {load: async () => review, choose: async (...args) => { window.choices = args }});`, resolveDir: root }, bundle: true, format: 'iife', write: false })
const origin = 'https://fila.test'
const batch = { id: 'audit', mode: 'colors', status: 'finished', total: 1, created_at: '2026-01-01T00:00:00Z', finished_at: '2026-01-01T00:00:01Z', items: [{ id: 12, filament_id: 10, live_filament_id: 10, status: 'no_change', decision: 'pending', revision: 1, decision_revision: 0, identity: { designation: 'Decepticon Purple' }, color_references: ['RAL 4007'], proposals: [], issues: [], approved_ids: [], changed_fields: [], applied_at: null }] }
const change = { color_id: 1, current_name: 'Marketing Purple', proposed_name: 'Purple violet (RAL 4007)', usage_count: 8, token: 'rename-token' }
const baseReview = { current: [{ id: 1, name: change.current_name, hex_code: '#593D67' }], can_save: true, can_assign: true, can_create: true, can_update: true, references: [{ code: 'RAL 4007', name: 'Purple violet', hex_code: '#44263C', manufacturer_source: 'https://manufacturer.test/purple', source: 'https://ral-farben.de/ral-4007-purple-violet', saved: true, matches: [{ id: 2, name: 'Existing violet', hex_code: '#44263C' }], token: 'assignment-token', name_changes: [change] }] }
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
try {
  const context = await browser.newContext()
  let review = structuredClone(baseReview), failure = null, loadFailure = false, reads = 0, created = false
  const posts = [], modes = [], errors = [], decisions = [], mutationPaths = []
  await context.route(origin + '/**', async route => {
    const req = route.request(), path = new URL(req.url()).pathname
    if (req.method() !== 'GET') mutationPaths.push(path)
    if (path === '/draft-fixture') return route.fulfill({ contentType: 'text/html', body: `<style>${css}</style><section><div data-swatch-content></div><p data-swatch-status tabindex="-1" role="status"></p></section><script>${draft.outputFiles[0].text}</script>` })
    if (path.startsWith('/plugin-page/')) return route.fulfill({ contentType: 'text/html', body: html })
    if (path.endsWith('/swatches')) {
      if (req.method() === 'GET') {
        reads++
        return loadFailure ? route.fulfill({ status: 503, json: {} }) : route.fulfill({ json: review })
      }
      posts.push(req.postDataJSON())
      if (failure) return route.fulfill({ status: failure, json: { detail: failure === 409 ? 'The swatch changed. Review again.' : 'Could not rename this color.' } })
      if (req.postDataJSON().action === 'rename') {
        review = structuredClone(baseReview)
        review.current[0].name = change.proposed_name
        review.references[0].name_changes = []
      }
      return route.fulfill({ json: {} })
    }
    if (path.endsWith('/decisions')) {
      const payload = req.postDataJSON()
      decisions.push(payload)
      for (const decision of payload.items) {
        const item = batch.items.find(item => item.id === decision.item_id)
        item.decision = decision.decision
        item.approved_ids = decision.approved_ids
        item.decision_revision++
      }
      return route.fulfill({ json: batch })
    }
    if (path.endsWith('/batches/audit')) return route.fulfill({ json: batch })
    if (path.endsWith('/batches')) {
      if (req.method() === 'POST') { modes.push(req.postDataJSON()); created = true; return route.fulfill({ json: batch }) }
      return route.fulfill({ json: created ? [batch] : [] })
    }
    if (path.endsWith('/filaments')) return route.fulfill({ json: { items: [{ id: 10, manufacturer_id: 7, designation: 'Decepticon Purple', material_type: 'PLA', record_token: 'a'.repeat(32) }], total: 1 } })
    if (path.endsWith('/status')) return route.fulfill({ json: { enabled: true, can_chat: true, can_create: true, can_update: true, can_configure: true } })
    if (path.endsWith('/settings')) return route.fulfill({ json: { model_endpoints: [], supplier_sites: [] } })
    if (path.endsWith('/models')) return route.fulfill({ json: { groups: [] } })
    if (path.endsWith('/me')) return route.fulfill({ json: { id: 1 } })
    if (path.endsWith('/manufacturers') || path.endsWith('/colors')) return route.fulfill({ json: { items: [], total: 0 } })
    return route.fulfill({ status: 404, json: {} })
  })
  const page = await context.newPage()
  page.setDefaultTimeout(5000)
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(origin + '/plugin-page/filabrain?tab=library')
  await page.getByRole('radio', { name: 'Color audit', exact: true }).check()
  await page.getByRole('checkbox', { name: /Decepticon Purple/ }).check()
  await page.getByRole('button', { name: 'Start research', exact: true }).click()
  await page.locator('[data-swatch-review]').waitFor()
  assert.equal(modes[0].mode, 'colors')
  assert.deepEqual(modes[0].filament_ids, [10])
  await page.locator('[data-batch-progress]').filter({ hasText: 'Color audit' }).waitFor()
  const panel = page.locator('[data-swatch-review]')
  const rename = panel.getByRole('button', { name: 'Rename shared swatch', exact: true })
  await rename.waitFor()
  assert.match(await panel.textContent(), /Marketing Purple.*Purple violet \(RAL 4007\)/s)
  assert.match(await panel.textContent(), /Filaments using this swatch: 8/)
  assert.match(await panel.textContent(), /#593D67/)
  assert.equal(await panel.getByRole('link', { name: 'Palette source', exact: true }).getAttribute('href'), baseReview.references[0].source)
  assert.equal(posts.length, 0)
  await rename.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: 'Rename shared swatch', exact: true })
  await dialog.waitFor()
  assert.match(await dialog.textContent(), /everywhere/)
  assert.match(await dialog.textContent(), /HEX.*manufacturer/s)
  assert.equal(await dialog.getByRole('button', { name: 'Cancel', exact: true }).evaluate(el => el === document.activeElement), true)
  await page.keyboard.press('Tab')
  assert.equal(await dialog.getByRole('button', { name: 'Rename everywhere', exact: true }).evaluate(el => el === document.activeElement), true)
  await page.keyboard.press('Shift+Tab')
  assert.equal(await dialog.getByRole('button', { name: 'Cancel', exact: true }).evaluate(el => el === document.activeElement), true, 'both confirmation actions are keyboard accessible')
  await page.keyboard.press('Escape')
  assert.equal(posts.length, 0, 'opening and canceling must not send a request')
  assert.equal(await rename.evaluate(el => el === document.activeElement), true, 'cancel restores keyboard focus')
  await rename.click()
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  assert.equal(posts.length, 0)
  for (const width of [320, 390, 1280]) {
    await page.setViewportSize({ width, height: 900 })
    await rename.click()
    await dialog.waitFor()
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width)
    const bounds = await dialog.boundingBox()
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1)
    assert.equal(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth), true)
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  }
  await page.evaluate(() => { for (const key of ['colors', 'filaments', 'filament-filter-options', 'spool-filter-options']) localStorage.setItem('filaman-cache:' + key, 'cached') })
  const initialReads = reads
  await rename.click()
  await dialog.getByRole('button', { name: 'Rename everywhere', exact: true }).click()
  await panel.locator('[data-swatch-status]').filter({ hasText: 'Shared swatch renamed.' }).waitFor()
  assert.deepEqual(posts[0], { code: 'RAL 4007', token: 'rename-token', action: 'rename', color_id: 1, color_name: 'Marketing Purple' })
  assert.ok(reads > initialReads, 'success loads fresh names and tokens')
  assert.deepEqual(await page.evaluate(() => ['colors', 'filaments', 'filament-filter-options', 'spool-filter-options'].map(key => localStorage.getItem('filaman-cache:' + key))), [null, null, null, null])
  assert.equal(await rename.count(), 0)
  assert.equal(await panel.locator('[data-swatch-status]').evaluate(el => el === document.activeElement), true)
  await page.getByRole('link', { name: 'All runs', exact: true }).click()
  await page.getByRole('link', { name: 'Color audit', exact: true }).waitFor()
  async function reloadReview(value = baseReview) {
    review = structuredClone(value)
    await page.goto(origin + '/plugin-page/filabrain?tab=batches&id=audit')
    await panel.getByText('#593D67', { exact: true }).waitFor()
  }
  for (const status of [500, 403, 409]) {
    await reloadReview()
    failure = status
    await rename.click()
    if (status === 409) review.references[0].name_changes = []
    if (status === 403) review.can_update = false
    await dialog.getByRole('button', { name: 'Rename everywhere', exact: true }).click()
    await panel.locator('[data-swatch-status]').filter({ hasText: status === 409 ? 'The swatch changed.' : 'Could not rename this color.' }).waitFor()
    assert.equal(await dialog.count(), 0)
    if ([403, 409].includes(status)) assert.equal(await rename.count(), 0, 'stale or unauthorized suggestions are replaced')
    else {
      await rename.click()
      assert.equal(await panel.locator('[data-swatch-status]').textContent(), '', 'a new review clears the previous error')
      await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
    }
  }
  failure = 409
  await reloadReview()
  await rename.click()
  loadFailure = true
  await dialog.getByRole('button', { name: 'Rename everywhere', exact: true }).click()
  await panel.locator('[data-swatch-status]').filter({ hasText: 'Could not load swatches.' }).waitFor()
  assert.equal(await rename.count(), 0, 'a failed refresh cannot leave the stale token actionable')
  loadFailure = false
  failure = null
  const longReview = structuredClone(baseReview)
  longReview.current[0].name = 'Marketing' + 'X'.repeat(220)
  longReview.references[0].name_changes[0].current_name = longReview.current[0].name
  longReview.references[0].name_changes[0].proposed_name = 'Purple' + 'Y'.repeat(220) + ' (RAL 4007)'
  longReview.references[0].source += '?reference=' + 'z'.repeat(400)
  await reloadReview(longReview)
  for (const width of [320, 390, 1280]) {
    await page.setViewportSize({ width, height: 900 })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width, 'long names fit the page')
    await rename.click()
    assert.equal(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth), true, 'long names fit the confirmation')
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  }
  for (const value of [{ ...baseReview, can_update: false }, { ...baseReview, can_update: undefined }, { ...baseReview, references: [{ ...baseReview.references[0], name_changes: [] }] }, { ...baseReview, references: [{ ...baseReview.references[0], hex_code: null, source: null, name_changes: [] }] }]) {
    await reloadReview(value)
    assert.equal(await rename.count(), 0)
    assert.equal(await panel.getByRole('combobox').count(), 1, 'HEX corrections remain available')
  }
  const conflictReview = structuredClone(baseReview)
  conflictReview.references[0].name_changes = []
  conflictReview.references[0].rename_conflicts = ['RAL 9005', 'PANTONE-' + 'X'.repeat(240)]
  await reloadReview(conflictReview)
  const conflictNote = panel.getByText(/Shared swatch also has conflicting references:/)
  await conflictNote.waitFor()
  assert.match(await conflictNote.textContent(), /RAL 9005.*Reuse or create a swatch to change this filament only/s)
  assert.equal(await rename.count(), 0, 'conflicting shared references cannot be renamed globally')
  const conflictChoice = panel.getByRole('combobox')
  assert.equal(await conflictChoice.isEnabled(), true)
  for (const action of ['reuse:2', 'create']) {
    await conflictChoice.selectOption(action)
    assert.equal(await conflictChoice.inputValue(), action, 'per-filament HEX correction choices remain available')
    assert.equal(await panel.getByRole('button', { name: 'Apply swatch', exact: true }).isEnabled(), true)
  }
  await page.setViewportSize({ width: 320, height: 900 })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 320, 'long conflicting codes wrap at phone width')
  assert.equal(await conflictNote.evaluate(el => el.scrollWidth <= el.clientWidth), true)
  const savedReview = structuredClone(baseReview)
  savedReview.references[0].saved_reference = true
  savedReview.references[0].manufacturer_source = null
  await reloadReview(savedReview)
  await panel.locator('.swatch-sources').getByText('Saved reference', { exact: true }).waitFor()
  assert.equal(await panel.getByRole('link', { name: 'Manufacturer', exact: true }).count(), 0, 'saved reference is not a manufacturer claim')
  assert.equal(await panel.getByRole('link', { name: 'Palette source', exact: true }).count(), 1)
  await reloadReview()
  for (const action of ['keep', 'reuse:2', 'create']) {
    await panel.getByRole('combobox').selectOption(action)
    await panel.getByRole('button', { name: action === 'keep' ? 'Save reference' : 'Apply swatch', exact: true }).click()
    await panel.locator('[data-swatch-status]').filter({ hasText: 'Color choice saved.' }).waitFor()
    assert.equal(posts.at(-1).action, action.split(':')[0])
    assert.equal(posts.at(-1).token, 'assignment-token')
  }
  batch.status = 'active'
  batch.items.push({ ...batch.items[0], id: 13, filament_id: 11, live_filament_id: 11, status: 'queued', color_references: [], identity: { designation: 'Other pending filament' } })
  await reloadReview()
  await rename.click()
  batch.status = 'finished'
  batch.items[1].status = 'no_change'
  await page.locator('[data-batch-progress]').filter({ hasText: /Finished/ }).waitFor()
  assert.equal(await dialog.isVisible(), true, 'background batch polling must preserve an open rename confirmation')
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  batch.items.pop()
  batch.total = 2
  batch.items.push({ ...structuredClone(batch.items[0]), id: 13, filament_id: 11, live_filament_id: 11, identity: { designation: 'Signal Black' } })
  batch.items[0].issues = [{ code: 'unavailable_source', message: 'Ancillary product site unavailable.', url: 'https://manufacturer.test/unavailable' }]
  const auditReview = structuredClone(baseReview)
  auditReview.references.push({ ...structuredClone(baseReview.references[0]), code: 'Pantone 7549 C', hex_code: null, source: null, name_changes: [] })
  await reloadReview(auditReview)
  await page.locator('[data-batch-progress]').filter({ hasText: '2 of 2 ready to review' }).waitFor()
  const warning = page.locator('.batch-diagnostics .batch-evidence > li').filter({ hasText: 'Ancillary product site unavailable.' })
  assert.equal(await warning.count(), 1)
  assert.equal(await warning.isVisible(), false, 'ancillary lookup warnings stay under Search details for usable color audits')
  await page.getByText('Search details (1)', { exact: true }).click()
  await warning.waitFor()
  assert.equal(await panel.getByText(/Preview unavailable/).isVisible(), true, 'missing publisher preview remains visible')
  const mutationCount = mutationPaths.length
  await page.getByRole('button', { name: 'Done & next', exact: true }).click()
  await page.locator('[data-batch-progress]').filter({ hasText: '1 of 2 ready to review' }).waitFor()
  assert.equal(await page.getByRole('heading', { name: 'Apply report', exact: true }).count(), 0, 'completing an audit review does not create an inventory apply report')
  assert.deepEqual(decisions.at(-1), { items: [{ item_id: 12, revision: 1, decision_revision: 0, approved_ids: [], decision: 'skipped' }] })
  assert.deepEqual(mutationPaths.slice(mutationCount), ['/api/v1/filabrain/batches/audit/decisions'], 'marking reviewed does not write inventory')
  const queue = page.locator('[data-batch-queue]')
  await queue.getByRole('button', { name: /Decepticon Purple.*Reviewed/ }).waitFor()
  assert.equal(await queue.getByRole('button', { name: /Signal Black/ }).getAttribute('aria-current'), 'true')
  await page.locator('[data-batch-review]').getByRole('heading', { name: 'Signal Black', exact: true }).waitFor()
  await queue.getByRole('button', { name: /Decepticon Purple.*Reviewed/ }).click()
  await page.getByRole('button', { name: 'Reopen review', exact: true }).waitFor()
  for (const mode of ['fill', 'standardize']) {
    batch.mode = mode
    await page.goto(origin + '/plugin-page/filabrain?tab=batches&id=audit')
    await queue.getByRole('button', { name: /Decepticon Purple.*Skipped/ }).waitFor()
    assert.equal(await page.getByRole('button', { name: 'Done & next', exact: true }).count(), 0, 'existing modes retain their review semantics')
    await page.locator('[data-batch-progress]').filter({ hasText: '0 of 2 ready to review' }).waitFor()
  }
  batch.mode = 'colors'
  batch.items.pop()
  batch.total = 1
  batch.items[0].decision = 'pending'
  batch.items[0].decision_revision = 0
  batch.items[0].issues = []
  await page.goto(origin + '/draft-fixture')
  await page.evaluate(value => window.mountDraft(value), baseReview)
  assert.equal(await page.getByRole('button', { name: 'Rename shared swatch', exact: true }).count(), 0, 'draft adapters never offer shared rename')
  for (const action of ['reuse:2', 'create']) {
    await page.getByRole('combobox').selectOption(action)
    await page.getByRole('button', { name: 'Use swatch', exact: true }).click()
    assert.equal(await page.evaluate(() => window.choices[1]), action.split(':')[0])
  }
  assert.deepEqual(errors, [])
  console.log('Color audit: mode, reviewed shared rename, stale/error handling, keyboard, mobile layouts and existing swatch choices passed')
} finally { await browser.close() }
