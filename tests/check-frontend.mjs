import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const html = await readFile(new URL('../filabrain/page.html', import.meta.url), 'utf8')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
const url = 'https://fila.test/plugin-page/filabrain'
const manufacturer = { id: 7, name: 'Fixture Brand' }
const pickerRecordToken = 'a'.repeat(32)
const filament = {
  id: 10, designation: 'Fixture PLA', manufacturer_id: 7, manufacturer,
  material_type: 'PLA', diameter_mm: 1.75, color_mode: 'single',
}
const evidence = [{ title: 'Fixture catalog', url: 'https://manufacturer.test/pla', evidence: '1.24 g/cm³' }]
const fixtureBatch = {
  id: 'fixture-batch', mode: 'fill', status: 'active', total: 1,
  created_at: '2026-01-01T00:00:00Z', finished_at: null,
  items: [{ id: 12, filament_id: 10, live_filament_id: 10, status: 'ready', decision: 'pending',
    revision: 1, decision_revision: 0, identity: { designation: 'Fixture PLA' }, issues: [],
    approved_ids: [], changed_fields: [], applied_at: null,
    proposals: [{ id: 'density', fields: { density_g_cm3: { old: null, value: 1.24 } },
      sources: [], reason: 'Confirmed density', group_key: null }],
  }],
}

try {
  const context = await browser.newContext()
  const page = await context.newPage()
  page.setDefaultTimeout(5_000)
  const writes = []
  const savePaths = []
  let mcpEnabled = false
  let searchUrl = ''
  let supplierDownloadMiB = 8
  let supplierSites = []
  let savedManufacturerWebsite
  let chatModelId = 'fixture:default'
  let canConfigure = true
  let modelsUnavailable = false
  let hostPicker = false
  let account = 7
  let chatRequests = 0
  let proposalRequests = 0
  let pairedProposal = null
  let heldModelList
  let staleBatchOnce = false
  let libraryReads = 0
  let editorRecordToken = 'c'.repeat(32)
  let hostEditorToken
  let editorMode = 'edit'
  let recordUnavailable = false
  let supportsRecordToken = true
  let hostSendsIdentity = false
  await context.route('https://fila.test/**', async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (path.startsWith('/plugin-page/')) return route.fulfill({ contentType: 'text/html', body: html })
    if (path === '/filament-picker/' && hostPicker) return route.fulfill({ contentType: 'text/html', body: `
      <label data-filament-picker${hostPicker === "current" ? '="1"' : ""}><input type="checkbox"> Fixture PLA</label>
      <script>
        const checkbox = document.querySelector('input')
        checkbox.onchange = () => parent.postMessage({
          type: 'filaman:filament-picker-selection', filament_ids: checkbox.checked ? [10] : [],
          expected_filaments: checkbox.checked ? [{id:10,manufacturer_id:7,designation:'Fixture PLA',material_type:'PLA',record_token:'${pickerRecordToken}'}] : [],
        }, location.origin)
        window.addEventListener('message', event => {
          if (event.source !== parent || event.origin !== location.origin ||
              event.data?.type !== 'filaman:filament-picker-clear-selection') return
          checkbox.checked = false
          checkbox.onchange()
        })
      </script>` })
    if (path === '/selection-fixture') return route.fulfill({ contentType: 'text/html', body: `
      <iframe sandbox="allow-scripts allow-same-origin allow-forms" src="${url}?filament_host=selection"></iframe>
      <script>window.addEventListener('message', event => {
        if (event.origin !== location.origin || event.source !== document.querySelector('iframe').contentWindow) return
        const { type, request_id } = event.data;
        if (type === 'filaman:filament-context') event.source.postMessage({type, request_id, mode:'selection', filament_ids:[10],
          ${hostSendsIdentity ? `expected_filaments: [{id:10,manufacturer_id:7,designation:'Fixture PLA',material_type:'PLA',record_token:'${pickerRecordToken}'}]` : ''}
        }, location.origin)
      })</script>` })
    if (path === '/editor-fixture') return route.fulfill({ contentType: 'text/html', body: `
      <iframe style="width: 640px; height: 520px" sandbox="allow-scripts allow-same-origin allow-forms" src="${url}?filament_host=editor"></iframe>
      <script>
        window.addEventListener('message', event => {
          if (event.origin !== location.origin || event.source !== document.querySelector('iframe').contentWindow) return
          const { type, request_id, units } = event.data;
          if (type === 'filaman:filament-context') event.source.postMessage({type, request_id, mode:'${editorMode}',
            filament_id:${editorMode === 'edit' ? 10 : 'null'}, record_token:${JSON.stringify(hostEditorToken)}, fields:{ designation:'Fixture PLA', manufacturer_id:7, manufacturer_name:'Fixture Brand', material_type:'PLA', diameter_mm:1.75 }}, location.origin)
          if (type === 'filaman:filament-stage') {
            window.staged = units;
            event.source.postMessage({type, request_id, staged:units.flatMap(unit => Object.keys(unit)), skipped:[]}, location.origin)
          }
        })
      </script>` })
    if (path.endsWith('/me')) return route.fulfill({ json: { id: account } })
    if (path.endsWith('/status')) return route.fulfill({ json: { enabled: true, can_chat: true, chat_model: chatModelId,
      can_create: true, can_update: true, can_update_manufacturer: true, can_configure: canConfigure, mcp_enabled: mcpEnabled } })
    if (path.endsWith('/filaments/10/research-record')) return recordUnavailable
      ? route.fulfill({ status: 503, json: {} })
      : route.fulfill({ json: { record_token: editorRecordToken } })
    if (path.endsWith('/filaments/10/manufacturer-website') && req.method() === 'PUT') {
      savedManufacturerWebsite = req.postDataJSON()
      return route.fulfill({ status: 204 })
    }
    if (path.endsWith('/settings/models') && heldModelList) await heldModelList
    if (path.endsWith('/models')) return modelsUnavailable
      ? route.fulfill({ status: 502, json: {} })
      : route.fulfill({ json: { models: ['fixture:default', 'fixture:alternate'] } })
    if (path.endsWith('/chat')) {
      assert.equal(req.postDataJSON().messages[0].content, 'Compare these materials')
      if (chatRequests++ === 0) assert.equal(req.postDataJSON().model_id, 'fixture:alternate')
      else assert.deepEqual(req.postDataJSON().messages.map(message => message.role), ['user', 'assistant', 'user'])
      return route.fulfill({ json: { reply: 'PLA is easier to print.', action: null } })
    }
    if (path.endsWith('/proposals')) {
      proposalRequests++
      const target = req.postDataJSON()
      if (target.filament_id === 10 && target.action === 'fill') assert.equal(target.model_id, 'fixture:alternate')
      if (target.product_url) assert.equal(target.product_url, evidence[0].url)
      if (target.guidance && target.mode === 'create') assert.equal(target.guidance, 'Look for a datasheet')
      if (target.guidance && target.mode === 'edit') {
        assert.equal(target.action, 'standardize')
        assert.equal(target.filament_id, 10)
        if (target.guidance === 'Check an unavailable source') return route.fulfill({
          status: 502, json: { detail: 'Source unavailable' },
        })
        assert.equal(target.guidance, 'Focus on the material finish')
      }
      if (target.action === 'standardize') return route.fulfill({ json: {
        target, suggestions: [], issues: [],
        proposals: [{ id: 'linked', fields: {
          designation: { old: 'Fixture PLA', value: 'Fixture-PLA' },
          material_subgroup: { old: null, value: target.guidance ? 'Silk' : 'Matte' },
        }, sources: [], reason: 'Consistent designation and finish.', group_key: null }],
      } })
      return route.fulfill({ json: { target, suggestions: [
        { field: 'density_g_cm3', value: 1.24, sources: evidence },
        { field: 'manufacturer_sku', value: 'FIX-123', sources: evidence },
      ], issues: target.mode === 'edit' ? [
        { code: 'manufacturer_page', name: 'Fixture datasheet', url: evidence[0].url },
        { code: 'manufacturer_page', name: 'Fixture datasheet', url: evidence[0].url },
        { code: 'manufacturer_color_reference', name: 'RAL 9005 Signal Black', url: evidence[0].url },
        { code: 'manufacturer_website_found', name: manufacturer.name, source: 'FilaManDB', url: 'https://manufacturer.test/' },
        { code: 'possible_match', name: 'Fixture PLA', source: 'FilaManDB', url: evidence[0].url, product_url: evidence[0].url },
      ] : [] } })
    }
    if (path.endsWith('/settings/mcp')) {
      mcpEnabled = req.postDataJSON().mcp_enabled
      return route.fulfill({ json: { mcp_enabled: mcpEnabled } })
    }
    if (path.endsWith('/settings/test-search')) {
      assert.deepEqual(req.postDataJSON(), { search_url: 'http://searxng:8080' })
      return route.fulfill({ json: { ok: true } })
    }
    if (path.endsWith('/settings/test')) {
      assert.deepEqual(req.postDataJSON(), {
        model_url: 'http://ollama:11434', model_id: 'fixture:default', api_key: 'fixture-key', clear_key: false,
      })
      return route.fulfill({ json: { ok: true } })
    }
    if (path.endsWith('/pairings') && req.method() === 'POST') {
      assert.equal(req.postDataJSON().filament_id, 10)
      return route.fulfill({ json: { id: 'fixture-pair', url: 'https://fila.test/api/v1/filabrain/mcp', token: 'fixture-token' } })
    }
    if (path.endsWith('/pairings/fixture-pair')) {
      return req.method() === 'DELETE' ? route.fulfill({ status: 204 })
        : route.fulfill({ json: pairedProposal ? { proposal_id: 'from-client', proposal: pairedProposal } : {} })
    }
    if (path.endsWith('/settings')) {
      const settings = req.method() === 'PUT' ? req.postDataJSON() : null
      if (settings) {
        searchUrl = settings.search_url
        supplierDownloadMiB = settings.supplier_download_mib
        supplierSites = settings.supplier_sites ?? supplierSites
        if (settings.model_id) chatModelId = settings.model_id
      }
      return route.fulfill({ json: {
        model_url: settings?.model_url || '', model_id: settings?.model_id || '',
        search_url: searchUrl, has_key: false, mcp_enabled: mcpEnabled,
        supplier_sites: supplierSites, supplier_download_mib: supplierDownloadMiB,
      } })
    }
    if (path.endsWith('/manufacturers')) return route.fulfill({ json: { items: [manufacturer], total: 1 } })
    if (path.endsWith('/colors')) return route.fulfill({ json: { items: [], total: 0 } })
    if (path.endsWith('/filaments') && req.method() === 'GET') {
      libraryReads++
      const requestedToken = new URL(req.url()).searchParams.get('include_record_token') === 'true'
      const record_token = requestedToken && supportsRecordToken ? pickerRecordToken : undefined
      return route.fulfill({ json: { items: [{ ...filament, record_token }], total: 1 } })
    }
    if (path.includes('/saved-filaments/') && req.method() === 'POST') {
      writes.push(req.postDataJSON())
      savePaths.push(path)
      return route.fulfill({ json: { id: 11 } })
    }
    if (path.endsWith('/batches') && req.method() === 'POST') {
      const hostSelection = new URL(req.frame().url()).searchParams.get('filament_host') === 'selection'
      const { id, manufacturer_id, designation, material_type } = filament
      assert.deepEqual(req.postDataJSON(), {
        filament_ids: [10], mode: 'fill',
        ...(hostSelection && !hostSendsIdentity ? {} : { expected_filaments: [{
          id, manufacturer_id, designation, material_type,
          ...(hostPicker === 'current' || supportsRecordToken ? { record_token: pickerRecordToken } : {}),
        }] }),
      })
      if (staleBatchOnce) {
        staleBatchOnce = false
        return route.fulfill({ status: 409, json: { detail: { code: 'filament_selection_changed', message: 'Stale selection' } } })
      }
      return route.fulfill({ json: fixtureBatch })
    }
    if (path.endsWith('/batches/fixture-batch')) return route.fulfill({ json: fixtureBatch })
    if (path.endsWith('/decisions')) {
      const [decision] = req.postDataJSON().items
      assert.deepEqual(decision.approved_ids, ['density'])
      Object.assign(fixtureBatch.items[0], { decision: 'approved', approved_ids: ['density'], decision_revision: 1 })
      return route.fulfill({ json: fixtureBatch })
    }
    if (path.endsWith('/apply')) {
      assert.equal(req.postDataJSON().items[0].decision_revision, 1)
      Object.assign(fixtureBatch.items[0], { status: 'applied', applied_at: '2026-01-01T00:00:00Z' })
      return route.fulfill({ json: { batch: fixtureBatch, results: [{ item_id: 12, status: 'applied', changed_fields: [] }] } })
    }
    if (path.endsWith('/batches')) return route.fulfill({ json: [] })
    return route.fulfill({ status: 404, json: {} })
  })

  chatModelId = ''
  await page.goto(url)
  const setupModel = page.getByRole('button', { name: 'Set up a model' })
  await setupModel.waitFor()
  assert.equal(await page.getByRole('button', { name: 'Compare materials' }).isVisible(), false)
  assert.equal(await page.getByRole('textbox', { name: 'Your question' }).isVisible(), false)
  await setupModel.click()
  const modelUrl = page.getByRole('textbox', { name: 'Model server URL' })
  assert.equal(await modelUrl.evaluate(el => document.activeElement === el), true)
  await modelUrl.fill('http://ollama:11434')
  const modelId = page.getByRole('textbox', { name: 'Model ID', exact: true })
  assert.equal(await modelId.isVisible(), false)
  await page.locator('#filabrain-settings').getByText('Enter a model ID manually', { exact: true }).click()
  await modelId.fill('fixture:default')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await page.getByText('Settings saved.').waitFor()
  await page.getByRole('button', { name: 'Chat', exact: true }).click()
  await page.getByRole('textbox', { name: 'Your question' }).waitFor()
  assert.equal(await setupModel.isVisible(), false)
  assert.equal(await page.getByRole('button', { name: 'Guide', exact: true }).count(), 1)
  assert.equal(await page.locator('#plugin-header [data-tab] svg[aria-hidden="true"]').count(), 5)

  chatModelId = ''
  canConfigure = false
  await page.reload()
  await page.getByText('Ask a FilaMan administrator to set up a chat model.').waitFor()
  assert.equal(await setupModel.isVisible(), false)
  canConfigure = true
  chatModelId = 'fixture:default'
  modelsUnavailable = true
  await page.reload()
  const warning = page.getByRole('link', { name: 'Could not load other models.' })
  await warning.waitFor()
  const pickerButton = page.locator('[data-chat-form] [data-model-trigger]')
  const warningBounds = await warning.boundingBox()
  const pickerBounds = await pickerButton.boundingBox()
  assert.ok(warningBounds.x >= pickerBounds.x + pickerBounds.width)
  assert.equal(await warning.evaluate(el => getComputedStyle(el).color), 'rgb(255, 211, 107)')
  assert.equal(await warning.getAttribute('href'), '?tab=settings')
  await warning.press('Enter')
  assert.equal(await modelUrl.evaluate(el => el === document.activeElement), true)

  canConfigure = false
  await page.reload()
  await page.getByText('Could not load other models.', { exact: true }).waitFor()
  assert.equal(await warning.count(), 0)

  canConfigure = true
  modelsUnavailable = false
  await page.reload()
  const chatModel = page.getByRole('button', { name: /^Model for this conversation/ })
  const chatMenu = page.getByRole('menu', { name: 'Model for this conversation' })
  await page.locator('[data-chat-form] [data-model-trigger]:not(:disabled)').waitFor()
  await chatModel.press('Enter')
  await page.locator('[data-chat-form] [data-model-trigger][aria-expanded="true"]').waitFor()
  const defaultModel = chatMenu.getByRole('menuitemradio', { name: 'fixture:default Default' })
  await defaultModel.waitFor()
  assert.equal(await defaultModel.getAttribute('aria-checked'), 'true')
  await defaultModel.press('ArrowDown')
  const alternateModel = chatMenu.getByRole('menuitemradio', { name: 'fixture:alternate' })
  assert.equal(await alternateModel.evaluate(el => el === document.activeElement), true)
  await alternateModel.press('Enter')
  await chatMenu.waitFor({ state: 'hidden' })
  assert.match(await chatModel.innerText(), /fixture:alternate/)
  await page.getByRole('textbox', { name: 'Your question' }).fill('Compare these materials')
  await page.getByRole('button', { name: 'Send message' }).click()
  await page.getByText('PLA is easier to print.').waitFor()
  const guide = page.getByRole('dialog', { name: 'Guide' })
  const headerGuide = page.locator('#plugin-header').getByRole('button', { name: 'Guide' })
  const draft = page.getByRole('textbox', { name: 'Your question' })
  assert.equal(await draft.getAttribute('placeholder'), 'Ask FilaBrain…')
  await draft.fill('Unsent follow-up')
  const chatCard = page.locator('.research-chat')
  const fullWidth = (await chatCard.boundingBox()).width
  await headerGuide.click()
  await guide.waitFor()
  assert.equal(await headerGuide.getAttribute('aria-expanded'), 'true')
  assert.ok((await chatCard.boundingBox()).width < fullWidth)
  const desktopComposer = await page.locator('[data-chat-form]').boundingBox()
  const desktopGuide = await guide.boundingBox()
  assert.ok(desktopComposer.y + desktopComposer.height <= desktopGuide.y + desktopGuide.height)
  const providerLink = guide.getByRole('link', { name: 'Ollama setup' })
  await providerLink.waitFor()
  const artBounds = await guide.getByRole('img').boundingBox()
  assert.ok(artBounds.y + artBounds.height < (await providerLink.boundingBox()).y)
  await guide.getByRole('button', { name: 'Close' }).press('Escape')
  await guide.waitFor({ state: 'hidden' })
  assert.equal(await headerGuide.evaluate(el => el === document.activeElement), true)
  assert.equal(await draft.inputValue(), 'Unsent follow-up')
  await page.getByText('PLA is easier to print.').waitFor()

  await page.setViewportSize({ width: 390, height: 780 })
  const composerBounds = await page.locator('[data-chat-form]').boundingBox()
  assert.ok(composerBounds.y + composerBounds.height <= 780, 'the mobile composer fits in the viewport')
  assert.ok((await chatModel.boundingBox()).height >= 44)
  await headerGuide.click()
  await guide.waitFor()
  assert.equal(await guide.evaluate(el => el.matches(':modal')), true)
  assert.equal((await guide.boundingBox()).width, 390)
  assert.equal((await guide.boundingBox()).height, 780)
  await page.setViewportSize({ width: 1280, height: 720 })
  await page.waitForFunction(() => document.querySelector('#filabrain-docs').matches('[open]:not(:modal)'))
  await guide.getByRole('button', { name: 'Close' }).click()
  await guide.waitFor({ state: 'hidden' })
  assert.equal(await headerGuide.evaluate(el => el === document.activeElement), true)
  assert.equal(await draft.inputValue(), 'Unsent follow-up')

  await page.getByRole('button', { name: 'New chat' }).click()
  assert.match(await chatModel.innerText(), /fixture:default/)

  await page.getByRole('button', { name: 'Research new filament' }).click()
  await page.getByLabel('Manufacturer', { exact: true }).selectOption('7')
  await page.getByLabel('Product name', { exact: true }).fill('Fixture PLA')
  await page.locator('[data-view="chat"]:not([hidden]) #product-research').waitFor()
  const firstSearchPicker = page.locator('[data-chat-form] [data-model-trigger]')
  await firstSearchPicker.waitFor()
  assert.equal(await page.getByLabel('Material', { exact: true }).isVisible(), false)
  await page.getByText('Search options', { exact: true }).click()
  await page.getByLabel('Material', { exact: true }).fill('PLA')
  await page.getByLabel('Diameter (mm)').fill('1.75')
  const findSpecs = page.getByRole('button', { name: 'Find specifications' })
  assert.equal(await findSpecs.locator('svg[aria-hidden="true"]').count(), 1)
  await findSpecs.click()
  await page.getByRole('link', { name: 'Fixture catalog' }).first().waitFor()
  await page.getByRole('textbox', { name: 'Follow up on this search' }).fill('Look for a datasheet')
  await page.getByRole('button', { name: 'Send follow-up' }).click()
  await page.getByRole('log').getByText('Look for a datasheet', { exact: true }).waitFor()
  await page.getByRole('log').getByText('Here’s what I found. Review the results below.', { exact: true }).nth(1).waitFor()
  const earlierResult = page.getByRole('log').getByText('Earlier findings', { exact: true })
  await earlierResult.click()
  const earlierSource = page.getByRole('log').getByRole('link', { name: 'Fixture catalog' }).first()
  await earlierSource.waitFor()
  assert.equal(await earlierSource.evaluate(el => Boolean(el.closest('details')?.nextElementSibling?.classList.contains('chat-user'))), true)
  await earlierResult.click()
  await page.getByRole('checkbox', { name: /Density/ }).check()
  assert.equal(writes.length, 0)
  const second = await context.newPage()
  second.setDefaultTimeout(5_000)
  await second.goto(url)
  await second.locator('[data-chat-history] summary').click()
  await second.getByRole('button', { name: /Find specifications · Fixture Brand Fixture PLA/ }).click()
  await second.getByRole('button', { name: 'Save new filament' }).click()
  const secondConfirm = second.getByRole('dialog').getByRole('button', { name: 'Create filament' })
  await secondConfirm.waitFor()
  await page.getByRole('button', { name: 'Save new filament' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Create filament' }).click()
  await page.getByRole('link', { name: 'Open saved filament' }).waitFor()
  assert.deepEqual(writes, [{ manufacturer_id: 7, designation: 'Fixture PLA', material_type: 'PLA',
    diameter_mm: 1.75, density_g_cm3: 1.24 }])

  await secondConfirm.click()
  await second.getByRole('link', { name: 'Open saved filament' }).waitFor()
  assert.equal(writes.length, 1, 'a stale confirmation does not submit a second create')
  await second.close()

  await page.reload()
  const history = page.locator('.chat-header').getByText('History', { exact: true })
  await history.click()
  await page.getByRole('button', { name: 'Compare these materials', exact: true }).click()
  await page.getByText('PLA is easier to print.').waitFor()
  assert.match(await page.getByRole('button', { name: /^Model for this conversation/ }).innerText(), /fixture:alternate/)
  await page.getByRole('textbox', { name: 'Your question' }).fill('Which settings should I try?')
  await page.getByRole('button', { name: 'Send message' }).click()
  await page.getByText('PLA is easier to print.').nth(1).waitFor()
  await history.click()
  await page.getByRole('button', { name: /Find specifications · Fixture Brand Fixture PLA/ }).click()
  await page.getByRole('log').getByText('Look for a datasheet', { exact: true }).waitFor()
  await earlierResult.first().click()
  await earlierSource.waitFor()
  await page.getByRole('link', { name: 'Fixture catalog' }).first().waitFor()
  const savedLink = page.getByRole('link', { name: 'Open saved filament' })
  await savedLink.waitFor()
  assert.match(await savedLink.getAttribute('href'), /^\/api\/v1\/filabrain\/saved-filaments\/[a-z0-9-]+$/)
  assert.equal(await page.getByRole('button', { name: 'Save new filament' }).count(), 0)
  account = 8
  await page.reload()
  await history.click()
  await page.getByText('No saved conversations yet.').waitFor()
  account = 7

  await page.getByRole('button', { name: 'Research library' }).click()
  const fallbackFilament = page.getByRole('checkbox', { name: /Fixture PLA/ })
  const clearSelection = page.getByRole('button', { name: 'Clear selection' })
  await fallbackFilament.check()
  await clearSelection.click()
  assert.equal(await fallbackFilament.isChecked(), false)
  assert.equal(await page.getByRole('button', { name: 'Start research' }).isDisabled(), true)
  await fallbackFilament.check()
  const readsBeforeConflict = libraryReads
  staleBatchOnce = true
  await page.getByRole('button', { name: 'Start research' }).click()
  await page.getByText('The filament library changed. Select your filaments again.').waitFor()
  await fallbackFilament.waitFor()
  assert.equal(libraryReads, readsBeforeConflict + 1)
  assert.equal(await fallbackFilament.isChecked(), false)
  await fallbackFilament.check()
  await page.getByRole('button', { name: 'Start research' }).click()
  await page.getByText('Confirmed density').waitFor()
  assert.equal(await page.getByRole('link', { name: /Back to Filaments/i }).count(), 0)
  await page.locator('.fm-card.batch-toolbar').waitFor()
  await page.getByRole('checkbox', { name: /Density/ }).check()
  await page.getByRole('button', { name: /Approve/ }).click()
  await page.getByRole('button', { name: 'Apply approved changes' }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Apply/ }).click()
  await page.getByRole('heading', { name: 'Apply report' }).waitFor()
  await page.getByRole('link', { name: 'All batches' }).click()
  await page.getByRole('region', { name: 'Research batches' }).getByText('No research batches yet.').waitFor()

  await page.getByRole('button', { name: 'Model & MCP settings' }).click()
  const settingsTitle = page.getByRole('heading', { name: 'Model settings', exact: true })
  await settingsTitle.waitFor()
  const settingsBefore = await settingsTitle.boundingBox()
  const settingsForm = page.getByRole('region', { name: 'Model settings' })
  const formBefore = await settingsForm.boundingBox()
  await headerGuide.click()
  await guide.waitFor()
  assert.equal((await settingsTitle.boundingBox()).y, settingsBefore.y, 'the guide preserves the settings heading position')
  const formWithGuide = await settingsForm.boundingBox()
  assert.equal(formWithGuide.x, formBefore.x, 'the guide preserves the settings form position')
  assert.equal(formWithGuide.width, formBefore.width, 'the guide preserves the settings form width')
  await guide.getByText('External MCP', { exact: true }).waitFor()
  await page.getByRole('button', { name: 'Research library' }).click()
  await guide.getByRole('heading', { name: 'Research library' }).waitFor()
  await page.getByRole('button', { name: 'Model & MCP settings' }).click()
  const guideBounds = await guide.boundingBox()
  assert.ok(guideBounds.x >= formBefore.x + formBefore.width, 'the guide sits beside settings')
  assert.equal(guideBounds.y, (await page.locator('[data-view="settings"]').boundingBox()).y, 'the guide and settings start on the same row')
  assert.ok(guideBounds.y + guideBounds.height >= 696, 'the guide reaches the bottom page gutter')
  await page.setViewportSize({ width: 1024, height: 720 })
  await page.waitForFunction(() => document.querySelector('#filabrain-docs').matches(':modal'))
  assert.equal((await settingsForm.boundingBox()).width, formBefore.width, 'the fallback keeps settings full width')
  await page.setViewportSize({ width: 1280, height: 720 })
  await page.waitForFunction(() => document.querySelector('#filabrain-docs').matches('[open]:not(:modal)'))
  assert.equal(await headerGuide.evaluate(button => button.closest('nav') === null), true)
  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({ width, height: 780 })
    const guideButtonBounds = await headerGuide.boundingBox()
    const firstTabBounds = await page.getByRole('button', { name: 'Chat', exact: true }).boundingBox()
    assert.equal(guideButtonBounds.height, firstTabBounds.height)
    assert.ok(Math.abs(guideButtonBounds.y - firstTabBounds.y) <= 1, `Guide aligns with the tabs at ${width}px`)
  }
  await page.setViewportSize({ width: 1440, height: 780 })
  const guideClose = guide.getByRole('button', { name: 'Close' })
  assert.equal(await guideClose.locator('svg[aria-hidden="true"]').count(), 1)
  await guideClose.click()
  await guide.waitFor({ state: 'hidden' })
  const settings = page.locator('#mcp-settings').locator('..').locator('..')
  await settings.getByText('Off', { exact: true }).waitFor()
  const enableSearch = page.getByRole('button', { name: 'Set up search' })
  const searchField = page.getByRole('textbox', { name: 'SearXNG URL (optional)' })
  const sources = page.getByRole('region', { name: 'Research sources' })
  const builtIn = sources.getByRole('list')
  await builtIn.getByText('FilaManDB', { exact: true }).waitFor()
  await builtIn.getByText('Open Filament Database', { exact: true }).waitFor()
  assert.equal(await builtIn.getByRole('checkbox').count(), 0)
  const downloadLimit = sources.getByRole('spinbutton', { name: 'Page and PDF limit (MiB)' })
  assert.equal(await downloadLimit.inputValue(), '8')
  await downloadLimit.fill('12')
  const internet = page.getByRole('region', { name: 'Internet search' })
  const supplierDomain = internet.getByRole('textbox', { name: 'Supplier domain' })
  assert.equal(await supplierDomain.isVisible(), false)
  assert.equal(await searchField.isVisible(), false)
  await enableSearch.click()
  assert.equal(await searchField.evaluate(el => el === document.activeElement), true)
  await page.getByRole('link', { name: 'SearXNG Docker setup' }).waitFor()
  const searchTest = internet.getByRole('button', { name: 'Test SearXNG' })
  assert.equal(await searchTest.isDisabled(), true)
  await modelUrl.fill('invalid-model-url')
  await searchField.fill('http://searxng:8080')
  await searchTest.click()
  const searchPassed = internet.getByText('SearXNG returned search results.')
  await searchPassed.waitFor()
  await searchField.fill('invalid-search-url')
  await searchPassed.waitFor({ state: 'hidden' })
  await modelUrl.fill('http://ollama:11434')
  if (!await modelId.isVisible()) await page.locator('#filabrain-settings summary').click()
  await modelId.fill('fixture:default')
  const modelKey = page.locator('#filabrain-settings input[name=api_key]')
  await modelKey.fill('fixture-key')
  await page.getByRole('button', { name: 'Test connection', exact: true }).click()
  await page.getByText('The model returned a verified sample.').waitFor()
  await modelKey.fill('')
  await searchField.fill('http://searxng:8080')
  await modelUrl.fill('http://ollama:11434')
  let releaseModelList
  heldModelList = new Promise(resolve => { releaseModelList = resolve })
  const loadModels = page.getByRole('button', { name: 'Load models', exact: true })
  await loadModels.click()
  assert.equal(await loadModels.isDisabled(), true)
  await internet.getByRole('button', { name: 'Test SearXNG' }).click()
  await internet.getByText('SearXNG returned search results.').waitFor()
  assert.equal(searchUrl, '', 'testing SearXNG does not save the URL')
  releaseModelList()
  heldModelList = undefined
  await page.getByLabel('Available models').selectOption('fixture:default')
  await supplierDomain.fill('shop.example.org')
  await internet.getByRole('button', { name: 'Add supplier' }).click()
  const supplier = internet.getByRole('checkbox', { name: 'shop.example.org' })
  assert.equal(await supplier.isChecked(), true)
  await page.locator('#filabrain-settings').getByRole('button', { name: 'Save', exact: true }).click()
  await page.getByText('Settings saved.').waitFor()
  assert.equal(searchUrl, 'http://searxng:8080')
  await page.reload()
  await page.getByRole('button', { name: 'Model & MCP settings' }).click()
  await searchField.waitFor()
  assert.equal(await searchField.inputValue(), searchUrl)
  assert.equal(await downloadLimit.inputValue(), '12')
  assert.equal(await supplier.isChecked(), true)
  await supplier.uncheck()
  await page.locator('#filabrain-settings').getByRole('button', { name: 'Save', exact: true }).click()
  await page.getByText('Settings saved.').waitFor()
  assert.deepEqual(supplierSites, [{ domain: 'shop.example.org', enabled: false }])
  await internet.getByRole('button', { name: 'Remove shop.example.org' }).click()
  await page.locator('#filabrain-settings').getByRole('button', { name: 'Save', exact: true }).click()
  await page.getByText('Settings saved.').waitFor()
  assert.deepEqual(supplierSites, [])
  assert.equal(await enableSearch.isVisible(), false)
  assert.equal(await settings.getByRole('textbox', { name: 'MCP URL' }).isVisible(), false)
  await settings.getByRole('switch').press('Space')
  await settings.getByText('On', { exact: true }).waitFor()
  assert.equal(await settings.getByRole('textbox', { name: 'MCP URL' }).isVisible(), true)

  await page.goto('https://fila.test/editor-fixture')
  const frame = page.frameLocator('iframe')
  const followUp = frame.getByRole('textbox', { name: 'Follow up on this search' })
  const drawerGuide = frame.getByRole('dialog', { name: 'Guide' })
  await frame.getByRole('button', { name: 'Guide', exact: true }).click()
  await drawerGuide.getByRole('heading', { name: 'Research this filament', exact: true }).waitFor()
  assert.equal(await drawerGuide.evaluate(el => el.matches(':modal')), true)
  await page.locator('iframe').evaluate(el => { el.style.width = '960px' })
  await frame.locator('#filabrain-docs[open]:not(:modal)').waitFor()
  const drawerBounds = await frame.locator('.research-chat').boundingBox()
  assert.ok((await drawerGuide.boundingBox()).x >= drawerBounds.x + drawerBounds.width)
  await frame.getByRole('button', { name: 'Model & MCP settings' }).click()
  await frame.locator('#filabrain-docs:modal').waitFor()
  await drawerGuide.getByText('External MCP', { exact: true }).waitFor()
  await drawerGuide.getByRole('button', { name: 'Close' }).click()
  const settingsGuide = frame.getByRole('button', { name: 'Guide', exact: true })
  await settingsGuide.locator('xpath=self::*[@aria-expanded="false"]').waitFor()
  assert.equal(await settingsGuide.evaluate(el => el === el.ownerDocument.activeElement), true)
  await page.locator('iframe').evaluate(el => { el.style.width = '640px' })
  await frame.getByRole('button', { name: 'Back to chat' }).click()
  const researchModel = frame.getByRole('button', { name: /^Model for this conversation/ })
  const researchMenu = frame.getByRole('menu', { name: 'Model for this conversation' })
  await researchModel.click()
  await researchMenu.getByRole('menuitemradio', { name: 'fixture:default Default' }).press('Escape')
  await researchMenu.waitFor({ state: 'hidden' })
  await researchModel.click()
  await researchMenu.getByRole('menuitemradio', { name: 'fixture:alternate' }).click()
  await researchMenu.waitFor({ state: 'hidden' })
  assert.equal(await frame.getByText('Advanced options', { exact: true }).count(), 0)
  assert.equal(await frame.getByRole('button', { name: 'Pair a client' }).isVisible(), false)
  await frame.getByRole('button', { name: 'Model & MCP settings' }).click()
  const pair = frame.getByRole('region', { name: 'Pair this draft' })
  await pair.getByRole('button', { name: 'Pair a client' }).click()
  const token = pair.getByRole('textbox', { name: /Bearer token/ })
  await token.waitFor()
  assert.equal(await token.inputValue(), 'fixture-token')
  await pair.getByRole('button', { name: 'Disconnect' }).click()
  assert.equal(await token.isVisible(), false)
  await frame.getByRole('button', { name: 'Back to chat' }).click()
  assert.equal(await followUp.isVisible(), false)
  assert.match(await researchModel.innerText(), /fixture:alternate/)
  await frame.getByRole('button', { name: 'Find specifications' }).click()
  await frame.getByRole('heading', { name: 'Chat', exact: true }).waitFor()
  const densityChoice = frame.getByRole('checkbox', { name: /Density/ })
  await densityChoice.waitFor()
  const checks = frame.getByText('Sources checked (1)', { exact: true })
  await checks.waitFor()
  const checkedSheet = frame.getByText('Manufacturer page checked: Fixture datasheet', { exact: false })
  assert.equal(await checkedSheet.isVisible(), false)
  await checks.click()
  assert.equal(await checkedSheet.getByRole('link', { name: 'Open source' }).getAttribute('href'), evidence[0].url)
  await checks.click()
  assert.equal(await frame.getByRole('button', { name: 'Stage fields in FilaMan editor' }).isDisabled(), true)
  await densityChoice.check()
  await frame.getByRole('button', { name: 'Stage 1 field' }).waitFor()
  await densityChoice.uncheck()
  assert.equal(await frame.getByRole('button', { name: 'Stage fields in FilaMan editor' }).isDisabled(), true)
  await densityChoice.check()
  await followUp.waitFor()
  await frame.getByRole('log').getByText('Find specifications', { exact: true }).waitFor()
  const colorReference = frame.getByText('Manufacturer color reference: RAL 9005 Signal Black')
  assert.equal(await colorReference.getByRole('link', { name: 'Open source' }).getAttribute('href'), evidence[0].url)
  assert.equal(savedManufacturerWebsite, undefined)
  await frame.getByRole('button', { name: 'Save manufacturer website' }).click()
  await frame.getByText('Manufacturer website saved.', { exact: false }).waitFor()
  assert.deepEqual(savedManufacturerWebsite, {
    manufacturer_id: 7, name: manufacturer.name, url: 'https://manufacturer.test/', record_token: editorRecordToken,
  })
  await frame.getByRole('button', { name: 'Stage 1 field' }).click()
  await frame.getByText('1 field staged.', { exact: false }).waitFor()
  assert.deepEqual(await page.evaluate(() => window.staged), [{ density_g_cm3: { old: null, value: 1.24 } }])
  await frame.locator('#research-options > summary').click()
  await frame.getByRole('button', { name: 'Find specifications' }).click()
  const researchPage = frame.getByRole('button', { name: 'Research this page' })
  await researchPage.waitFor()
  assert.equal(await researchPage.locator('svg[aria-hidden=true]').count(), 1)
  await researchPage.click()
  await frame.getByRole('log').getByText('Research this page', { exact: true }).waitFor()
  await densityChoice.waitFor()
  await frame.locator('#research-options > summary').click()
  await frame.getByRole('button', { name: 'Standardize this filament' }).click()
  await frame.getByRole('checkbox', { name: /Fixture-PLA.*Matte/ }).check()
  await followUp.fill('Check an unavailable source')
  await frame.getByRole('button', { name: 'Send follow-up' }).click()
  await frame.getByText('Source unavailable', { exact: true }).waitFor()
  assert.equal(await frame.getByRole('checkbox', { name: /Fixture-PLA.*Matte/ }).isChecked(), true)
  assert.equal(await followUp.inputValue(), 'Check an unavailable source')
  await followUp.fill('Focus on the material finish')
  await followUp.press('Enter')
  await frame.getByRole('log').getByText('Focus on the material finish', { exact: true }).waitFor()
  await frame.getByRole('log').getByText('Source unavailable', { exact: true }).waitFor()
  await frame.getByRole('checkbox', { name: /Fixture-PLA.*Silk/ }).check()
  assert.equal(await followUp.inputValue(), '')
  await frame.getByRole('button', { name: 'Stage 2 fields' }).click()
  await frame.getByText('2 fields staged.', { exact: false }).waitFor()
  assert.deepEqual(await page.evaluate(() => window.staged), [{
    designation: { old: 'Fixture PLA', value: 'Fixture-PLA' },
    material_subgroup: { old: null, value: 'Silk' },
  }])
  assert.equal(writes.length, 1)

  const requestsBeforeReopen = proposalRequests
  await page.reload()
  await frame.locator('[data-chat-history] summary').click({ timeout: 2000 })
  await frame.locator('[data-chat-history-list]').getByRole('button', { name: 'Find specifications', exact: true }).click()
  await frame.getByRole('log').getByText('Focus on the material finish', { exact: true }).waitFor()
  await frame.getByRole('checkbox', { name: /Fixture-PLA.*Silk/ }).waitFor()
  assert.equal(proposalRequests, requestsBeforeReopen, 'reopening editor research does not run another lookup')
  await frame.getByRole('checkbox', { name: /Fixture-PLA.*Silk/ }).check()
  await page.reload()
  await frame.locator('[data-chat-history] summary').click()
  await frame.locator('[data-chat-history-list]').getByRole('button', { name: 'Find specifications', exact: true }).click()
  await frame.getByRole('button', { name: 'Stage 2 fields' }).waitFor()
  await frame.getByRole('button', { name: 'Model & MCP settings' }).click()
  pairedProposal = {
    target: { mode: 'edit', filament_id: 10 },
    suggestions: [{ field: 'density_g_cm3', value: 1.28, sources: evidence }],
    issues: [],
  }
  await pair.getByRole('button', { name: 'Pair a client' }).click()
  await frame.locator('[data-chat-log]').getByText('A paired client sent a proposal.').waitFor({ state: 'attached' })
  assert.equal(await token.isVisible(), true, 'an incoming proposal leaves settings visible')
  await frame.getByRole('button', { name: 'Back to chat' }).click()
  await frame.getByRole('checkbox', { name: /Density.*1\.28/ }).waitFor()
  await page.reload()
  await frame.locator('[data-chat-history] summary').click()
  await frame.locator('[data-chat-history-list]').getByRole('button', { name: 'Find specifications', exact: true }).click()
  await frame.getByRole('checkbox', { name: /Density.*1\.28/ }).waitFor()
  pairedProposal = null
  for (const token of ['d'.repeat(32), null, 'unavailable']) {
    recordUnavailable = token === 'unavailable'
    editorRecordToken = token
    await page.reload()
    await frame.locator('[data-chat-history] summary').click()
    await frame.locator('[data-chat-history-list]').getByRole('button', { name: 'Find specifications', exact: true }).click()
    await frame.getByText(/Saved findings are read-only/).waitFor()
    await frame.getByText('Earlier findings', { exact: true }).first().waitFor()
    assert.equal(await frame.getByRole('checkbox', { name: /Density.*1\.28/ }).count(), 0)
    assert.equal(proposalRequests, requestsBeforeReopen)
  }

  recordUnavailable = false
  editorRecordToken = 'd'.repeat(32)
  hostEditorToken = 'c'.repeat(32)
  await page.reload()
  await frame.getByRole('button', { name: 'Find specifications' }).click()
  await frame.getByText(/The filament identity changed/).waitFor()
  assert.equal(proposalRequests, requestsBeforeReopen)
  hostEditorToken = undefined
  recordUnavailable = false
  editorMode = 'create'
  await page.reload()
  await frame.getByRole('button', { name: 'Find specifications' }).click()
  await frame.getByRole('checkbox', { name: /Density/ }).check()
  const createLookups = proposalRequests
  await frame.locator('[data-chat-history] summary').click()
  await frame.locator('[data-chat-history-list]').getByRole('button', { name: 'Find specifications', exact: true }).click()
  await frame.getByRole('button', { name: 'Stage 1 field' }).waitFor()
  assert.equal(proposalRequests, createLookups)

  hostPicker = 'legacy'
  supportsRecordToken = false
  await page.goto(`${url}?tab=library`)
  await page.getByLabel('Search filament library').waitFor()
  assert.equal(await page.locator('#library-picker').count(), 0)

  hostPicker = 'current'
  await page.goto(`${url}?tab=library`)
  const picker = page.frameLocator('#library-picker')
  const pickFilament = picker.getByRole('checkbox', { name: 'Fixture PLA' })
  await pickFilament.check()
  await page.getByText('1 of 50 selected').waitFor()
  assert.equal(await page.getByLabel('Search filament library').isVisible(), false)
  await clearSelection.click()
  await page.getByText('0 of 50 selected').waitFor()
  await pickFilament.locator('xpath=..').locator('input:not(:checked)').waitFor()
  assert.equal(await page.getByRole('button', { name: 'Start research' }).isDisabled(), true)
  await pickFilament.check()
  await page.getByRole('button', { name: 'Start research' }).click()
  await page.getByRole('heading', { name: 'Review queue' }).waitFor()

  await page.goto('https://fila.test/selection-fixture')
  await frame.getByText('1 of 50 selected').waitFor()
  assert.equal(await frame.getByLabel('Search filament library').isVisible(), false)
  await frame.getByRole('button', { name: 'Start research' }).click()
  await frame.getByRole('heading', { name: 'Review queue' }).waitFor()
  hostSendsIdentity = true
  staleBatchOnce = true
  await page.goto('https://fila.test/selection-fixture')
  const startHostBatch = frame.getByRole('button', { name: 'Start research' })
  await startHostBatch.click()
  await frame.getByText('The filament library changed. Select your filaments again.').waitFor()
  assert.equal(await startHostBatch.isDisabled(), true)
  await page.goto(url)
  await page.locator('[data-chat-history] summary').click()
  await page.getByRole('button', { name: /Find specifications · Fixture Brand Fixture PLA/ }).click()
  await page.getByRole('link', { name: 'Open saved filament' }).waitFor()
  await page.getByLabel('Product name', { exact: true }).fill('Fixture PETG')
  await page.getByLabel('Manufacturer', { exact: true }).selectOption('7')
  await page.getByText('Search options', { exact: true }).click()

  await page.getByLabel('Material', { exact: true }).fill('PETG')
  await page.getByLabel('Diameter (mm)').fill('1.75')
  await page.getByRole('button', { name: 'Find specifications' }).click()
  await page.getByRole('checkbox', { name: /Density/ }).check()
  const otherDraft = await context.newPage()
  otherDraft.setDefaultTimeout(5_000)
  await otherDraft.goto(url)
  await otherDraft.locator('[data-chat-history] summary').click()
  const researchTitle = /Find specifications · Fixture Brand Fixture PLA/
  await otherDraft.getByRole('button', { name: researchTitle }).click()
  await otherDraft.locator('#research-options > summary').click()
  await otherDraft.getByLabel('Product name', { exact: true }).fill('Fixture ASA')
  await otherDraft.getByRole('button', { name: 'Find specifications' }).click()
  await otherDraft.getByRole('checkbox', { name: /Density/ }).check()
  await otherDraft.getByRole('button', { name: 'Save new filament' }).click()
  const otherConfirm = otherDraft.getByRole('dialog').getByRole('button', { name: 'Create filament' })
  await otherConfirm.waitFor()

  await page.getByRole('button', { name: 'Save new filament' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Create filament' }).click()
  await page.getByRole('link', { name: 'Open saved filament' }).nth(1).waitFor()
  assert.equal(writes.length, 2)
  assert.equal(writes[1].designation, 'Fixture PETG')
  assert.notEqual(savePaths[1], savePaths[0])
  await otherConfirm.click()
  await otherDraft.getByRole('link', { name: 'Open saved filament' }).nth(1).waitFor()
  assert.deepEqual(writes.map(write => write.designation), ['Fixture PLA', 'Fixture PETG', 'Fixture ASA'])
  assert.equal(new Set(savePaths).size, 3)
  await otherDraft.reload()
  for (const tab of [otherDraft, page]) {
    await tab.locator('[data-chat-history] summary').click()
    await tab.getByRole('button', { name: researchTitle }).click()
    const savedLinks = tab.getByRole('link', { name: 'Open saved filament' })
    await savedLinks.first().waitFor()
    assert.deepEqual(
      await savedLinks.evaluateAll(links => links.map(link => link.getAttribute('href')).sort()),
      [...savePaths].sort(),
      'History keeps the original save and both saves made from separate tabs',
    )
    assert.equal(await tab.getByRole('button', { name: 'Save new filament' }).count(), 0)
  }
  console.log('FilaBrain plugin page: chat, selected create, batch approve/apply, MCP, linked editor staging and host selection passed')
} finally {
  await browser.close()
}
