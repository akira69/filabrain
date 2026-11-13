import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const html = await readFile(new URL('../filabrain/page.html', import.meta.url), 'utf8')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
const url = 'https://fila.test/plugin-page/filabrain'
const manufacturer = { id: 7, name: 'Fixture Brand' }
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
  const page = await browser.newPage()
  page.setDefaultTimeout(5_000)
  const writes = []
  let mcpEnabled = false
  let searchUrl = ''
  let supplierDownloadMiB = 8
  let supplierSites = []
  let savedManufacturerWebsite
  let chatModelId = 'fixture:default'
  let canConfigure = true
  let modelsUnavailable = false
  let hostPicker = false
  await page.route('https://fila.test/**', async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (path.startsWith('/plugin-page/')) return route.fulfill({ contentType: 'text/html', body: html })
    if (path === '/filament-picker/' && hostPicker) return route.fulfill({ contentType: 'text/html', body: `
      <label data-filament-picker><input type="checkbox"> Fixture PLA</label>
      <script>document.querySelector('input').onchange = event => parent.postMessage({
        type: 'filaman:filament-picker-selection', filament_ids: event.target.checked ? [10] : [],
      }, location.origin)</script>` })
    if (path === '/selection-fixture') return route.fulfill({ contentType: 'text/html', body: `
      <iframe sandbox="allow-scripts allow-same-origin allow-forms" src="${url}?filament_host=selection"></iframe>
      <script>window.addEventListener('message', event => {
        if (event.origin !== location.origin || event.source !== document.querySelector('iframe').contentWindow) return
        const { type, request_id } = event.data;
        if (type === 'filaman:filament-context') event.source.postMessage({type, request_id, mode:'selection', filament_ids:[10]}, location.origin)
      })</script>` })
    if (path === '/editor-fixture') return route.fulfill({ contentType: 'text/html', body: `
      <iframe style="width: 640px; height: 520px" sandbox="allow-scripts allow-same-origin allow-forms" src="${url}?filament_host=editor"></iframe>
      <script>
        window.addEventListener('message', event => {
          if (event.origin !== location.origin || event.source !== document.querySelector('iframe').contentWindow) return
          const { type, request_id, units } = event.data;
          if (type === 'filaman:filament-context') event.source.postMessage({type, request_id, mode:'edit',
            filament_id:10, fields:{ designation:'Fixture PLA', manufacturer_id:7, manufacturer_name:'Fixture Brand', material_type:'PLA', diameter_mm:1.75 }}, location.origin)
          if (type === 'filaman:filament-stage') {
            window.staged = units;
            event.source.postMessage({type, request_id, staged:units.flatMap(unit => Object.keys(unit)), skipped:[]}, location.origin)
          }
        })
      </script>` })
    if (path.endsWith('/status')) return route.fulfill({ json: { enabled: true, can_chat: true, chat_model: chatModelId,
      can_create: true, can_update: true, can_update_manufacturer: true, can_configure: canConfigure, mcp_enabled: mcpEnabled } })
    if (path.endsWith('/filaments/10/manufacturer-website') && req.method() === 'PUT') {
      savedManufacturerWebsite = req.postDataJSON()
      return route.fulfill({ status: 204 })
    }
    if (path.endsWith('/models')) return modelsUnavailable
      ? route.fulfill({ status: 502, json: {} })
      : route.fulfill({ json: { models: ['fixture:default', 'fixture:alternate'] } })
    if (path.endsWith('/chat')) {
      assert.equal(req.postDataJSON().messages[0].content, 'Compare these materials')
      assert.equal(req.postDataJSON().model_id, 'fixture:alternate')
      return route.fulfill({ json: { reply: 'PLA is easier to print.', action: null } })
    }
    if (path.endsWith('/proposals')) {
      const target = req.postDataJSON()
      if (target.filament_id === 10 && target.action === 'fill') assert.equal(target.model_id, 'fixture:alternate')
      if (target.product_url) assert.equal(target.product_url, evidence[0].url)
      if (target.guidance) {
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
        { code: 'manufacturer_color_reference', name: 'RAL 9005 Signal Black', url: evidence[0].url },
        { code: 'manufacturer_website_found', name: manufacturer.name, source: 'FilaManDB', url: 'https://manufacturer.test/' },
        { code: 'possible_match', name: 'Fixture PLA', source: 'FilaManDB', url: evidence[0].url, product_url: evidence[0].url },
      ] : [] } })
    }
    if (path.endsWith('/settings/mcp')) {
      mcpEnabled = req.postDataJSON().mcp_enabled
      return route.fulfill({ json: { mcp_enabled: mcpEnabled } })
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
    if (path.endsWith('/filaments') && req.method() === 'GET') return route.fulfill({ json: { items: [filament], total: 1 } })
    if (path.endsWith('/filaments') && req.method() === 'POST') {
      writes.push(req.postDataJSON())
      return route.fulfill({ json: { id: 11 } })
    }
    if (path.endsWith('/batches') && req.method() === 'POST') {
      assert.deepEqual(req.postDataJSON(), { filament_ids: [10], mode: 'fill' })
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
  const firstSearchPicker = page.locator('#product-research [data-model-trigger]')
  await firstSearchPicker.waitFor()
  assert.equal(await page.getByLabel('Material', { exact: true }).isVisible(), false)
  await page.getByText('Search options', { exact: true }).click()
  await page.getByLabel('Material', { exact: true }).fill('PLA')
  await page.getByLabel('Diameter (mm)').fill('1.75')
  await page.getByRole('button', { name: 'Find specifications' }).click()
  await page.getByRole('link', { name: 'Fixture catalog' }).first().waitFor()
  await page.locator('#research-follow-up [data-model-trigger]').waitFor()
  await page.getByRole('checkbox', { name: /Density/ }).check()
  assert.equal(writes.length, 0)
  await page.getByRole('button', { name: 'Save new filament' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Create filament' }).click()
  await page.getByRole('link', { name: 'Open saved filament' }).waitFor()
  assert.deepEqual(writes, [{ manufacturer_id: 7, designation: 'Fixture PLA', material_type: 'PLA',
    diameter_mm: 1.75, density_g_cm3: 1.24 }])

  await page.getByRole('button', { name: 'Research library' }).click()
  await page.getByRole('checkbox', { name: /Fixture PLA/ }).check()
  await page.getByRole('button', { name: 'Start research' }).click()
  await page.getByText('Confirmed density').waitFor()
  await page.getByRole('checkbox', { name: /Density/ }).check()
  await page.getByRole('button', { name: /Approve/ }).click()
  await page.getByRole('button', { name: 'Apply approved changes' }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Apply/ }).click()
  await page.getByRole('heading', { name: 'Apply report' }).waitFor()

  await page.getByRole('button', { name: 'Model & MCP settings' }).click()
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
  await searchField.fill('http://searxng:8080')
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
  const researchModel = frame.getByRole('button', { name: /^Model for this conversation/ })
  const researchMenu = frame.getByRole('menu', { name: 'Model for this conversation' })
  await researchModel.click()
  await researchMenu.getByRole('menuitemradio', { name: 'fixture:default Default' }).press('Escape')
  await researchMenu.waitFor({ state: 'hidden' })
  await researchModel.click()
  await researchMenu.getByRole('menuitemradio', { name: 'fixture:alternate' }).click()
  await researchMenu.waitFor({ state: 'hidden' })
  await frame.getByText('Advanced options', { exact: true }).waitFor()
  assert.equal(await frame.getByRole('button', { name: 'Pair a client' }).isVisible(), false)
  assert.equal(await followUp.isVisible(), false)
  await frame.getByRole('button', { name: 'Find specifications' }).click()
  const densityChoice = frame.getByRole('checkbox', { name: /Density/ })
  await densityChoice.waitFor()
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
    manufacturer_id: 7, name: manufacturer.name, url: 'https://manufacturer.test/',
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
  await frame.getByRole('checkbox', { name: /Fixture-PLA.*Silk/ }).check()
  assert.equal(await followUp.inputValue(), '')
  await frame.getByRole('button', { name: 'Stage 2 fields' }).click()
  await frame.getByText('2 fields staged.', { exact: false }).waitFor()
  assert.deepEqual(await page.evaluate(() => window.staged), [{
    designation: { old: 'Fixture PLA', value: 'Fixture-PLA' },
    material_subgroup: { old: null, value: 'Silk' },
  }])
  assert.equal(writes.length, 1)

  hostPicker = true
  await page.goto(`${url}?tab=library`)
  const picker = page.frameLocator('#library-picker')
  const pickFilament = picker.getByRole('checkbox', { name: 'Fixture PLA' })
  await pickFilament.check()
  await page.getByText('1 of 50 selected').waitFor()
  assert.equal(await page.getByLabel('Search filament library').isVisible(), false)
  await pickFilament.uncheck()
  await page.getByText('0 of 50 selected').waitFor()
  assert.equal(await page.getByRole('button', { name: 'Start research' }).isDisabled(), true)
  await pickFilament.check()
  await page.getByRole('button', { name: 'Start research' }).click()
  await page.getByRole('heading', { name: 'Review queue' }).waitFor()

  await page.goto('https://fila.test/selection-fixture')
  await frame.getByText('1 of 50 selected').waitFor()
  assert.equal(await frame.getByLabel('Search filament library').isVisible(), false)
  await frame.getByRole('button', { name: 'Start research' }).click()
  await frame.getByRole('heading', { name: 'Review queue' }).waitFor()
  console.log('FilaBrain plugin page: chat, research sources, selected create, batch approve/apply, MCP, linked editor staging and host selection passed')
} finally {
  await browser.close()
}
