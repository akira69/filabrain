import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const html = await readFile(new URL('../filabrain/page.html', import.meta.url), 'utf8')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
const url = 'https://fila.test/plugin-page/filabrain'
async function openResearchDetails(page) {
  const details = page.locator('#research-options')
  if (!await details.evaluate(element => element.open)) await details.locator(':scope > summary').click()
}
const manufacturer = { id: 7, name: 'Fixture Brand' }
const pickerRecordToken = 'a'.repeat(32)
const filament = {
  id: 10, designation: 'Fixture PLA', manufacturer_id: 7, manufacturer,
  material_type: 'PLA', diameter_mm: 1.75, color_mode: 'single',
}
const evidence = [{ title: 'Fixture catalog', url: 'https://manufacturer.test/pla', evidence: '1.24 g/cm³' }]
const longProductUrl = evidence[0].url + '?reference=' + 'x'.repeat(500)
const refillUrl = 'https://manufacturer.test/pla-refill'
const rotatingVariantUrl = 'https://manufacturer.test/pla-select-package'
const batchSource = { title: 'Fixture catalog | Fixture Brand', url: 'https://manufacturer.test/pla', quote: 'Fixture catalog' }
const fixtureBatch = {
  id: 'fixture-batch', mode: 'fill', status: 'active', total: 1,
  created_at: '2026-01-01T00:00:00Z', finished_at: null,
  items: [{ id: 12, filament_id: 10, live_filament_id: 10, status: 'ready', decision: 'pending',
    revision: 1, decision_revision: 0, identity: { designation: 'Fixture PLA' },
    issues: [
      { code: 'manufacturer_page', name: batchSource.title, url: batchSource.url },
      { code: 'possible_match', name: 'Other PLA', url: 'https://manufacturer.test/other', source: 'Web search' },
      { code: 'possible_match', name: 'Other PLA', url: 'https://manufacturer.test/other', source: 'Web search' },
      { code: 'source_disagreement', field: 'density_g_cm3', options: [
        { value: 1.24, sources: [{ ...evidence[0], title: 'Fixture manufacturer', evidence: 'Density for Fixture PLA: 1.24 g/cm³' }] },
        { value: 1.30, sources: [{ title: 'Reseller', url: 'https://reseller.test/pla', evidence: 'Reseller lists 1.30 g/cm³' }] },
      ] },
    ],
    approved_ids: [], changed_fields: [], applied_at: null,
    proposals: [{ id: 'density', fields: { density_g_cm3: { old: null, value: 1.24 } },
      sources: [batchSource, batchSource], reason: 'Confirmed density', group_key: null }],
  }],
}

try {
  const context = await browser.newContext()
  const page = await context.newPage()
  page.setDefaultTimeout(5_000)
  const writes = []
  const savePaths = []
  let searchUrl = ''
  let chatLiveSources = true
  let microphoneEnabled = true
  let supplierDownloadMiB = 8
  let supplierSites = []
  let savedManufacturerWebsite
  let chatModelId = 'fixture:default'
  let endpoints = []
  let defaultEndpoint = null
  let canConfigure = true
  let canUseFilaments = true
  let canAddSpool = false
  let scaleRequested = false
  let stockRequested = false
  let scaleResultReads = 0
  let scalePassiveReads = 0
  let passiveScanReads = 0
  const scanWrites = []
  let modelsUnavailable = false
  let hostPicker = false
  let account = 7
  let chatRequests = 0
  let proposalRequests = 0
  let draftColorMode = false
  const colorWrites = []
  const manufacturerWrites = []
  let makerResponse
  let heldContextLoad
  const sharedChatRequests = []
  let colorResponse
  let swatchResponse
  let canCreatePalette = true
  let duplicateSpoolLookup = false
  let supplierDefaultSku = 'FIX-123'
  let conflictingSpoolWidth = false
  let differentSpoolPackage = false
  let heldModelList
  let staleBatchOnce = false
  let hasResearchRun = false
  let libraryReads = 0
  let editorRecordToken = 'c'.repeat(32)
  let hostEditorToken
  let editorMode = 'edit'
  let recordUnavailable = false
  const variantWarning = 'Choose the product page before selecting a value.'
  const variantIssue = {
    code: 'conflict', field: 'drying_temp_c', requires_product_choice: true,
    options: [
      { value: 50, sources: [{ title: 'Variant A datasheet', url: 'https://manufacturer.test/a.pdf' }] },
      { value: 60, sources: [{ title: 'Variant B datasheet', url: 'https://manufacturer.test/b.pdf' }] },
    ],
  }
  let supportsRecordToken = true
  let hostSendsIdentity = false
  let holdStage = false
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
            filament_id:${editorMode === 'edit' ? 10 : 'null'}, record_token:${JSON.stringify(hostEditorToken)}, capabilities:{color_staging:true,manufacturer_staging:true}, fields:{ designation:'Fixture PLA', manufacturer_id:7, manufacturer_name:'Fixture Brand', material_type:'PLA', diameter_mm:1.75, ...window.editorEdits }}, location.origin)
          if (type === 'filaman:filament-stage') {
            window.staged = units;
            const reply = () => event.source.postMessage({type, request_id, staged:units.flatMap(unit => Object.keys(unit)), skipped:[]}, location.origin)
            if (${JSON.stringify(holdStage)}) window.finishStage = reply;
            else reply()
          }
        })
      </script>` })
    if (path.endsWith('/me')) return route.fulfill({ json: { id: account } })
    if (path === '/api/v1/filamentdb/spool-profile-image') return route.fulfill({
      contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"/>',
    })
    if (path === '/api/v1/devices/active') return route.fulfill({ json: [
      { id: 4, name: 'SpoolmanScale' }, { id: 8, name: 'FilaMan ESP32' },
    ] })
    if (path === '/api/v1/tag/readers') return route.fulfill({ json: [
      { reader_id: 'spoolmanscale-A1B2C3', name: 'SpoolmanScale' },
      { reader_id: 'usb-nfc', name: 'NFC reader' },
    ] })
    if (path === '/api/v1/tag/last-scan') {
      const reader = new URL(req.url()).searchParams.get('reader_id')
      if (reader === 'spoolmanscale-A1B2C3') {
        return route.fulfill({ json: ++scalePassiveReads === 1
          ? { reader_id: reader, seq: 21, uid: '0201' }
          : { reader_id: reader, seq: 22, uid: '0202', spool_id: 19 } })
      }
      if (reader === 'usb-nfc') {
        passiveScanReads++
        return route.fulfill({ json: passiveScanReads < 3
          ? { reader_id: reader, seq: 100, uid: '0101', spool_id: 12 }
          : { reader_id: reader, seq: 101, uid: '0102', spool_id: 17 },
        })
      }
      return route.fulfill({ json: { seq: 0 } })
    }
    if (path === '/api/v1/devices/8/request-tag-scan') {
      stockRequested = true
      scanWrites.push(path)
      return route.fulfill({ json: { success: true } })
    }
    if (path === '/api/v1/devices/8/tag-scan-result') {
      return route.fulfill({ json: stockRequested ? {
        status: 'success', timestamp: '2026-11-16T12:02:00Z',
        tag_data: { protocol: 'filaman', brand: 'Fixture Brand', type: 'PLA', color_hex: '#33AA55' },
      } : { status: 'none' } })
    }
    if (path === '/api/v1/devices/4/request-tag-scan') {
      scaleRequested = true
      scanWrites.push(path)
      return route.fulfill({ json: { success: true } })
    }
    if (path === '/api/v1/devices/4/tag-scan-result') {
      const fresh = scaleRequested && ++scaleResultReads > 1
      return route.fulfill({ json: {
        status: 'success',
        timestamp: fresh ? '2026-11-16T12:01:00Z' : '2026-11-16T12:00:00Z',
        tag_data: fresh
          ? { brand: 'Fixture Brand', type: 'PLA', color_hex: '33AA55', min_temp: '200', max_temp: '220', sm_id: 32 }
          : { brand: 'Old result', type: 'PETG', color_hex: 'FF0000', sm_id: 99 },
      } })
    }
    if (path === '/api/v1/filaments' && new URL(req.url()).searchParams.get('search') === 'FIX-123') {
      return route.fulfill({ json: { items: [], total: 0 } })
    }
    if (path === '/api/v1/filabrain/spool-filaments') {
      const params = new URL(req.url()).searchParams
      if (params.get('exact') === 'FIX-123') return route.fulfill({ json: { items: [filament], total: 1 } })
      assert.equal(params.get('search'), 'Bambu Lab PLA Basic Jade White')
      return route.fulfill({ json: { items: [{
        ...filament, id: 26, designation: 'PLA Basic', manufacturer: { name: 'Bambu Lab' },
        manufacturer_color_name: 'Jade White',
      }], total: 1 } })
    }
    if (path === '/api/v1/filaments' && new URL(req.url()).searchParams.get('type') === 'PLA') {
      assert.equal(new URL(req.url()).searchParams.get('search'), 'Fixture Brand')
      return route.fulfill({ json: { items: [
        { ...filament, colors: [{ color: { hex_code: '#33AA55' } }] },
        { ...filament, id: 11, designation: 'Other Color', colors: [{ color: { hex_code: '#112233' } }] },
      ], total: 2 } })
    }
    if (path.endsWith('/status')) return route.fulfill({ json: { enabled: true, can_chat: canUseFilaments, chat_model: chatModelId, chat_endpoint_id: chatModelId ? defaultEndpoint : null,
      can_create: canUseFilaments, can_create_color: canCreatePalette, can_create_manufacturer: true, can_add_spool: canAddSpool, can_read_spools: canAddSpool, can_update: canUseFilaments, can_update_manufacturer: true, can_configure: canConfigure } })
    if (path.endsWith('/filaments/10/research-record')) return recordUnavailable
      ? route.fulfill({ status: 503, json: {} })
      : route.fulfill({ json: { record_token: editorRecordToken } })
    if (path.endsWith('/filaments/10/manufacturer-website') && req.method() === 'PUT') {
      savedManufacturerWebsite = req.postDataJSON()
      return route.fulfill({ status: 204 })
    }
    if (path.endsWith('/settings/models')) {
      if (heldModelList) await heldModelList
      const { model_url } = req.postDataJSON()
      if (model_url.includes('offline')) return route.fulfill({ status: 502, json: { detail: 'Could not load models from this server.' } })
      return route.fulfill({ json: { models: model_url.includes('remote')
        ? ['fixture:alternate', 'fixture:hidden'] : ['fixture:default', 'fixture:alternate'] } })
    }
    if (path.endsWith('/models')) return modelsUnavailable
      ? route.fulfill({ status: 502, json: {} })
      : route.fulfill({ json: {
        default_endpoint_id: defaultEndpoint || 'default',
        default_model_id: chatModelId,
        groups: (endpoints.length ? endpoints : [{ id: 'default', name: 'Default endpoint', models: null, hidden_models: [] }])
          .map(({ id, name, models, hidden_models, model_id }) => ({ id, name, models: [...new Set([
            ...(model_id ? [model_id] : []), ...(models?.length ? models : id === defaultEndpoint || id === 'default'
              ? ['fixture:default', 'fixture:alternate'] : []),
          ])].filter(model => !hidden_models.includes(model)) })),
      } })
    if (path.endsWith('/chat') && req.postDataJSON().messages.at(-1)?.content === 'Explain this filament') {
      sharedChatRequests.push(req.postDataJSON())
      return route.fulfill({ json: { reply: 'PLA is easier to print.', action: null } })
    }
    if (path.endsWith('/chat')) {
      assert.equal(req.postDataJSON().messages[0].content, 'Compare these materials')
      if (chatRequests++ === 0) {
        assert.equal(req.postDataJSON().model_id, 'fixture:alternate')
        assert.equal(req.postDataJSON().endpoint_id, defaultEndpoint)
      }
      else assert.deepEqual(req.postDataJSON().messages.map(message => message.role), ['user', 'assistant', 'user'])
      return route.fulfill({ json: { reply: 'PLA is easier to print.', action: null } })
    }
    if (path.endsWith('/proposals')) {
      proposalRequests++
      const target = req.postDataJSON()
      if (target.spool_lookup) return route.fulfill({ json: {
        target, issues: duplicateSpoolLookup ? [{ code: 'duplicate', filament_id: 10 }] : [],
        suggestions: [], spool_reply: 'Which spool matches yours?',
        spool_profiles: [{ id: 'example-spool', title: 'Fixture Brand PLA 1 kg', source: 'OnlySpoolz',
          url: 'https://www.onlyspoolz.com/portfolio/spool-fixture/', empty_weight_g: 233,
          outer_diameter_mm: 200, width_mm: 66, hole_diameter_mm: 58, net_weight_g: 1000 },
        ...(target.identity?.sku === 'FIX-123' ? [] : [{
          id: 'filamandb-735', title: '170g Spool', source: 'FilaManDB',
          url: 'https://db.filaman.app/api/v1/spool-profiles/735',
          photo: '/api/v1/filamentdb/spool-profile-image?image_file=735.png', empty_weight_g: 170,
          ...(differentSpoolPackage ? { net_weight_g: 1000 } : { package_weights_g: [] }) }]),
        ...(differentSpoolPackage ? [{
          id: 'filamandb-736', title: 'Shared Reel', source: 'FilaManDB',
          url: 'https://db.filaman.app/api/v1/spool-profiles/736', empty_weight_g: 175,
          package_weights_g: [1000, 2000],
        }, {
          id: 'filamandb-737', title: 'Unlisted Package Weight Reel', source: 'FilaManDB',
          url: 'https://db.filaman.app/api/v1/spool-profiles/737', empty_weight_g: 181,
          ...(target.spool_package_weight_g ? { package_weights_g: [] } : {}),
        }] : []),
        ...(target.identity?.weight_g === 1000 ? [{
          id: 'other-package', title: 'Fixture Brand PLA 3 kg', source: 'OnlySpoolz',
          url: 'https://www.onlyspoolz.com/portfolio/spool-fixture-3kg/', empty_weight_g: 322, net_weight_g: 3000,
        }] : [])],
      } })
      if (target.query) {
        assert.equal(target.model_id, undefined, 'discovery does not use an LLM')
        assert.equal(target.endpoint_id, undefined)
        assert.equal(target.mode, target.filament_id ? 'edit' : 'create')
        assert.equal(target.identity.sku, target.query === 'explicit SKU fixture' ? 'FIX-12X' : '')
        assert.equal(target.sku_hint, '')
        const candidate = {
          code: 'possible_match', name: '<Fixture purple PLA>', source: 'Fixture catalog', source_product_code: 'SHOP-789',
          url: evidence[0].url, product_url: evidence[0].url, match_kind: 'similar_sku',
          identity: { manufacturer: 'Fixture Brand', designation: 'Fixture purple PLA', sku: 'FIX-123',
            diameter_mm: 1.75, weight_g: 1000, color: 'Purple' },
        }
        const discoverySku = ['FIX-12X', 'FIX 123'].includes(target.query) ? target.query : undefined
        return route.fulfill({ json: { target, discovery_sku: discoverySku, suggestions: [], issues: [candidate, candidate,
          ...(discoverySku ? [{ ...candidate, product_url: rotatingVariantUrl, url: rotatingVariantUrl,
            identity: { ...candidate.identity, sku: 'FIX-285', diameter_mm: 2.85, weight_g: 500, color: 'Blue' },
          }] : []),
          { code: 'search_incomplete', source: 'Unavailable catalog' },
        ] } })
      }
      if (target.guidance === 'Recheck this density') return route.fulfill({ json: { target, issues: [], suggestions: [
        { field: 'density_g_cm3', value: 1.27, sources: evidence },
      ] } })
      if (chatModelId && target.filament_id === 10 && target.action === 'fill') {
        assert.equal(target.model_id, 'fixture:alternate')
        assert.equal(target.endpoint_id, endpoints.find(endpoint => endpoint.name === 'Remote models')?.id || defaultEndpoint)
      }
      if (target.product_url === refillUrl) return route.fulfill({ json: { target, issues: [], suggestions: [
        { field: 'density_g_cm3', value: 1.22, sources: [{ title: 'Refill specifications', url: refillUrl, evidence: '1.22 g/cm³' }] },
        { field: 'manufacturer_sku', value: 'FIX-REFILL', sources: [{ title: 'Refill specifications', url: refillUrl, evidence: 'FIX-REFILL' }] },
      ] } })
      if (target.product_url === rotatingVariantUrl) {
        const refill = !target.identity.sku && target.guidance === 'Check the density again'
        return route.fulfill({ json: { target, issues: [], suggestions: [
          ...(!target.identity.sku ? [{ field: 'manufacturer_sku', value: refill ? 'NEW-PACKAGE' : 'OLD-PACKAGE', sources: evidence }] : []),
          { field: 'raw_material_weight_g', value: refill ? 500 : 1000, sources: evidence },
          { field: 'density_g_cm3', value: refill ? 1.22 : 1.24, sources: evidence },
        ] } })
      }
      if (target.product_url === longProductUrl) return route.fulfill({ json: { target, issues: [], suggestions: [
        { field: 'density_g_cm3', value: 1.24, sources: evidence },
      ] } })
      if (target.product_url) assert.equal(target.product_url, evidence[0].url)
      if (target.guidance === 'Continue in stale tab') return route.fulfill({
        status: 502, json: { detail: 'Source unavailable' },
      })
      if (target.guidance && target.mode === 'create') assert.equal(target.guidance, 'Look for a datasheet')
      if (chatModelId && target.guidance && target.mode === 'edit') {
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
      if (target.reference_url === 'https://manufacturer.test/specs.pdf') return route.fulfill({ json: { target,
        suggestions: [
          { field: 'density_g_cm3', value: 1.27, sources: [{ ...evidence[0], evidence: '1.27 g/cm³' }] },
          { field: 'manufacturer_sku', value: 'FIX-123', sources: evidence },
          { field: 'bed_temp_range_c', value: { min: 42, max: 42 }, sources: evidence },
        ], issues: [],
      } })
      return route.fulfill({ json: { target, suggestions: [
        { field: 'density_g_cm3', value: 1.24, sources: evidence },
        ...(!target.identity?.sku ? [{ field: 'manufacturer_sku', value: supplierDefaultSku, sources: evidence }] : []),
        ...(conflictingSpoolWidth ? [{ field: 'spool_width_mm', value: 72, sources: evidence }] : []),
        ...(differentSpoolPackage ? [{ field: 'raw_material_weight_g', value: 500, sources: evidence }] : []),
        ...(!chatModelId ? [{ field: 'drying_temp_c', value: 50, sources: evidence }] : []),
      ], issues: draftColorMode ? [{ code: 'manufacturer_color_reference', name: 'RAL 4007 Purple / Decepticon', reference_code: 'RAL 4007', url: evidence[0].url }] : target.mode === 'edit' ? [
        { code: 'manufacturer_page', name: 'Fixture datasheet', url: evidence[0].url },
        { code: 'manufacturer_page', name: 'Fixture datasheet', url: evidence[0].url },
        { code: 'manufacturer_color_reference', name: 'RAL 9005 Signal Black', url: evidence[0].url },
        { code: 'manufacturer_website_found', name: manufacturer.name, source: 'FilaManDB', url: 'https://manufacturer.test/' },
        { code: 'possible_match', name: 'Fixture PLA', source: 'FilaManDB', url: evidence[0].url, product_url: evidence[0].url },
        ...(target.product_url ? [
          variantIssue,
          { code: 'ambiguous_match', source: 'Fixture catalog' },
          { code: 'unavailable_source', source: 'Fixture catalog', url: evidence[0].url, reason: 'Timed out' },
        ] : []),
      ] : [] } })
    }
    if (path.endsWith('/settings/test-search')) {
      assert.deepEqual(req.postDataJSON(), { search_url: 'http://searxng:8080' })
      return route.fulfill({ json: { ok: true } })
    }
    if (path.endsWith('/settings/test')) {
      const { endpoint_id, ...credentials } = req.postDataJSON()
      assert.equal(endpoint_id, defaultEndpoint)
      assert.deepEqual(credentials, {
        model_url: 'http://ollama:11434', model_id: 'fixture:default', api_key: 'fixture-key', clear_key: false,
      })
      return route.fulfill({ json: { ok: true } })
    }
    if (path.endsWith('/settings')) {
      const settings = req.method() === 'PUT' ? req.postDataJSON() : null
      if (settings) {
        searchUrl = settings.search_url
        chatLiveSources = settings.chat_live_sources ?? chatLiveSources
        microphoneEnabled = settings.microphone_enabled ?? microphoneEnabled
        supplierDownloadMiB = settings.supplier_download_mib
        supplierSites = settings.supplier_sites ?? supplierSites
        if (settings.model_endpoints) {
          endpoints = settings.model_endpoints.map(({ api_key, clear_key, ...endpoint }) => ({ ...endpoint, has_key: !!api_key && !clear_key }))
          defaultEndpoint = settings.default_endpoint_id
        }
        if (settings.model_id || defaultEndpoint) chatModelId = settings.model_id || endpoints.find(endpoint => endpoint.id === defaultEndpoint).model_id
      }
      return route.fulfill({ json: {
        model_url: endpoints.find(endpoint => endpoint.id === defaultEndpoint)?.model_url || '', model_id: chatModelId,
        model_endpoints: endpoints, default_endpoint_id: defaultEndpoint,
        search_url: searchUrl, chat_live_sources: chatLiveSources, microphone_enabled: microphoneEnabled, has_key: false,
        supplier_sites: supplierSites, supplier_download_mib: supplierDownloadMiB,
      } })
    }
    if (path.endsWith('/manufacturers') && req.method() === 'POST') {
      const data = req.postDataJSON(); manufacturerWrites.push(data); if (makerResponse) await makerResponse
      return route.fulfill({ status: 201, json: { id: 9 + manufacturerWrites.length, ...data } })
    }
    if (path.endsWith('/manufacturers')) return route.fulfill({ json: { items: [manufacturer], total: 1 } })
    if (path.endsWith('/color-previews')) {
      if (swatchResponse) await swatchResponse
      const data = req.postDataJSON()
      return route.fulfill({ json: { current: [], can_save: true, can_assign: true, can_create: canCreatePalette,
        references: data.references.map(reference => ({ ...reference, name: 'Purple violet', hex_code: '#44263C',
          source: 'https://www.ral-farben.de/en/colour/ral-classic/ral-4007', saved: false, token: '',
          matches: [{ id: 1, name: 'RAL4007', hex_code: '#44263C' }],
        })),
      } })
    }
    if (path === '/api/v1/colors/1') return route.fulfill({ json: { id: 1, name: 'RAL4007', hex_code: '#44263c' } })
    if (path.endsWith('/colors') && req.method() === 'POST') {
      const data = req.postDataJSON()
      colorWrites.push(data)
      if (colorResponse) await colorResponse
      return route.fulfill({ status: 201, json: { id: 22 + colorWrites.length, ...data } })
    }
    if (path.endsWith('/colors')) return route.fulfill({ json: { items: [{ id: 1, name: 'Fixture Red' }], total: 1 } })
    if (path.endsWith('/filaments') && req.method() === 'GET') {
      if (heldContextLoad) await heldContextLoad
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
      hasResearchRun = true
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
      const changed_fields = ['density_g_cm3']
      Object.assign(fixtureBatch.items[0], { status: 'applied', applied_at: '2026-01-01T00:00:00Z', changed_fields })
      return route.fulfill({ json: { batch: fixtureBatch, results: [{ item_id: 12, status: 'applied', changed_fields }] } })
    }
    if (path.endsWith('/batches')) return route.fulfill({ json: hasResearchRun ? [fixtureBatch] : [] })
    return route.fulfill({ status: 404, json: {} })
  })

  canAddSpool = true
  const intake = await context.newPage()
  intake.setDefaultTimeout(5_000)
  await intake.goto(`${url}?tab=spool`)
  const readerChoice = intake.getByRole('combobox', { name: 'Reader', exact: true })
  await readerChoice.locator('option', { hasText: 'FilaMan ESP32' }).waitFor({ state: 'attached' })
  await readerChoice.selectOption('device-4')
  await intake.locator('#spool-read-tag').click()
  const useFilament = intake.getByRole('link', { name: 'Use this filament' })
  await intake.getByText(/^Tag read · Fixture Brand/).waitFor()
  assert.equal(await useFilament.count(), 0)
  assert.equal(await intake.getByRole('button', { name: 'Research new filament' }).count(), 0)
  assert.equal(await intake.getByRole('link', { name: 'Choose in FilaMan' }).count(), 0)
  assert.equal(await intake.getByRole('link', { name: 'Check existing spools' }).getAttribute('href'), '/spools')
  assert.equal(await intake.locator('a[href="/spools/32"]').count(), 0)
  await intake.getByText(/suggests spool #32/).waitFor()
  await intake.setViewportSize({ width: 320, height: 700 })
  assert.equal(await intake.evaluate(() => document.documentElement.scrollWidth), 320)
  assert.equal(await readerChoice.locator('option', { hasText: 'SpoolmanScale' }).count(), 2)
  assert.equal(await readerChoice.locator('option:checked').textContent(), 'SpoolmanScale · Filament details')
  await intake.getByRole('button', { name: 'Find filament for a new spool' }).click()
  await useFilament.waitFor()
  assert.equal(await useFilament.getAttribute('href'), '/spools/new?filament_id=10')
  assert.equal(await intake.getByText('Old result').isVisible(), false)
  assert.equal(await intake.getByText('Other Color').isVisible(), false)
  await intake.getByRole('button', { name: 'Research new filament' }).click()
  assert.equal(await intake.locator('#draft-manufacturer').inputValue(), 'Fixture Brand')
  assert.equal(await intake.locator('#product-research input[name="material_type"]').inputValue(), 'PLA')
  await intake.locator('[data-tab="spool"]').click()
  const openSpool = intake.getByRole('link', { name: 'Open existing spool' })
  const checkTag = intake.getByRole('button', { name: 'Check tag for linked spool' })
  await readerChoice.selectOption({ label: 'SpoolmanScale · Spool link' })
  await checkTag.click()
  await openSpool.waitFor()
  assert.equal(await openSpool.getAttribute('href'), '/spools/19')
  await readerChoice.selectOption({ label: 'NFC reader · Spool link' })
  await checkTag.click()
  await openSpool.waitFor()
  assert.equal(await openSpool.getAttribute('href'), '/spools/17')
  await readerChoice.selectOption({ label: 'FilaMan ESP32 · Filament details' })
  await intake.getByRole('button', { name: 'Read filament details' }).click()
  await intake.getByRole('button', { name: 'Find filament for a new spool' }).waitFor()
  assert.equal(await useFilament.count(), 0)
  await intake.getByRole('button', { name: 'Find filament for a new spool' }).click()
  await useFilament.waitFor()
  assert.equal(await useFilament.getAttribute('href'), '/spools/new?filament_id=10')
  assert.deepEqual(scanWrites, ['/api/v1/devices/4/request-tag-scan', '/api/v1/devices/8/request-tag-scan'])
  await intake.getByLabel('Product name, SKU, or URL').fill('FIX-123')
  await intake.getByRole('button', { name: 'Find in library' }).click()
  await useFilament.waitFor()
  assert.equal(await useFilament.getAttribute('href'), '/spools/new?filament_id=10')
  await intake.getByLabel('Product name, SKU, or URL').fill('Bambu Lab PLA Basic Jade White')
  await intake.getByRole('button', { name: 'Find in library' }).click()
  await useFilament.waitFor()
  assert.equal(await useFilament.getAttribute('href'), '/spools/new?filament_id=26')
  assert.equal(await intake.evaluate(() => document.documentElement.scrollWidth), 320)
  await intake.close()
  canAddSpool = false

  chatModelId = ''
  await page.goto(url)
  const setupModel = page.getByRole('button', { name: 'Set up a model' })
  await setupModel.waitFor()
  assert.equal(await page.getByRole('button', { name: 'Compare materials' }).isVisible(), false)
  assert.equal(await page.getByRole('textbox', { name: 'Your question' }).isVisible(), false)
  await setupModel.click()
  const modelUrl = page.getByRole('textbox', { name: 'Model server URL' })
  await page.waitForFunction(() => document.activeElement === document.querySelector('[name="model_url"]'))
  await modelUrl.fill('http://ollama:11434')
  const modelId = page.getByRole('textbox', { name: 'Model ID', exact: true })
  assert.equal(await modelId.isVisible(), false)
  await page.locator('#filabrain-settings').getByText('Enter a model ID manually', { exact: true }).click()
  await modelId.fill('fixture:default')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await page.getByText('Settings saved.').waitFor()
  await page.getByRole('tab', { name: 'Chat', exact: true }).click()
  await page.getByRole('textbox', { name: 'Your question' }).waitFor()
  assert.equal(await setupModel.isVisible(), false)
  assert.equal(await page.getByRole('button', { name: 'Guide', exact: true }).count(), 1)
  assert.equal(await page.locator('#plugin-header [data-tab]:not(:has(svg[aria-hidden="true"]))').count(), 0)

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
  const pickerButton = page.locator('[data-chat-form]:visible [data-model-trigger]')
  const warningBounds = await warning.boundingBox()
  const pickerBounds = await pickerButton.boundingBox()
  assert.ok(warningBounds.x >= pickerBounds.x + pickerBounds.width)
  assert.equal(await warning.evaluate(el => getComputedStyle(el).color), 'rgb(247, 200, 106)')
  assert.equal(await warning.getAttribute('href'), '?tab=settings')
  await warning.press('Enter')
  await page.waitForFunction(() => document.activeElement === document.querySelector('[name="model_url"]'))

  canConfigure = false
  await page.reload()
  await page.getByText('Could not load other models.', { exact: true }).waitFor()
  assert.equal(await warning.count(), 0)
  const migrationGuide = page.getByRole('dialog', { name: 'Guide' })
  await page.locator('#plugin-header').getByRole('button', { name: 'Guide' }).click()
  await migrationGuide.getByText('FilaMan MCP', { exact: true }).click()
  await migrationGuide.getByText(/switch your client to FilaMan’s built-in/).waitFor()
  await migrationGuide.getByRole('button', { name: 'Close' }).click()

  canConfigure = true
  modelsUnavailable = false
  await page.reload()
  const chatModel = page.getByRole('button', { name: /^Model for this conversation/ })
  const chatMenu = page.getByRole('menu', { name: 'Model for this conversation' })
  await page.locator('[data-chat-form]:visible [data-model-trigger]:not(:disabled)').waitFor()
  await chatModel.press('Enter')
  await page.locator('[data-chat-form]:visible [data-model-trigger][aria-expanded="true"]').waitFor()
  const defaultModel = chatMenu.getByRole('menuitemradio', { name: 'fixture:default Default' })
  await defaultModel.waitFor()
  assert.equal(await defaultModel.getAttribute('aria-checked'), 'true')
  await defaultModel.press('ArrowDown')
  const alternateModel = chatMenu.getByRole('menuitemradio', { name: 'fixture:alternate' })
  assert.equal(await alternateModel.evaluate(el => el === document.activeElement), true)
  await alternateModel.press('Enter')
  await chatMenu.waitFor({ state: 'hidden' })
  assert.equal((await chatModel.innerText()).trim(), 'fixture:alternate')
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
  assert.equal((await chatCard.boundingBox()).width, fullWidth, 'the guide overlays instead of squeezing chat')
  const desktopComposer = await page.locator('[data-chat-form]:visible').boundingBox()
  const desktopGuide = await guide.boundingBox()
  assert.ok(desktopComposer.y + desktopComposer.height <= desktopGuide.y + desktopGuide.height)
  const checkGuideTypography = () => guide.evaluate(panel => {
    const title = getComputedStyle(panel.querySelector('h2'))
    const section = getComputedStyle(document.querySelector('.chat-header h2'))
    const body = getComputedStyle(panel.querySelector('.docs-content'))
    const hint = getComputedStyle(document.querySelector('.fm-field-hint'))
    return { title: [title.fontFamily, title.fontSize, title.fontWeight],
      section: [section.fontFamily, section.fontSize, section.fontWeight],
      body: body.fontSize, hint: hint.fontSize, overflow: panel.scrollWidth > panel.clientWidth }
  }).then(({ title, section, body, hint, overflow }) => {
    assert.deepEqual(title, section, 'Guide title uses the shared section heading style')
    assert.equal(body, hint, 'Guide body matches the FilaBrain secondary text size')
    assert.equal(overflow, false)
  })
  await checkGuideTypography()
  const omniRoute = guide.getByRole('link', { name: 'OmniRoute setup' })
  await omniRoute.waitFor()
  assert.equal(await omniRoute.getAttribute('href'), 'https://github.com/cheaper-inference/OmniRoute')
  await guide.getByText('groq/qwen/qwen3.8-27b', { exact: true }).waitFor()
  const providerLink = guide.getByRole('link', { name: 'Ollama setup' })
  await providerLink.waitFor()
  const artBounds = await guide.getByRole('img').boundingBox()
  assert.ok(artBounds.y + artBounds.height < (await providerLink.boundingBox()).y)
  await guide.getByRole('button', { name: 'Close' }).press('Escape')
  await guide.waitFor({ state: 'hidden' })
  assert.equal(await headerGuide.evaluate(el => el === document.activeElement), true)
  assert.equal(await draft.inputValue(), 'Unsent follow-up')
  await page.getByText('PLA is easier to print.').waitFor()

  const headerNav = page.locator('#plugin-header nav')
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 780 })
    const tabs = await headerNav.getByRole('tab').evaluateAll(buttons => buttons.map(button => {
      const { top, right, height } = button.getBoundingClientRect()
      return { top, right, height, name: button.innerText.trim(), icon: !!button.querySelector('svg') }
    }))
    assert.deepEqual(tabs.map(tab => tab.name), ['Chat', 'New', 'Library'])
    assert.ok(tabs.every(tab => Math.abs(tab.top - tabs[0].top) <= 8 && tab.right < width && tab.height >= 44 && tab.icon), JSON.stringify({width,tabs}))
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width)
  }
  const pluginHeader = page.locator('#plugin-header')
  const headerSettings = pluginHeader.getByRole('button', { name: 'FilaBrain settings' })
  const activeTab = headerNav.getByRole('tab', { selected: true })
  for (const width of [390, 1120, 1121]) {
    await page.setViewportSize({ width, height: 780 })
    const order = [headerGuide, headerSettings, activeTab]
    await order[0].focus()
    for (const button of order.slice(1)) {
      await page.keyboard.press('Tab')
      assert.ok(await button.evaluate(el => el === document.activeElement), 'utility controls precede the active tab: ' + JSON.stringify({width,expected:await button.getAttribute('aria-label'),active:await page.evaluate(()=>document.activeElement?.outerHTML)}))
    }
  }
  await headerSettings.focus()
  await page.setViewportSize({ width: 320, height: 780 })
  assert.ok(await headerSettings.evaluate(el => el === document.activeElement), 'header focus survives resizing')
  await page.evaluate(() => { document.documentElement.style.fontSize = '200%' })
  assert.ok(await pluginHeader.evaluate(header => {
    const title = header.querySelector('h1 span').getBoundingClientRect()
    const utilities = header.querySelector('.header-utilities').getBoundingClientRect()
    return title.right <= utilities.left || title.bottom <= utilities.top
  }), 'the title and utility buttons have separate space with enlarged text')
  assert.equal(await pluginHeader.evaluate(header => header.scrollWidth - header.clientWidth), 0, JSON.stringify(await pluginHeader.locator('*').evaluateAll(nodes => nodes.filter(node=>node.getBoundingClientRect().right>320).map(node=>[node.tagName,node.className,node.getBoundingClientRect().width,getComputedStyle(node).minWidth]))))
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 320)
  await page.evaluate(() => { document.documentElement.style.removeProperty('font-size') })
  await page.setViewportSize({ width: 390, height: 780 })
  const composerBounds = await page.locator('[data-chat-form]:visible').boundingBox()
  assert.ok(composerBounds.y + composerBounds.height <= 780, 'the mobile composer fits in the viewport')
  assert.ok((await chatModel.boundingBox()).height >= 44)
  await headerGuide.click()
  await guide.waitFor()
  assert.equal(await guide.evaluate(el => el.matches(':modal')), true)
  assert.equal((await guide.boundingBox()).width, 390)
  assert.equal((await guide.boundingBox()).height, 780)
  await checkGuideTypography()
  await page.setViewportSize({ width: 320, height: 780 })
  await checkGuideTypography()
  await page.setViewportSize({ width: 1280, height: 720 })
  await page.waitForFunction(() => document.querySelector('#filabrain-docs').matches(':modal'))
  await guide.getByRole('button', { name: 'Close' }).click()
  await guide.waitFor({ state: 'hidden' })
  assert.equal(await headerGuide.evaluate(el => el === document.activeElement), true)
  assert.equal(await draft.inputValue(), 'Unsent follow-up')

  await page.getByRole('button', { name: 'New chat' }).click()
  assert.match(await chatModel.innerText(), /fixture:default/)

  await page.getByRole('tab', { name: 'New filament', exact: true }).click()
  await openResearchDetails(page)
  await page.getByLabel('Manufacturer', { exact: true }).fill('Fixture Brand')
  await page.getByLabel('Product name', { exact: true }).fill('Fixture PLA')
  await page.locator('[data-view="new"]:not([hidden]) #product-research').waitFor()
  const firstSearchPicker = page.locator('[data-chat-form]:visible [data-model-trigger]')
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
  await second.getByRole('tab', { name: 'New filament', exact: true }).click()
  await second.locator('[data-chat-history]:visible summary').click()
  await second.getByRole('button', { name: /Find specifications · Fixture Brand Fixture PLA/ }).click()
  await second.getByRole('button', { name: 'Save new filament' }).click()
  const secondConfirm = second.getByRole('dialog').getByRole('button', { name: 'Create filament' })
  await secondConfirm.waitFor()
  await page.getByRole('button', { name: 'Save new filament' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Create filament' }).click()
  await page.getByRole('link', { name: 'Open saved filament' }).waitFor()
  assert.deepEqual(writes, [{ manufacturer_id: 7, designation: 'Fixture PLA', material_type: 'PLA',
    diameter_mm: 1.75, density_g_cm3: 1.24 }])

  await page.locator('#research-options > summary').click()
  await page.getByLabel('Product name', { exact: true }).fill('Unsaved Fixture PETG')
  await page.getByRole('button', { name: 'Find specifications' }).click()
  await page.getByRole('checkbox', { name: /Density/ }).check()
  await secondConfirm.click()
  await second.getByRole('link', { name: 'Open saved filament' }).waitFor()
  assert.equal(writes.length, 1, 'a stale confirmation does not submit a second create')
  await second.getByRole('textbox', { name: 'Follow up on this search' }).fill('Continue in stale tab')
  await second.getByRole('button', { name: 'Send follow-up' }).click()
  await second.getByText('Source unavailable', { exact: true }).waitFor()
  await second.close()

  await page.reload()
  await page.getByRole('tab', { name: 'Chat', exact: true }).click()
  const history = page.locator('[data-chat-history]:visible summary')
  await history.click()
  await page.getByRole('button', { name: 'Compare these materials', exact: true }).click()
  await page.getByText('PLA is easier to print.').waitFor()
  assert.match(await page.getByRole('button', { name: /^Model for this conversation/ }).innerText(), /fixture:alternate/)
  await page.getByRole('textbox', { name: 'Your question' }).fill('Which settings should I try?')
  await page.getByRole('button', { name: 'Send message' }).click()
  await page.getByText('PLA is easier to print.').nth(1).waitFor()
  await page.getByRole('tab', { name: 'New filament', exact: true }).click()
  await history.click()
  await page.getByRole('button', { name: /Find specifications · Fixture Brand Fixture PLA/ }).click()
  await page.getByRole('log').getByText('Look for a datasheet', { exact: true }).waitFor()
  await earlierResult.first().click()
  await earlierSource.waitFor()
  await page.getByRole('link', { name: 'Fixture catalog' }).first().waitFor()
  await page.getByRole('log').getByText(/Unsaved Fixture PETG/).waitFor()
  assert.equal(await page.getByRole('checkbox', { name: /Density/ }).isChecked(), true)
  assert.equal(await page.getByRole('button', { name: 'Save new filament' }).isEnabled(), true)
  const savedLink = page.getByRole('link', { name: 'Open saved filament' })
  await savedLink.waitFor()
  assert.match(await savedLink.getAttribute('href'), /^\/api\/v1\/filabrain\/saved-filaments\/[a-z0-9-]+$/)
  account = 8
  await page.reload()
  await history.click()
  await page.getByText('No saved conversations yet.').waitFor()
  account = 7

  await page.getByRole('tab', { name: 'Research library', exact: true }).click()
  await page.locator('#library-hint').filter({ hasText: /up to 50 filaments.*Runs tab opens/ }).waitFor()
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
  const runsTab = headerNav.getByRole('tab', { name: 'Research runs', exact: true })
  assert.equal(await runsTab.getAttribute('aria-current'), 'page')
  assert.equal(await page.getByRole('link', { name: /Back to Filaments/i }).count(), 0)
  await page.locator('.batch-toolbar').waitFor()
  assert.equal(await page.getByRole('heading', { name: 'Research runs', exact: true }).count(), 0)
  const batchReview = page.locator('[data-batch-review]')
  assert.equal(await batchReview.locator('.batch-review-fields').getByText('Density (g/cm³)', { exact: true }).count(), 1)
  await batchReview.getByText('Sources (1)', { exact: true }).click()
  assert.equal(await batchReview.getByRole('link', { name: batchSource.title }).count(), 1)
  assert.equal(await batchReview.locator('.batch-disclosure blockquote').count(), 0)
  assert.equal(await batchReview.getByText('Reseller lists 1.30 g/cm³', { exact: true }).count(), 1)
  assert.equal(await batchReview.getByText('Manufacturer value', { exact: true }).count(), 1)
  assert.equal(await batchReview.getByText(/Possible match.*Other PLA/).isVisible(), false)
  await batchReview.getByText('Search details (1)', { exact: true }).click()
  assert.equal(await batchReview.getByText(/Possible match.*Other PLA/).count(), 1)
  assert.equal(await batchReview.getByText(/Possible match.*Other PLA/).isVisible(), true)
  await page.getByRole('checkbox', { name: /Density/ }).check()
  await page.getByRole('button', { name: /Approve/ }).click()
  await page.getByRole('button', { name: 'Apply approved changes' }).click()
  await page.getByRole('dialog').getByRole('button', { name: /Apply/ }).click()
  await page.getByRole('heading', { name: 'Apply report' }).waitFor()
  const appliedDetails = page.locator('[data-batch-report] details[data-result="applied"]')
  await appliedDetails.locator('summary').click()
  assert.equal(await appliedDetails.getByText('Density (g/cm³)', { exact: true }).count(), 1)
  await page.getByRole('link', { name: 'All runs' }).click()
  const batchOverview = page.getByRole('region', { name: 'Research runs' })
  await batchOverview.getByRole('link', { name: 'Fill missing specs' }).waitFor()
  assert.ok(await batchOverview.getByText(/Select filaments in Library and click Start research/).isVisible())
  assert.ok(await batchOverview.getByText(/Apply report shows which changes were saved/).isVisible())
  await page.goto(url)
  await runsTab.waitFor()

  await page.getByRole('button', { name: 'FilaBrain settings' }).click()
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
  await guide.getByText('FilaMan MCP', { exact: true }).click()
  assert.equal(await guide.getByRole('link', { name: 'FilaMan’s app settings' }).getAttribute('href'), '/admin/app-settings#mcp-title')
  await guide.getByText(/switch your client to FilaMan’s built-in/).waitFor()
  await page.getByRole('tab', { name: 'Research library', exact: true }).click()
  await guide.getByRole('heading', { name: 'Research library' }).waitFor()
  await guide.getByRole('button', { name: 'Close' }).click()
  await page.getByRole('button', { name: 'FilaBrain settings' }).click()
  await headerGuide.click()
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
    const [guideButtonBounds, settingsButtonBounds] = await pluginHeader.locator('.header-utilities button').evaluateAll(
      buttons => buttons.map(button => button.getBoundingClientRect().toJSON()),
    )
    assert.equal(guideButtonBounds.height, settingsButtonBounds.height)
    assert.ok(Math.abs(guideButtonBounds.y - settingsButtonBounds.y) <= 1)
    for (const button of [headerGuide, page.locator('#plugin-header [data-tab="settings"]')]) {
      const offset = await button.evaluate(el => {
        const bounds = el.getBoundingClientRect()
        const icon = el.querySelector('svg').getBoundingClientRect()
        return Math.abs((icon.top + icon.bottom - bounds.top - bounds.bottom) / 2)
      })
      assert.ok(offset <= 1, 'header utility icon is vertically centered')
    }
    const labels = await headerNav.getByRole('tab').evaluateAll(buttons => buttons.map(button => button.innerText.trim()))
    assert.deepEqual(labels, width <= 800
      ? ['Chat', 'New', 'Library', 'Runs']
      : ['Chat', 'New filament', 'Research library', 'Research runs'])
  }
  await page.setViewportSize({ width: 1440, height: 780 })
  const guideClose = guide.getByRole('button', { name: 'Close' })
  assert.equal(await guideClose.locator('svg[aria-hidden="true"]').count(), 1)
  await guideClose.click()
  await guide.waitFor({ state: 'hidden' })
  const microphoneSetting = page.getByRole('checkbox', { name: 'Enable microphone dictation' })
  assert.equal(await microphoneSetting.isChecked(), true)
  const savePreferences = page.locator('#filabrain-settings').getByRole('button', { name: 'Save', exact: true })
  assert.equal(await savePreferences.isDisabled(), true)
  await microphoneSetting.uncheck()
  assert.equal(await savePreferences.isEnabled(), true)
  await savePreferences.click()
  await page.getByText('Settings saved.').waitFor()
  assert.equal(microphoneEnabled, false)
  assert.equal(await savePreferences.isDisabled(), true)
  await microphoneSetting.check()
  await savePreferences.click()
  await page.getByText('Settings saved.').waitFor()
  assert.equal(microphoneEnabled, true)
  const enableSearch = page.getByRole('button', { name: 'Set up search' })
  const searchField = page.getByRole('textbox', { name: 'SearXNG URL (optional)' })
  const sources = page.getByRole('region', { name: 'Research sources' })
  const autoSources = sources.getByRole('checkbox', { name: 'Check live sources automatically' })
  assert.equal(await autoSources.isDisabled(), true)
  await sources.getByRole('button', { name: 'Setup guide' }).click()
  await guide.locator('#guide-search-setup').waitFor()
  await guide.getByRole('link', { name: 'SearXNG with Docker' }).waitFor()
  await guide.getByRole('button', { name: 'Close' }).click()
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
  if (!await modelId.isVisible()) await page.locator('#filabrain-settings').getByText('Enter a model ID manually', { exact: true }).click()
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
  await page.getByRole('button', { name: 'FilaBrain settings' }).click()
  await searchField.waitFor()
  assert.equal(await searchField.inputValue(), searchUrl)
  assert.equal(await downloadLimit.inputValue(), '12')
  assert.equal(await supplier.isChecked(), true)
  const endpointPicker = page.getByRole('combobox', { name: 'Model endpoint' })
  const visibility = page.getByRole('region', { name: 'Model settings' }).getByText('Models shown in chat', { exact: true })
  const desktopSize = page.viewportSize()
  await page.setViewportSize({ width: 320, height: 760 })
  await page.getByRole('button', { name: 'Add endpoint' }).click()
  await page.getByRole('textbox', { name: 'Endpoint name' }).fill('Remote models')
  await modelUrl.fill('https://offline.test/v1')
  await loadModels.click()
  await page.getByText('Could not load models from this server.').waitFor()
  await modelUrl.fill('https://remote.test/v1')
  await loadModels.click()
  await page.getByLabel('Available models').selectOption('fixture:alternate')
  await visibility.click()
  await page.getByRole('checkbox', { name: 'fixture:hidden', exact: true }).uncheck()
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 320)
  await page.setViewportSize(desktopSize)
  await page.locator('#filabrain-settings').getByRole('button', { name: 'Save', exact: true }).click()
  await page.getByText('Settings saved.').waitFor()
  await page.reload()
  await page.getByRole('button', { name: 'FilaBrain settings' }).click()
  await endpointPicker.selectOption({ label: 'Remote models' })
  await visibility.click()
  assert.equal(await page.getByRole('checkbox', { name: 'fixture:hidden', exact: true }).isChecked(), false)
  await page.getByRole('tab', { name: 'Chat', exact: true }).click()
  await chatModel.click()
  const remoteGroup = chatMenu.getByRole('group', { name: 'Remote models' })
  await remoteGroup.waitFor()
  assert.equal(await remoteGroup.getByRole('menuitemradio', { name: 'fixture:hidden' }).count(), 0)
  await remoteGroup.getByRole('menuitemradio', { name: 'fixture:alternate' }).click()
  assert.equal((await chatModel.innerText()).trim(), 'fixture:alternate')
  assert.match(await chatModel.getAttribute('aria-label'), /Remote models/)
  await page.evaluate(() => {
    const key = 'filabrain:chat:7'
    const history = JSON.parse(localStorage.getItem(key) || '[]')
    history.unshift({ id: 'legacy-chat', title: 'Conversation before model endpoints', messages: [
      { role: 'user', content: 'A saved question' }, { role: 'assistant', content: 'A saved answer' },
    ] })
    localStorage.setItem(key, JSON.stringify(history))
  })
  const legacyChat = await context.newPage()
  await legacyChat.goto(url)
  await legacyChat.locator('[data-chat-history]:visible summary').click()
  await legacyChat.getByRole('button', { name: 'Conversation before model endpoints' }).click()
  await legacyChat.getByRole('textbox', { name: 'Your question' }).fill('A follow-up')
  await legacyChat.getByRole('button', { name: 'Send message' }).click()
  await legacyChat.locator('[data-chat-status]').getByText('Choose a model').waitFor()
  await legacyChat.close()
  assert.equal(chatRequests, 2, 'legacy history cannot send to the new default before a model is chosen')
  await page.getByRole('button', { name: 'FilaBrain settings' }).click()
  await supplier.uncheck()
  await page.locator('#filabrain-settings').getByRole('button', { name: 'Save', exact: true }).click()
  await page.getByText('Settings saved.').waitFor()
  assert.deepEqual(supplierSites, [{ domain: 'shop.example.org', enabled: false }])
  await internet.getByRole('button', { name: 'Remove shop.example.org' }).click()
  await page.locator('#filabrain-settings').getByRole('button', { name: 'Save', exact: true }).click()
  await page.getByText('Settings saved.').waitFor()
  assert.deepEqual(supplierSites, [])
  assert.equal(await enableSearch.isVisible(), false)
  holdStage = true
  await context.addInitScript(() => {
    window.SpeechRecognition = class {
      constructor() { window.editorRecognition=this }
      start() {}
      abort() { this.aborted=true; this.onend?.() }
    }
  })
  await page.goto('https://fila.test/editor-fixture')
  const frame = page.frameLocator('iframe')
  await frame.getByRole('searchbox', { name: 'SKU, product link, or description' }).waitFor()
  const followUp = frame.getByRole('textbox', { name: 'Follow up on this search' })
  const drawerGuide = frame.getByRole('dialog', { name: 'Guide' })
  await frame.getByRole('button', { name: 'Guide', exact: true }).click()
  await drawerGuide.getByRole('heading', { name: 'Research this filament', exact: true }).waitFor()
  assert.equal(await drawerGuide.evaluate(el => el.matches(':modal')), true)
  await page.locator('iframe').evaluate(el => { el.style.width = '960px' })
  await frame.locator('#filabrain-docs:modal').waitFor()
  const drawerBounds = await frame.locator('.research-chat:visible').boundingBox()
  await drawerGuide.getByRole('button', { name: 'Close' }).click()
  assert.equal((await frame.locator('.research-chat:visible').boundingBox()).width, drawerBounds.width, 'the editor guide does not resize chat')
  await frame.getByRole('button', { name: 'FilaBrain settings' }).click()
  await frame.getByRole('button', { name: 'Guide', exact: true }).click()
  await frame.locator('#filabrain-docs:modal').waitFor()
  await drawerGuide.getByText('FilaMan MCP', { exact: true }).waitFor()
  await drawerGuide.getByRole('button', { name: 'Close' }).click()
  const settingsGuide = frame.getByRole('button', { name: 'Guide', exact: true })
  await settingsGuide.locator('xpath=self::*[@aria-expanded="false"]').waitFor()
  assert.equal(await settingsGuide.evaluate(el => el === el.ownerDocument.activeElement), true)
  await page.locator('iframe').evaluate(el => { el.style.width = '640px' })
  await frame.locator('[data-tab=editor]').click()
  const researchModel = frame.getByRole('button', { name: /^Model for this conversation/ })
  const researchMenu = frame.getByRole('menu', { name: 'Model for this conversation' })
  await researchModel.click()
  await researchMenu.getByRole('menuitemradio', { name: 'fixture:default Default' }).press('Escape')
  await researchMenu.waitFor({ state: 'hidden' })
  await researchModel.click()
  await researchMenu.getByRole('group', { name: 'Remote models' }).getByRole('menuitemradio', { name: 'fixture:alternate' }).click()
  await researchMenu.waitFor({ state: 'hidden' })
  assert.equal(await frame.getByText('Advanced options', { exact: true }).count(), 0)
  assert.equal(await frame.getByRole('button', { name: 'Pair a client' }).count(), 0)
  assert.equal(await followUp.isDisabled(), true)
  assert.match(await researchModel.innerText(), /fixture:alternate/)
  await frame.getByRole('button', { name: 'Find specifications' }).click()
  await frame.locator('[data-tab=editor][aria-selected=true]').waitFor()
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
  await page.waitForFunction(() => !!window.finishStage)
  assert.equal(await frame.locator('[data-chat-new]:visible').isDisabled(), true)
  const unstagedSku = frame.locator('[data-view=editor]').locator('[data-research-id=research-results]').getByRole('checkbox', { name: /FIX-123/ })
  assert.equal(await unstagedSku.isDisabled(), true)
  await frame.locator('[data-chat-history]:visible summary').click()
  assert.equal(await frame.locator('[data-chat-history-list]:visible').getByRole('button', { name: 'Find specifications' }).isDisabled(), true)
  await page.evaluate(() => { const reply = window.finishStage; delete window.finishStage; reply() })
  await frame.getByText('1 field staged.', { exact: false }).waitFor()
  assert.equal(await frame.locator('[data-chat-new]:visible').isDisabled(), false)
  const stagedFindings = frame.locator('.research-history').filter({ hasText: 'Density (g/cm³): 1.24' }).first()
  await stagedFindings.locator('summary').click()
  assert.equal(await stagedFindings.locator('.research-history-value').filter({ hasText: 'Density (g/cm³): 1.24' })
    .getByRole('link', { name: evidence[0].title }).getAttribute('href'), evidence[0].url)
  assert.equal(await frame.locator('[data-view=editor]').locator('[data-research-id=research-results]').getByRole('checkbox', { name: /Density/ }).count(), 0)
  assert.equal(await unstagedSku.isEnabled(), true)
  assert.deepEqual(await page.evaluate(() => window.staged), [{ density_g_cm3: { old: null, value: 1.24 } }])
  await frame.locator('[data-view=editor]').locator('[data-research-id=research-options] > summary').click()
  await frame.getByRole('button', { name: 'Find specifications' }).click()
  const researchPage = frame.getByRole('button', { name: 'Research this page' })
  await researchPage.waitFor()
  await frame.getByRole('button', { name: 'Send follow-up' }).waitFor()
  await frame.locator('[data-chat-form]:visible [name="message"]:not([readonly])').waitFor()
  await frame.getByRole('button', { name: 'Dictate a message', exact: true }).click()
  await frame.getByRole('button', { name: 'Stop dictation', exact: true }).waitFor()
  await frame.getByRole('button', { name: 'FilaBrain settings', exact: true }).click()
  assert.equal(await frame.locator('body').evaluate(() => window.editorRecognition.aborted), true, 'embedded settings stops capture before hiding the chat')
  await frame.locator('[data-tab=editor]').click()
  assert.equal(await researchPage.locator('svg[aria-hidden=true]').count(), 1)
  recordUnavailable = true
  const requestsBeforeFailedClick = proposalRequests
  await researchPage.click()
  await frame.locator('[data-view=editor]').locator('[data-research-id=research-status]').getByText(/HTTP 503/).waitFor()
  assert.equal(proposalRequests, requestsBeforeFailedClick)
  recordUnavailable = false
  await researchPage.click()
  await frame.getByRole('log').getByText('Research this page', { exact: true }).waitFor()
  await densityChoice.waitFor()
  const currentVariants = frame.locator('[data-view=editor]').locator('[data-research-id=research-results]').getByRole('group', { name: 'Drying temperature (°C)', exact: true })
  await currentVariants.getByText(variantWarning, { exact: true }).waitFor()
  assert.equal(await currentVariants.getByRole('radio').count(), 0)
  assert.equal(await currentVariants.getByRole('link').count(), 2)
  await frame.locator('[data-view=editor]').locator('[data-research-id=research-options] > summary').click()
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
  await page.waitForFunction(() => !!window.finishStage)
  await page.evaluate(() => window.finishStage())
  await frame.getByText('2 fields staged.', { exact: false }).waitFor()
  assert.deepEqual(await page.evaluate(() => window.staged), [{
    designation: { old: 'Fixture PLA', value: 'Fixture-PLA' },
    material_subgroup: { old: null, value: 'Silk' },
  }])
  assert.equal(writes.length, 1)

  const requestsBeforeReopen = proposalRequests
  holdStage = false
  await page.reload()
  await frame.locator('[data-chat-history]:visible summary').click({ timeout: 2000 })
  await frame.locator('[data-chat-history-list]:visible').getByRole('button', { name: 'Find specifications', exact: true }).click()
  await frame.getByRole('log').getByText('Focus on the material finish', { exact: true }).waitFor()
  await frame.getByRole('checkbox', { name: /Fixture-PLA.*Silk/ }).waitFor()
  const variantArchive = frame.locator('.research-history').filter({ hasText: 'Variant A datasheet' }).first()
  await variantArchive.locator('summary').click()
  await variantArchive.getByText(/Several products or variants could match/).waitFor()
  const archivedFailure = variantArchive.getByText(/Fixture catalog.*Timed out/)
  assert.equal(await archivedFailure.getByRole('link', { name: 'Open source' }).getAttribute('href'), evidence[0].url)
  const variantGroup = variantArchive.getByRole('group', { name: 'Drying temperature (°C)', exact: true })
  await variantGroup.getByText(variantWarning, { exact: true }).waitFor()
  for (const option of variantIssue.options) {
    const row = variantGroup.locator('.research-history-value').filter({ hasText: `: ${option.value}` })
    assert.equal(await row.getByRole('link', { name: option.sources[0].title }).getAttribute('href'), option.sources[0].url)
  }
  assert.equal(proposalRequests, requestsBeforeReopen, 'reopening editor research does not run another lookup')
  await frame.getByRole('checkbox', { name: /Fixture-PLA.*Silk/ }).check()
  await page.reload()
  await frame.locator('[data-chat-history]:visible summary').click()
  await frame.locator('[data-chat-history-list]:visible').getByRole('button', { name: 'Find specifications', exact: true }).click()
  await frame.getByRole('button', { name: 'Stage 2 fields' }).waitFor()
  for (const token of ['d'.repeat(32), null, 'unavailable']) {
    recordUnavailable = token === 'unavailable'
    editorRecordToken = token
    await page.reload()
    await frame.locator('[data-chat-history]:visible summary').click()
    await frame.locator('[data-chat-history-list]:visible').getByRole('button', { name: 'Find specifications', exact: true }).click()
    await frame.getByText(/Saved findings are read-only/).waitFor()
    await frame.getByText('Earlier findings', { exact: true }).first().waitFor()
    assert.equal(await frame.getByRole('checkbox', { name: /Fixture-PLA.*Silk/ }).count(), 0)
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
  await frame.locator('[data-chat-history]:visible summary').click()
  await frame.locator('[data-chat-history-list]:visible').getByRole('button', { name: 'Find specifications', exact: true }).click()
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
  await page.getByRole('tab', { name: 'New filament', exact: true }).click()
  await page.locator('[data-chat-history]:visible summary').click()
  await page.getByRole('button', { name: /Find specifications · Fixture Brand Fixture PLA/ }).click()
  await page.getByRole('link', { name: 'Open saved filament' }).waitFor()
  await page.locator('#research-options > summary').click()
  await page.getByLabel('Product name', { exact: true }).fill('Fixture PETG')
  await page.getByLabel('Manufacturer', { exact: true }).fill('Fixture Brand')
  if (!await page.getByLabel('Material', { exact: true }).isVisible()) {
    await page.locator('#research-advanced-fields > summary').click()
  }
  await page.getByLabel('Material', { exact: true }).fill('PETG')
  await page.getByLabel('Diameter (mm)').fill('1.75')
  await page.getByRole('button', { name: 'Find specifications' }).click()
  await page.getByRole('checkbox', { name: /Density/ }).check()
  const otherDraft = await context.newPage()
  otherDraft.setDefaultTimeout(5_000)
  await otherDraft.goto(url)
  await otherDraft.getByRole('tab', { name: 'New filament', exact: true }).click()
  await otherDraft.locator('[data-chat-history]:visible summary').click()
  const researchTitle = /Find specifications · Fixture Brand Fixture PLA/
  await otherDraft.getByRole('button', { name: researchTitle }).click()
  await otherDraft.locator('#research-options > summary').click()
  if (!await otherDraft.getByLabel('FilaMan palette color').isVisible()) {
    await otherDraft.getByText('Search options', { exact: true }).click()
  }
  await otherDraft.getByLabel('FilaMan palette color').selectOption('1')
  const swatchResearch = otherDraft.waitForRequest(req => req.url().endsWith('/proposals'))
  await otherDraft.getByRole('button', { name: 'Find specifications' }).click()
  assert.equal((await swatchResearch).postDataJSON().palette_color_id, 1)
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
  assert.deepEqual(writes.map(write => write.designation), ['Fixture PLA', 'Fixture PETG', 'Fixture PETG'])
  assert.deepEqual(writes[2].colors, [{ color_id: 1 }])
  assert.equal(new Set(savePaths).size, 3)
  await otherDraft.reload()
  for (const tab of [otherDraft, page]) {
    await tab.getByRole('tab', { name: 'New filament', exact: true }).click()
    await tab.locator('[data-chat-history]:visible summary').click()
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
  await page.setViewportSize({ width: 320, height: 650 })
  for (const [language, labels] of [
    ['de', ['Chat', 'Neu', 'Bibliothek', 'Läufe']],
    ['fr', ['Chat', 'Nouveau', 'Bibliothèque', 'Suivi']],
  ]) {
    await page.evaluate(value => localStorage.setItem('lang', value), language)
    await page.goto(url)
    await headerNav.getByRole('tab').last().waitFor()
    const tabs = await headerNav.getByRole('tab').evaluateAll(buttons => buttons.map(button => [
      button.innerText.trim(), button.getBoundingClientRect().top, button.getBoundingClientRect().right,
    ]))
    assert.deepEqual(tabs.map(([label]) => label), labels)
    for (const label of labels) {
      assert.equal(await headerNav.getByRole('tab', { name: label, exact: true }).count(), 1,
        'mobile tab accessible name matches its visible label')
    }
    assert.ok(tabs.every(([, top]) => Math.abs(top - tabs[0][1]) <= 8), language + ' tabs share one row')
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 320, language + ' tab overflow stays inside the strip')
    await headerNav.getByRole('tab').last().focus()
    await page.keyboard.press('Home')
    assert.equal(await headerNav.getByRole('tab', { selected: true }).innerText(), 'Chat')
  }
  chatModelId = ''
  editorMode = 'edit'
  await page.setViewportSize({ width: 1280, height: 780 })
  await page.evaluate(() => {
    localStorage.setItem('lang', 'en')
    localStorage.removeItem('filabrain:chat:7:editor:10')
  })
  await page.goto('https://fila.test/editor-fixture')
  await frame.getByRole('button', { name: 'Find specifications' }).click()
  await frame.getByRole('checkbox', { name: /Density/ }).waitFor()
  const lookupsBeforeModelFreeReopen = proposalRequests
  await page.reload()
  await frame.locator('[data-chat-history]:visible summary').click()
  await frame.locator('[data-chat-history-list]:visible').getByRole('button', { name: 'Find specifications', exact: true }).click()
  await frame.getByRole('checkbox', { name: /Density/ }).waitFor()
  assert.equal(proposalRequests, lookupsBeforeModelFreeReopen)
  const guidance = 'Check the supplier specifications again'
  const followUpResponse = page.waitForResponse(response => response.request().method() === 'POST' &&
    response.url().endsWith('/proposals') && response.request().postDataJSON().guidance === guidance)
  await followUp.fill(guidance)
  await frame.getByRole('button', { name: 'Send follow-up' }).click()
  const modelFreeResult = await followUpResponse
  assert.ok(modelFreeResult.ok())
  await modelFreeResult.finished()
  const followUpPayload = modelFreeResult.request().postDataJSON()
  assert.equal(followUpPayload.model_id, undefined)
  assert.equal(followUpPayload.endpoint_id, undefined)

  const activeResearch = frame.locator('[data-view=editor]').locator('[data-research-id=research-results]')
  const reviewedSku = activeResearch.getByRole('checkbox', { name: /FIX-123/ })
  await reviewedSku.check()
  const reviewedDrying = activeResearch.getByRole('checkbox', { name: /Drying temperature.*50/ })
  await reviewedDrying.check()
  await page.evaluate(() => { window.editorEdits = { drying_temp_c: 60 } })
  const followUpField = frame.getByRole('combobox', { name: 'Follow-up field', exact: true })
  assert.equal(await followUpField.count(), 1, 'research can follow up on one field')
  await followUpField.selectOption('density_g_cm3')
  const checkDensity = 'Check the published density https://manufacturer.test/specs.pdf'
  const checkedReference = page.waitForRequest(request => request.method() === 'POST' &&
    request.url().endsWith('/proposals') && request.postDataJSON().guidance.startsWith('Check the published density'))
  await followUp.fill(checkDensity)
  await followUp.press('Enter')
  const referencePayload = (await checkedReference).postDataJSON()
  assert.equal(referencePayload.reference_url, 'https://manufacturer.test/specs.pdf')
  assert.equal(referencePayload.guidance, 'Check the published density')
  await activeResearch.getByRole('checkbox', { name: /Density.*1\.27/ }).waitFor()
  assert.equal(await reviewedSku.isChecked(), true, 'an unrelated reviewed value stays selected')
  assert.equal(await activeResearch.getByRole('checkbox', { name: /Bed temperature.*42/ }).count(), 0)
  assert.equal(await reviewedDrying.count(), 0, 'a value edited in the host form is no longer eligible')
  assert.equal(await frame.getByRole('button', { name: 'Stage 1 field' }).isEnabled(), true)
  const stagedBeforeCorrection = await page.evaluate(() => window.staged)
  const correctedProduct = page.waitForRequest(request => request.method() === 'POST' &&
    request.url().endsWith('/proposals'))
  await followUp.fill(`Use the refill at ${refillUrl}`)
  await followUp.press('Enter')
  const correctedPayload = (await correctedProduct).postDataJSON()
  assert.equal(correctedPayload.product_url, refillUrl)
  assert.equal(correctedPayload.reference_url, undefined)
  await activeResearch.getByRole('checkbox', { name: /Density.*1\.22/ }).waitFor()
  assert.equal(await reviewedSku.count(), 0, 'the old variant is no longer proposed')
  assert.equal(await activeResearch.getByRole('checkbox', { name: /FIX-REFILL/ }).isChecked(), false)
  assert.deepEqual(await page.evaluate(() => window.staged), stagedBeforeCorrection, 'follow-ups never stage changes')

  const urlDraft = await context.newPage()
  urlDraft.setDefaultTimeout(5_000)
  await urlDraft.goto(`${url}?tab=new`)
  await openResearchDetails(urlDraft)
  await urlDraft.getByRole('textbox', { name: 'Product URL (optional)' }).fill(rotatingVariantUrl)
  await urlDraft.getByRole('button', { name: 'Find specifications' }).click()
  const urlResults = urlDraft.locator('#research-results')
  const oldPackage = urlResults.getByRole('checkbox', { name: /OLD-PACKAGE/ })
  await oldPackage.check()
  await urlDraft.getByRole('combobox', { name: 'Follow-up field' }).selectOption('density_g_cm3')
  const urlFollowUp = urlDraft.getByRole('textbox', { name: 'Follow up on this search' })
  await urlFollowUp.fill('Check the density again')
  const scopedReference = urlDraft.waitForResponse(response =>
    response.url().endsWith('/proposals') && response.request().postDataJSON().guidance === 'Check the density again')
  await urlFollowUp.press('Enter')
  assert.equal((await scopedReference).request().postDataJSON().identity.sku, 'OLD-PACKAGE')
  await urlDraft.locator('#save-filament:enabled').waitFor()
  assert.equal(await oldPackage.isChecked(), true, 'a follow-up keeps the accepted SKU even when the source does not repeat it')
  const changedProduct = urlDraft.waitForResponse(response =>
    response.url().endsWith('/proposals') && response.request().postDataJSON().product_url === refillUrl)
  await urlFollowUp.fill(`Use the refill at ${refillUrl}`)
  await urlFollowUp.press('Enter')
  assert.equal((await changedProduct).request().postDataJSON().identity.sku, '')
  await urlResults.getByRole('checkbox', { name: /Density.*1\.22/ }).waitFor()
  assert.equal(await oldPackage.count(), 0)
  assert.equal(await urlResults.getByRole('checkbox', { name: /FIX-REFILL/ }).isChecked(), false)
  await urlDraft.close()

  await page.reload()
  await page.evaluate(() => { window.editorEdits = { spool_width_mm: 62, raw_material_weight_g: 1000 } })
  await frame.getByRole('button', { name: 'Find spool specs' }).click()
  await frame.getByText('Which spool matches yours?', { exact: true }).waitFor()
  await frame.getByRole('img', { name: 'Photo of 170g Spool' }).waitFor()
  assert.equal(await frame.getByRole('button', { name: 'This matches: Fixture Brand PLA 3 kg' }).count(), 0)
  const styleReply = 'It is clear plastic with no slots.'
  const styleRequest = page.waitForRequest(req => req.url().endsWith('/proposals') && req.postDataJSON().guidance === styleReply)
  await followUp.fill(styleReply)
  await followUp.press('Enter')
  assert.equal((await styleRequest).postDataJSON().spool_lookup, true)
  await frame.getByRole('button', { name: 'This matches: Fixture Brand PLA 1 kg' }).click()
  await frame.getByRole('combobox', { name: 'Follow-up field', exact: true }).selectOption('density_g_cm3')
  await followUp.fill('Recheck this density')
  await followUp.press('Enter')
  await activeResearch.getByRole('checkbox', { name: /Density.*1\.27/ }).waitFor()
  assert.equal(await activeResearch.getByRole('button', { name: 'Selected: Fixture Brand PLA 1 kg' }).count(), 1)
  assert.equal(await page.evaluate(() => window.staged), undefined)
  await frame.getByRole('button', { name: 'Stage 3 fields' }).click()
  await page.waitForFunction(() => window.staged)
  assert.deepEqual(await page.evaluate(() => window.staged), [
    { default_spool_weight_g: { old: null, value: 233 } },
    { spool_outer_diameter_mm: { old: null, value: 200 } },
    { spool_width_mm: { old: 62, value: 66 } },
  ], 'explicit spool choice stages reviewed replacements as well as missing dimensions')

  const spoolDraft = await context.newPage()
  spoolDraft.setDefaultTimeout(5_000)
  await spoolDraft.goto(`${url}?tab=new`)
  await openResearchDetails(spoolDraft)
  await spoolDraft.getByLabel('Manufacturer', { exact: true }).fill('Fixture Brand')
  await spoolDraft.getByLabel('Product name', { exact: true }).fill('Fixture PLA')
  await spoolDraft.locator('#research-advanced-fields summary').click()
  await spoolDraft.getByLabel('Material', { exact: true }).fill('PLA')
  await spoolDraft.getByLabel('Diameter (mm)').fill('1.75')
  await spoolDraft.getByRole('button', { name: 'Find spool specs' }).click()
  const chooseSpool = spoolDraft.getByRole('button', { name: 'This matches: Fixture Brand PLA 1 kg' })
  const selectedSpool = spoolDraft.getByRole('button', { name: 'Selected: Fixture Brand PLA 1 kg' })
  const firstSpoolSearch = spoolDraft.locator('.chat-history-list').getByRole('button', { name: 'Find spool specs · Fixture Brand Fixture PLA', exact: true }).first()
  const reopenSpoolDraft = async () => {
    await spoolDraft.reload()
    await spoolDraft.locator('[data-chat-history]:visible summary').click()
    await firstSpoolSearch.click()
  }
  await chooseSpool.waitFor()
  await reopenSpoolDraft()
  await chooseSpool.click()
  await spoolDraft.locator('#research-options > summary').click()
  await spoolDraft.getByRole('button', { name: 'Find specifications' }).click()
  await spoolDraft.getByRole('checkbox', { name: /Density.*1\.24/ }).waitFor()
  assert.equal(await selectedSpool.count(), 1, 'filament specs for the same product retain the spool choice')
  const writesBeforeSpool = writes.length
  await spoolDraft.getByRole('button', { name: 'Save new filament' }).click()
  await spoolDraft.locator('#create-preview').getByText('233', { exact: true }).waitFor()
  await spoolDraft.locator('#create-preview').getByText('66', { exact: true }).waitFor()
  assert.equal(writes.length, writesBeforeSpool, 'opening the create review does not save the filament')
  await spoolDraft.getByRole('button', { name: 'Cancel', exact: true }).click()
  conflictingSpoolWidth = true
  await spoolDraft.locator('#research-options > summary').click()
  await spoolDraft.getByRole('button', { name: 'Find specifications' }).click()
  const supplierWidth = spoolDraft.getByRole('checkbox', { name: /Spool Width.*72/ })
  const spoolWidth = spoolDraft.getByRole('checkbox', { name: /Spool Width.*66/ })
  await supplierWidth.waitFor()
  assert.equal(await selectedSpool.count(), 1)
  assert.equal(await spoolWidth.isChecked(), false, 'new conflicting measurements require another explicit choice')
  await supplierWidth.check()
  await spoolWidth.check()
  assert.equal(await supplierWidth.isChecked(), false, 'choosing the spool width clears the competing supplier value')
  await supplierWidth.check()
  assert.equal(await spoolWidth.isChecked(), false, 'choosing the supplier width clears the competing spool value')
  await spoolDraft.getByRole('button', { name: 'Save new filament' }).click()
  await spoolDraft.locator('#create-preview').getByText('233', { exact: true }).waitFor()
  await spoolDraft.locator('#create-preview').getByText('72', { exact: true }).waitFor()
  assert.equal(await spoolDraft.locator('#create-preview').getByText('66', { exact: true }).count(), 0)
  await spoolDraft.getByRole('button', { name: 'Cancel', exact: true }).click()
  conflictingSpoolWidth = false
  differentSpoolPackage = true
  await spoolDraft.locator('#research-options > summary').click()
  await spoolDraft.getByRole('button', { name: 'Find specifications' }).click()
  const packageWeight = spoolDraft.getByRole('checkbox', { name: /Filament weight.*500/i })
  await packageWeight.waitFor()
  assert.equal(await selectedSpool.count(), 0, 'specs for a different package weight require another spool choice')
  await spoolDraft.locator('#research-options > summary').click()
  await spoolDraft.getByRole('button', { name: 'Find spool specs' }).click()
  const chooseSharedReel = spoolDraft.getByRole('button', { name: 'This matches: Shared Reel' })
  await chooseSharedReel.waitFor()
  await spoolDraft.getByRole('button', { name: 'This matches: 170g Spool' }).click()
  await packageWeight.check()
  assert.equal(await spoolDraft.getByRole('button', { name: 'Selected: 170g Spool' }).count(), 0,
    'a FilaManDB reel linked to a 1 kg listing is cleared when the draft selects 500 g')
  assert.equal(await spoolDraft.getByRole('checkbox', { name: /Spool Weight.*170/i }).count(), 0)
  assert.equal(await chooseSharedReel.count(), 0, 'an unlisted package cannot use a shared reel')
  const chooseUnlistedReel = spoolDraft.getByRole('button', { name: 'This matches: Unlisted Package Weight Reel' })
  await packageWeight.uncheck()
  await chooseUnlistedReel.click()
  await packageWeight.check()
  assert.equal(await spoolDraft.getByRole('button', { name: /Unlisted Package Weight Reel/ }).count(), 0,
    'a legacy FilaManDB card needs a fresh lookup once the package weight is known')
  assert.equal(await spoolDraft.getByRole('checkbox', { name: /Spool Weight.*181/i }).count(), 0)
  await spoolDraft.locator('#research-options > summary').click()
  const weightedLookup = spoolDraft.waitForRequest(req => req.url().endsWith('/proposals') && req.postDataJSON().spool_lookup)
  await spoolDraft.getByRole('button', { name: 'Find spool specs' }).click()
  assert.equal((await weightedLookup).postDataJSON().spool_package_weight_g, 500)
  await chooseUnlistedReel.waitFor()
  assert.equal(await chooseSpool.count(), 0)
  await spoolDraft.getByRole('button', { name: 'Save new filament' }).click()
  await spoolDraft.locator('#create-preview').getByText('500', { exact: true }).waitFor()
  assert.equal(await spoolDraft.locator('#create-preview').getByText('170', { exact: true }).count(), 0)
  await spoolDraft.getByRole('button', { name: 'Cancel', exact: true }).click()
  await reopenSpoolDraft()
  await packageWeight.waitFor()
  assert.equal(await packageWeight.isChecked(), true)
  assert.equal(await chooseSpool.count(), 0, 'resuming the draft preserves its package filter')
  await packageWeight.uncheck()
  differentSpoolPackage = false
  await spoolDraft.locator('#research-options > summary').click()
  await spoolDraft.getByRole('button', { name: 'Find spool specs' }).click()
  await chooseSpool.click()
  duplicateSpoolLookup = true
  await spoolDraft.locator('#research-options > summary').click()
  await spoolDraft.getByRole('button', { name: 'Find spool specs' }).click()
  await spoolDraft.getByText('A matching filament already exists.', { exact: false }).waitFor()
  assert.equal(await spoolDraft.getByRole('button', { name: 'Save new filament' }).isDisabled(), true)
  await reopenSpoolDraft()
  await spoolDraft.getByText('Earlier findings', { exact: true }).first().click()
  await spoolDraft.locator('.research-history-value').filter({ hasText: '233' }).first().waitFor()
  duplicateSpoolLookup = false
  await spoolDraft.locator('#research-options > summary').click()
  await spoolDraft.getByRole('button', { name: 'Find spool specs' }).click()
  await spoolDraft.locator('#save-filament:enabled').waitFor()
  await selectedSpool.waitFor()
  await reopenSpoolDraft()
  await selectedSpool.click()
  await spoolDraft.getByRole('button', { name: 'This matches: 170g Spool' }).click()
  await spoolDraft.getByRole('button', { name: 'Save new filament' }).click()
  await spoolDraft.getByRole('dialog').getByRole('button', { name: 'Create filament' }).click()
  await spoolDraft.getByRole('link', { name: 'Open saved filament' }).waitFor()
  assert.equal(writes.at(-1).default_spool_weight_g, 170)
  await reopenSpoolDraft()
  await spoolDraft.getByRole('link', { name: 'Open saved filament' }).waitFor()
  await spoolDraft.getByText('Earlier findings', { exact: true }).last().click()
  await spoolDraft.locator('.research-history-value').filter({ hasText: '170' }).waitFor()
  await spoolDraft.close()

  const skuDraft = await context.newPage()
  skuDraft.setDefaultTimeout(5_000)
  await skuDraft.goto(`${url}?tab=new`)
  await openResearchDetails(skuDraft)
  await skuDraft.getByLabel('Manufacturer', { exact: true }).fill('Fixture Brand')
  await skuDraft.getByLabel('Product name', { exact: true }).fill('Fixture SKU PLA')
  await skuDraft.locator('#research-advanced-fields summary').click()
  await skuDraft.getByLabel('Material', { exact: true }).fill('PLA')
  await skuDraft.getByLabel('Diameter (mm)').fill('1.75')
  await skuDraft.getByRole('button', { name: 'Find specifications' }).click()
  const acceptedSku = skuDraft.getByRole('checkbox', { name: /FIX-123/ })
  await acceptedSku.waitFor()
  await skuDraft.locator('#research-options > summary').click()
  await skuDraft.getByRole('button', { name: 'Find spool specs' }).click()
  await skuDraft.getByRole('button', { name: 'This matches: 170g Spool' }).click()
  await acceptedSku.check()
  assert.equal(await skuDraft.getByRole('button', { name: /170g Spool/ }).count(), 0)
  assert.equal(await skuDraft.getByRole('checkbox', { name: /Spool Weight.*170/i }).count(), 0)
  await skuDraft.locator('#research-options > summary').click()
  const scopedLookup = skuDraft.waitForRequest(req => req.url().endsWith('/proposals') && req.postDataJSON().spool_lookup)
  await skuDraft.getByRole('button', { name: 'Find spool specs' }).click()
  assert.equal((await scopedLookup).postDataJSON().identity.sku, 'FIX-123')
  const correctReel = skuDraft.getByRole('button', { name: 'This matches: Fixture Brand PLA 1 kg' })
  await correctReel.click()
  assert.equal(await skuDraft.getByRole('button', { name: /170g Spool/ }).count(), 0)
  await skuDraft.getByRole('button', { name: 'Save new filament' }).click()
  await skuDraft.locator('#create-preview').getByText('FIX-123', { exact: true }).waitFor()
  await skuDraft.locator('#create-preview').getByText('233', { exact: true }).waitFor()
  await skuDraft.getByRole('button', { name: 'Cancel', exact: true }).click()
  await skuDraft.reload()
  await skuDraft.locator('[data-chat-history]:visible summary').click()
  await skuDraft.getByRole('button', { name: 'Find specifications · Fixture Brand Fixture SKU PLA', exact: true }).click()
  const scopedReel = skuDraft.getByRole('button', { name: 'Selected: Fixture Brand PLA 1 kg' })
  await scopedReel.waitFor()
  assert.equal(await acceptedSku.isChecked(), true)
  await skuDraft.locator('#research-options > summary').click()
  supplierDefaultSku = 'FIX-REFILL'
  const refreshedSpecs = skuDraft.waitForResponse(response =>
    response.url().endsWith('/proposals') && !response.request().postDataJSON().spool_lookup)
  await skuDraft.getByRole('button', { name: 'Find specifications' }).click()
  assert.equal((await refreshedSpecs).request().postDataJSON().identity.sku, 'FIX-123')
  await skuDraft.locator('#save-filament:enabled').waitFor()
  assert.equal(await acceptedSku.isChecked(), true, 'refreshing unchanged specifications preserves the accepted SKU')
  assert.equal(await scopedReel.count(), 1, 'refreshing unchanged specifications preserves its scoped reel')
  assert.equal(await skuDraft.getByRole('checkbox', { name: /FIX-REFILL/ }).count(), 0)
  await skuDraft.getByRole('button', { name: 'Save new filament' }).click()
  await skuDraft.locator('#create-preview').getByText('FIX-123', { exact: true }).waitFor()
  await skuDraft.locator('#create-preview').getByText('233', { exact: true }).waitFor()
  await skuDraft.getByRole('button', { name: 'Cancel', exact: true }).click()
  await acceptedSku.uncheck()
  supplierDefaultSku = 'FIX-123'
  assert.equal(await skuDraft.getByRole('button', { name: /Fixture Brand PLA 1 kg/ }).count(), 0)
  assert.equal(await skuDraft.getByRole('checkbox', { name: /Spool Weight.*233/i }).count(), 0)
  await skuDraft.close()

  const discoveryDefaultModel = chatModelId
  chatModelId = 'fixture:default'
  const discovery = await context.newPage()
  discovery.setDefaultTimeout(5_000)
  await discovery.goto(`${url}?tab=new`)
  const discoveryInput = discovery.getByRole('searchbox', { name: 'SKU, product link, or description' })
  await discoveryInput.waitFor()
  assert.equal(await discoveryInput.getAttribute('placeholder'), 'Paste an SKU, product link, or what you know — e.g. Paramount 3D PLA purple')
  assert.equal(await discovery.getByLabel('Manufacturer', { exact: true }).isVisible(), false)
  assert.equal(await discovery.locator('#research-options').evaluate(element => element.open), false)
  await discoveryInput.fill('Paramount 3D PLA purple')
  await discovery.getByRole('tab', { name: 'Chat', exact: true }).click()
  await discovery.getByRole('tab', { name: 'New filament', exact: true }).click()
  assert.equal(await discoveryInput.inputValue(), 'Paramount 3D PLA purple')
  const discoveryRequest = discovery.waitForRequest(req => req.url().endsWith('/proposals'))
  await discovery.getByRole('button', { name: 'Find specifications' }).click()
  assert.equal((await discoveryRequest).postDataJSON().query, 'Paramount 3D PLA purple')
  assert.equal(await discovery.locator('#research-follow-up-scope').isVisible(), false, 'discovery has no proposed fields to focus yet')
  const candidateCards = discovery.locator('.research-candidate')
  await candidateCards.first().waitFor()
  assert.equal(await candidateCards.count(), 1, 'duplicate candidate URLs produce one card')
  assert.equal(await candidateCards.locator('h3').textContent(), '<Fixture purple PLA>')
  assert.equal(await candidateCards.locator('h3 > *').count(), 0, 'candidate titles remain plain text')
  assert.equal(await candidateCards.locator('.research-candidate-identity').textContent(),
    'Manufacturer: Fixture Brand · SKU: FIX-123 · Diameter (mm): 1.75 · Net filament weight (g): 1000 · Color name: Purple')
  assert.equal(await candidateCards.getByRole('link', { name: 'Fixture catalog' }).getAttribute('href'), evidence[0].url)
  await candidateCards.getByText('Source product code: SHOP-789', { exact: true }).waitFor()
  await candidateCards.getByText('Similar SKU — check the variant', { exact: true }).waitFor()
  await discovery.getByText('Some sources could not be searched. Try again or add a product link.', { exact: true }).waitFor()
  assert.equal(await discovery.locator('#save-filament').isDisabled(), true, 'candidate discovery cannot save a fuzzy match')
  const writesBeforeChoice = writes.length
  await discovery.evaluate(() => {
    const key = 'filabrain:chat:7'
    const history = JSON.parse(localStorage.getItem(key) || '[]')
    for (const conversation of history) {
      if (conversation.research?.fields.query === 'Paramount 3D PLA purple') delete conversation.model
    }
    localStorage.setItem(key, JSON.stringify(history))
  })
  await discovery.reload()
  await discovery.locator('[data-chat-history]:visible summary').click()
  await discovery.getByRole('button', { name: 'Find specifications · Paramount 3D PLA purple', exact: true }).click()
  assert.equal(await discoveryInput.inputValue(), 'Paramount 3D PLA purple', 'query survives saved research history')
  await candidateCards.first().waitFor()
  const discoveryModel = discovery.locator('[data-chat-form]:visible [data-model-trigger]')
  await discoveryModel.getByText('Choose a model', { exact: true }).waitFor()
  const retryDiscovery = discovery.waitForResponse(response => response.url().endsWith('/proposals'))
  await discovery.getByRole('button', { name: 'Find specifications' }).click()
  await (await retryDiscovery).finished()
  await discovery.locator('#research-start-actions button[value="fill"]:not(:disabled)').waitFor()
  assert.equal(await discoveryModel.getByText('Choose a model', { exact: true }).count(), 1, 'discovery does not select or preserve a model')
  const proposalsBeforeChoice = proposalRequests
  await candidateCards.getByRole('button', { name: 'Research this page' }).click()
  await discovery.locator('#research-status').getByText('Choose a model', { exact: true }).waitFor()
  assert.equal(proposalRequests, proposalsBeforeChoice, 'full product-page research still requires model selection')
  await discoveryModel.click()
  await discovery.getByRole('menu', { name: 'Model for this conversation' })
    .getByRole('menuitemradio', { name: /^fixture:default(?: Default)?$/ }).first().click()
  const chosenCandidate = discovery.waitForRequest(req => req.url().endsWith('/proposals'))
  await candidateCards.getByRole('button', { name: 'Research this page' }).click()
  const chosenPayload = (await chosenCandidate).postDataJSON()
  assert.equal(chosenPayload.query, '', 'choosing a candidate clears the discovery constraint')
  assert.equal(chosenPayload.product_url, evidence[0].url)
  assert.equal(chosenPayload.identity.sku, '', 'a candidate SKU is not silently promoted into a strict constraint')
  await discovery.getByRole('checkbox', { name: /Density/ }).waitFor()
  assert.equal(writes.length, writesBeforeChoice, 'choosing a candidate only produces a reviewable draft')
  await openResearchDetails(discovery)
  assert.equal(await discovery.locator('[name="product_url"]').inputValue(), evidence[0].url)
  await discoveryInput.fill('A different filament')
  assert.equal(await discovery.locator('[name="product_url"]').inputValue(), '', 'editing the discovery query clears the previously selected URL')
  await discoveryInput.fill(evidence[0].url)
  const pastedUrl = discovery.waitForRequest(req => req.url().endsWith('/proposals'))
  await discovery.getByRole('button', { name: 'Find specifications' }).click()
  const urlPayload = (await pastedUrl).postDataJSON()
  assert.equal(urlPayload.product_url, evidence[0].url)
  assert.equal(urlPayload.query, '', 'pasted URLs use the existing product-page research path')
  await discovery.locator('#research-start-actions button[value="fill"]:not(:disabled)').waitFor()
  const beforeLongQuery = proposalRequests
  await discoveryInput.fill('x'.repeat(401))
  assert.equal(await discoveryInput.evaluate(input => input.validity.customError), true)
  await discovery.getByRole('button', { name: 'Find specifications' }).click()
  assert.equal(proposalRequests, beforeLongQuery, 'overlong prose is rejected before a request')
  await discoveryInput.fill(longProductUrl)
  assert.equal(await discoveryInput.inputValue(), longProductUrl, 'product URLs are not truncated at 400 characters')
  assert.equal(await discoveryInput.evaluate(input => input.checkValidity()), true)
  const longUrlRequest = discovery.waitForRequest(req => req.url().endsWith('/proposals'))
  await discovery.getByRole('button', { name: 'Find specifications' }).click()
  assert.equal((await longUrlRequest).postDataJSON().product_url, longProductUrl)
  await discovery.locator('#research-start-actions button[value="fill"]:not(:disabled)').waitFor()
  for (const skuQuery of ['FIX-12X', 'FIX 123']) {
    await discoveryInput.fill(skuQuery)
    const skuDiscovery = discovery.waitForRequest(req => req.url().endsWith('/proposals'))
    await discovery.getByRole('button', { name: 'Find specifications' }).click()
    assert.equal((await skuDiscovery).postDataJSON().query, skuQuery)
    await candidateCards.nth(1).waitFor()
    assert.equal(await candidateCards.nth(0).locator('h3').textContent(), await candidateCards.nth(1).locator('h3').textContent())
    assert.match(await candidateCards.nth(0).locator('.research-candidate-identity').textContent(), /SKU: FIX-123.*1.75.*1000.*Purple/)
    assert.match(await candidateCards.nth(1).locator('.research-candidate-identity').textContent(), /SKU: FIX-285.*2.85.*500.*Blue/)
    const selectedSku = discovery.waitForRequest(req => req.url().endsWith('/proposals'))
    await candidateCards.nth(0).getByRole('button', { name: 'Research this page' }).click()
    const selectedSkuPayload = (await selectedSku).postDataJSON()
    assert.equal(selectedSkuPayload.query, '')
    assert.equal(selectedSkuPayload.product_url, evidence[0].url)
    assert.equal(selectedSkuPayload.sku_hint, 'FIX-123', 'explicit product choice replaces an inferred search clue with the selected manufacturer SKU')
    await discovery.locator('#research-start-actions button[value="fill"]:not(:disabled)').waitFor()
    const refreshSku = discovery.waitForRequest(req => req.url().endsWith('/proposals'))
    await discovery.getByRole('button', { name: 'Find specifications' }).click()
    assert.equal((await refreshSku).postDataJSON().sku_hint, 'FIX-123', 'refresh retains the explicitly selected manufacturer SKU')
    await discovery.locator('#research-start-actions button[value="fill"]:not(:disabled)').waitFor()
  }
  await openResearchDetails(discovery)
  const advancedDetails = discovery.locator('#research-advanced-fields')
  if (!await advancedDetails.evaluate(element => element.open)) await advancedDetails.locator(':scope > summary').click()
  await discovery.locator('[name="manufacturer_sku"]').fill('FIX-12X')
  await discoveryInput.fill('explicit SKU fixture')
  const explicitDiscovery = discovery.waitForResponse(response => response.url().endsWith('/proposals'))
  await discovery.getByRole('button', { name: 'Find specifications' }).click()
  await (await explicitDiscovery).finished()
  await discovery.locator('#research-start-actions button[value="fill"]:not(:disabled)').waitFor()
  const explicitChoice = discovery.waitForRequest(req => req.url().endsWith('/proposals'))
  await candidateCards.first().getByRole('button', { name: 'Research this page' }).click()
  const explicitChoicePayload = (await explicitChoice).postDataJSON()
  assert.equal(explicitChoicePayload.identity.sku, 'FIX-12X', 'a product card preserves an explicitly entered manufacturer SKU')
  assert.equal(explicitChoicePayload.sku_hint, '', 'a card cannot add a conflicting inferred hint to an explicit SKU')
  await discovery.close()
  chatModelId = discoveryDefaultModel

  draftColorMode = true
  const colorDraft = await context.newPage()
  await colorDraft.goto(url)
  await colorDraft.getByRole('tab', { name: 'New filament', exact: true }).click()
  const colorForm = colorDraft.locator('#product-research')
  await colorForm.locator('#research-options').evaluate(el => { el.open = true })
  await colorDraft.getByLabel('Manufacturer', { exact: true }).fill(manufacturer.name)
  await colorDraft.getByLabel('Product name', { exact: true }).fill('PLA (Decepticon Purple)')
  await colorForm.locator('[name=manufacturer_color_name]').fill('Decepticon Purple')
  await colorDraft.locator('#research-advanced-fields').evaluate(el => { el.open = true })
  await colorDraft.getByRole('textbox', { name: 'Material', exact: true }).fill('PLA')
  await colorDraft.getByRole('spinbutton', { name: 'Diameter (mm)', exact: true }).fill('1.75')
  let releaseSwatches
  swatchResponse = new Promise(resolve => { releaseSwatches = resolve })
  const swatchRequest = colorDraft.waitForRequest(request => request.url().endsWith('/color-previews'))
  await colorDraft.getByRole('button', { name: 'Find specifications' }).click()
  await swatchRequest
  await colorDraft.locator('#product-research button[value=fill]:enabled').waitFor()
  assert.equal(await colorDraft.locator('#save-filament').isDisabled(), true,
    'a new filament cannot be saved while its color choices are still loading')
  releaseSwatches()
  swatchResponse = undefined
  const swatches = colorDraft.locator('[data-draft-swatches]')
  await swatches.getByText('RAL 4007', { exact: false }).first().waitFor()
  await colorDraft.getByRole('button', { name: 'Save new filament', exact: true }).click()
  assert.equal(await colorDraft.locator('#create-confirm').evaluate(el => el.open), false,
    'a known color reference must not silently save a new filament without a library swatch')
  assert.equal(await swatches.getByRole('combobox').evaluate(el => el === document.activeElement), true)
  await swatches.getByRole('combobox').selectOption('keep')
  await swatches.getByRole('button', { name: 'Continue without color', exact: true }).click()
  await colorDraft.getByRole('button', { name: 'Save new filament', exact: true }).click()
  await colorDraft.locator('#create-confirm[open]').waitFor()
  await colorDraft.locator('#create-cancel').click()
  await swatches.getByRole('combobox').selectOption('reuse:1')
  await swatches.getByRole('button', { name: 'Use swatch', exact: true }).click()
  await colorDraft.getByRole('checkbox', { name: /Library color/ }).waitFor()
  assert.equal(await colorDraft.getByRole('checkbox', { name: /Library color/ }).isChecked(), true)
  await swatches.getByRole('combobox').selectOption('create')
  let releaseColor
  colorResponse = new Promise(resolve => { releaseColor = resolve })
  await swatches.getByRole('button', { name: 'Use swatch', exact: true }).click()
  await colorDraft.locator('#save-filament:disabled').waitFor()
  assert.equal(await colorDraft.locator('#create-confirm').evaluate(el => el.open), false, 'pending color creation cannot open a stale save confirmation')
  releaseColor()
  colorResponse = undefined
  await colorDraft.locator('#save-filament:enabled').waitFor()
  assert.equal(colorWrites.at(-1).hex_code, '#44263C')
  assert.equal(colorWrites.at(-1).name, 'Purple violet (RAL 4007)')
  assert.equal(await colorDraft.getByRole('checkbox', { name: /Library color/ }).isChecked(), true)
  await colorDraft.getByRole('button', { name: 'Find spool specs' }).click()
  await colorDraft.getByRole('button', { name: 'This matches: Fixture Brand PLA 1 kg', exact: true }).click()
  await colorDraft.getByRole('button', { name: 'Save new filament', exact: true }).click()
  await colorDraft.locator('#create-confirm[open]').waitFor()
  await colorDraft.locator('#create-filament').click()
  await colorDraft.getByRole('link', { name: 'Open saved filament' }).waitFor()
  assert.deepEqual(writes.at(-1).colors, [{ color_id: 23 }])
  assert.equal(writes.at(-1).manufacturer_color_name, 'Decepticon Purple')
  assert.equal(writes.at(-1).default_spool_weight_g, 233)
  assert.equal(writes.at(-1).spool_outer_diameter_mm, 200)
  assert.equal(writes.at(-1).spool_width_mm, 66)
  await colorDraft.close()
  const manualDraft = await context.newPage()
  manualDraft.setDefaultTimeout(5000)
  await manualDraft.goto(`${url}?tab=new`)
  await openResearchDetails(manualDraft)
  await manualDraft.getByLabel('Manufacturer', { exact: true }).fill(manufacturer.name)
  await manualDraft.getByLabel('Product name', { exact: true }).fill('Manual swatch PLA')
  await manualDraft.locator('#research-advanced-fields').evaluate(el => { el.open = true })
  await manualDraft.getByLabel('Material', { exact: true }).fill('PLA')
  await manualDraft.getByLabel('Diameter (mm)', { exact: true }).fill('1.75')
  await manualDraft.locator('#draft-palette').selectOption('1')
  await manualDraft.getByRole('button', { name: 'Find specifications' }).click()
  await manualDraft.locator('#save-filament:enabled').waitFor()
  await openResearchDetails(manualDraft)
  await manualDraft.locator('#create-draft-color').click()
  const manualDialog = manualDraft.locator('#draft-color-dialog')
  await manualDialog.getByLabel('Color name', { exact: true }).fill('My purple')
  await manualDialog.getByLabel('Color hex', { exact: true }).fill('#123ABC')
  await manualDialog.getByRole('button', { name: 'Create swatch', exact: true }).click()
  await manualDialog.waitFor({ state: 'hidden' })
  assert.equal(await manualDraft.getByRole('checkbox', { name: /Library color.*My purple/ }).isChecked(), true, 'an explicit manual swatch replaces the selected draft color')
  await manualDraft.getByRole('button', { name: 'Save new filament', exact: true }).click()
  await manualDraft.locator('#create-confirm[open]').waitFor()
  await manualDraft.locator('#create-filament').click()
  await manualDraft.getByRole('link', { name: 'Open saved filament' }).waitFor()
  assert.deepEqual(writes.at(-1).colors, [{ color_id: 24 }])
  await manualDraft.close()

  const makerDraft = await context.newPage()
  makerDraft.setDefaultTimeout(5000)
  await makerDraft.goto(`${url}?tab=new`)
  await openResearchDetails(makerDraft)
  const makerName = makerDraft.getByLabel('Manufacturer', { exact: true })
  await makerName.fill('Manufacturer A')
  let releaseMaker
  makerResponse = new Promise(resolve => { releaseMaker = resolve })
  const creatingMaker = makerDraft.waitForRequest(request => request.url().endsWith('/manufacturers') && request.method() === 'POST')
  await makerDraft.locator('#create-draft-manufacturer').click()
  await creatingMaker
  await makerName.fill('Manufacturer B')
  releaseMaker()
  makerResponse = undefined
  await makerDraft.locator('#create-draft-manufacturer:enabled').waitFor()
  assert.equal(await makerName.inputValue(), 'Manufacturer B', 'a delayed manufacturer response cannot overwrite newer typing')
  await makerDraft.locator('#create-draft-manufacturer').click()
  await makerDraft.locator('#create-draft-manufacturer').waitFor({ state: 'hidden' })
  assert.equal(manufacturerWrites.at(-1).name, 'Manufacturer B')
  await makerDraft.close()
  draftColorMode = false

  editorMode = 'edit'
  chatModelId = 'fixture:default'
  hostEditorToken = editorRecordToken
  const sharedEditor = await context.newPage()
  sharedEditor.setDefaultTimeout(5000)
  await sharedEditor.goto('https://fila.test/editor-fixture')
  const shared = sharedEditor.frameLocator('iframe')
  await shared.locator('[data-tab=editor][aria-selected=true]').waitFor()
  await shared.locator('[data-view=editor] [name=query]').fill('Fixture purple')
  const editedDiscovery = sharedEditor.waitForRequest(request => request.url().endsWith('/proposals') && request.postDataJSON().query)
  await shared.getByRole('button', { name: 'Find specifications', exact: true }).click()
  assert.equal((await editedDiscovery).postDataJSON().filament_id, 10)
  await shared.locator('[data-view=editor] .research-candidate').waitFor()
  let releaseContext
  heldContextLoad = new Promise(resolve => { releaseContext = resolve })
  await shared.locator('[data-tab=chat]').click()
  const chatInput = shared.getByRole('textbox', { name: 'Your question', exact: true })
  const conversationRequest = sharedEditor.waitForRequest(request => request.url().endsWith('/filabrain/chat'))
  await chatInput.fill('Explain this filament')
  await chatInput.press('Enter')
  assert.equal(sharedChatRequests.length, 0, 'a contextual question waits for filament preload')
  releaseContext()
  heldContextLoad = undefined
  assert.equal((await conversationRequest).postDataJSON().filament_id, 10, 'general chat preloads the host filament')
  await shared.getByText('PLA is easier to print.', { exact: true }).waitFor()
  await shared.locator('[data-tab=editor]').click()
  assert.equal(await shared.locator('[data-view=editor] [name=query]').inputValue(), 'Fixture purple')
  await shared.locator('[data-tab=new]').click()
  const freshQuery = shared.locator('[data-view=new] [name=query]')
  await freshQuery.fill('Another product')
  const freshDiscovery = sharedEditor.waitForRequest(request => request.url().endsWith('/proposals') && request.postDataJSON().query === 'Another product')
  await shared.getByRole('button', { name: 'Find specifications', exact: true }).click()
  const freshTarget = (await freshDiscovery).postDataJSON()
  assert.equal(freshTarget.mode, 'create')
  assert.equal(freshTarget.filament_id, null, 'fresh creation cannot reuse the bound editor target')
  await sharedEditor.close()

  canUseFilaments = false
  await page.goto(url)
  await page.getByRole('region', { name: 'Model settings' }).waitFor()
  assert.equal(await page.getByRole('navigation', { name: 'FilaBrain' }).count(), 0)
  console.log('FilaBrain plugin page: both scales, NFC reader, spool lookup, chat, create/edit, batches and the MCP migration guide passed')
} finally {
  await browser.close()
}
