import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const html = await readFile(new URL('../filabrain/page.html', import.meta.url), 'utf8')
const art = await readFile(new URL('../filabrain/guide-art.webp', import.meta.url))
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
const origin = 'https://fila.test'
const endpoint = { id: 'local', name: 'Ollama', model_url: 'http://ollama:11434/v1', model_id: 'qwen3:8b', has_key: false, models: ['qwen3:8b'], hidden_models: [] }

try {
  const context = await browser.newContext()
  const fixture = route => {
    const path = new URL(route.request().url()).pathname
    if (path.startsWith('/plugin-page/')) return route.fulfill({ contentType: 'text/html', body: html })
    if (path.endsWith('/guide-art')) return route.fulfill({ contentType: 'image/webp', body: art })
    if (path === '/filament-picker/') return route.fulfill({ contentType: 'text/html', body: `<!doctype html>
      <html data-theme="dark"><head><style>
        :root { --bg-elevated: #1b2c24; --gradient-hero: linear-gradient(red, blue); }
        body { margin: 0; } .fm-page { background: var(--gradient-hero); }
      </style></head><body data-embedded><div id="fm-page" class="fm-page">
        <div data-filament-picker="1">Filament selection</div>
      </div></body></html>` })
    if (path.endsWith('/status')) return route.fulfill({ json: { enabled: true, can_chat: true, can_create: true,
      can_add_spool: true, can_update: true, can_configure: true, chat_model: endpoint.model_id, chat_endpoint_id: endpoint.id } })
    if (path.endsWith('/settings')) return route.fulfill({ json: { model_endpoints: [endpoint], default_endpoint_id: endpoint.id,
      supplier_download_mib: 8, supplier_sites: [], search_url: '', model_revision: 'fixture' } })
    if (path.endsWith('/filabrain/models')) return route.fulfill({ json: { groups: [{ id: endpoint.id, name: endpoint.name, models: endpoint.models }], default_endpoint_id: endpoint.id, default_model_id: endpoint.model_id } })
    if (path.endsWith('/me')) return route.fulfill({ json: { id: 1 } })
    if (path.endsWith('/batches')) return route.fulfill({ json: [] })
    if (path.endsWith('/manufacturers') || path.endsWith('/colors')) return route.fulfill({ json: { items: [], total: 0 } })
    return route.fulfill({ status: 404, json: {} })
  }
  await context.route(origin + '/**', fixture)
  const page = await context.newPage()
  page.setDefaultTimeout(5000)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  const chat = page.locator('.research-chat')
  const guide = page.getByRole('dialog', { name: 'Guide' })
  const openGuide = page.locator('#plugin-header').getByRole('button', { name: 'Guide' })
  async function screenshot(name) {
    if (process.env.ARTIFACT_DIR) await page.screenshot({ path: `${process.env.ARTIFACT_DIR}/${name}.png` })
  }
  async function noOverflow() {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), page.viewportSize().width)
  }

  for (const width of [320, 390, 768, 800, 1024, 1280, 1440, 1920]) {
    await page.setViewportSize({ width, height: 1000 })
    await page.goto(origin + '/plugin-page/filabrain')
    await page.getByRole('textbox', { name: 'Your question' }).waitFor()
    const before = await chat.boundingBox()
    assert.ok(before.y + before.height > 970, 'chat fills the viewport height')
    assert.ok(before.y + before.height <= 1000, 'chat fits within the viewport')
    assert.equal(await page.locator('[data-chat-prompt] svg[aria-hidden=true]').count(), 4)
    assert.equal(await page.locator('.chat-transcript').evaluate(el => getComputedStyle(el).scrollbarWidth), 'thin')
    assert.ok((await page.locator('.chat-header').boundingBox()).height <= 64)
    await page.getByRole('button', { name: 'Compare materials', exact: true }).click()
    assert.notEqual(await page.getByRole('textbox', { name: 'Your question' }).inputValue(), '')
    await page.getByRole('textbox', { name: 'Your question' }).fill('')
    await screenshot(`chat-${width}`)
    await openGuide.click()
    await guide.waitFor()
    const after = await chat.boundingBox()
    assert.equal(after.width, before.width, 'opening the guide does not squeeze chat')
    const bounds = await guide.boundingBox()
    assert.equal(await page.locator('.docs-header').evaluate(el => getComputedStyle(el).backgroundColor), await page.locator('.chat-header').evaluate(el => getComputedStyle(el).backgroundColor))
    assert.ok(Math.abs((await page.locator('.docs-header').boundingBox()).height - (await page.locator('.chat-header').boundingBox()).height) <= 1, 'guide and chat header heights match')
    assert.equal(await guide.locator('.docs-content dl').evaluate(el => getComputedStyle(el).display), 'block', 'guide text spans one column')
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1)
    assert.ok(bounds.y + bounds.height <= 1000)
    if (width <= 800) assert.ok(await guide.evaluate(el => el.matches(':modal')))
    if (width === 1920) {
      assert.ok(bounds.x >= after.x + after.width, 'wide guide uses the spare space to the right')
      assert.ok(bounds.width > 500, 'wide guide uses the spare horizontal space')
    }
    await screenshot(`guide-${width}`)
    await noOverflow()
    await guide.getByRole('button', { name: 'Close' }).click()
    assert.ok(await openGuide.evaluate(el => el === document.activeElement))
  }
  for (const [width, height] of [[390, 540], [844, 390]]) {
    await page.setViewportSize({ width, height })
    const composer = await page.locator('[data-chat-form]').boundingBox()
    assert.ok(composer.y >= 0 && composer.y + composer.height <= height, 'composer fits short phone windows')
    await noOverflow()
  }
  for (const theme of ['brand', 'light', 'dark']) {
    await page.evaluate(value => document.documentElement.dataset.theme=value, theme)
    const colors=await page.locator('#plugin-header .filabrain-mark').evaluate(svg=>({
      text:getComputedStyle(svg).color,
      strokes:[...svg.querySelectorAll('g[stroke]')].map(el=>getComputedStyle(el).stroke),
      fill:getComputedStyle(svg.querySelector('g[fill]:not([fill="none"])')).fill,
    }))
    assert.ok(colors.strokes.every(stroke=>stroke===colors.text))
    assert.equal(colors.fill,colors.text)
  }
  await page.evaluate(()=>document.documentElement.dataset.theme='brand')
  await page.locator('#research-chat-title').evaluate(async heading=>{
    const value=getComputedStyle(heading,'::before').maskImage
    if(value==='none') throw new Error('Chat header icon missing')
    const image=new Image()
    image.src=value.slice(5,-2)
    await image.decode()
  })
  // Changing the available width switches between a docked guide and drawer.

  await page.setViewportSize({ width: 1920, height: 1000 })
  await openGuide.click()
  const wideGuide = (await guide.boundingBox()).width
  await page.setViewportSize({ width: 1600, height: 1000 })
  assert.ok((await guide.boundingBox()).width < wideGuide, 'guide shrinks first')
  await page.setViewportSize({ width: 1024, height: 720 })
  await noOverflow()
  await page.setViewportSize({ width: 390, height: 720 })
  await page.locator('#filabrain-docs:modal').waitFor()
  await guide.getByRole('button', { name: 'Close' }).click()

  for (const width of [320, 390, 768, 800, 1024, 1280, 1440, 1920]) {
    await page.setViewportSize({ width, height: 1000 })
    await page.goto(origin + '/plugin-page/filabrain?tab=settings')
    const name = page.getByRole('textbox', { name: 'Endpoint name', exact: true })
    await name.waitFor()
    await page.locator('#research-model-endpoint:not(:disabled)').waitFor()
    const apiKey = page.getByLabel('API key (optional)', { exact: true })
    const endpointCard = page.locator('#research-endpoint-fields')
    assert.equal(await page.locator('#research-add-endpoint').evaluate(el => !!el.closest('.fm-section-header')), true)
    if (width >= 768) {
      assert.equal((await name.boundingBox()).y, (await apiKey.boundingBox()).y, 'name and key share a row')
      assert.ok((await endpointCard.boundingBox()).height < 510, 'endpoint editing is compact')
    }
    await screenshot(`settings-${width}`)
    await noOverflow()
    await openGuide.click()
    await guide.waitFor()
    await noOverflow()
    await guide.getByRole('button', { name: 'Close' }).click()
  }
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto(origin + '/plugin-page/filabrain?tab=library')
  const picker = page.frameLocator('#library-picker')
  await picker.locator('[data-filament-picker]').waitFor()
  assert.equal(await picker.locator('#fm-page').evaluate(el => getComputedStyle(el).backgroundImage), 'none')
  assert.equal(await picker.locator('body').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(27, 44, 36)')
  await screenshot('library-1280')
  const touch = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })
  await touch.route(origin + '/**', fixture)
  const phone = await touch.newPage()
  await phone.goto(origin + '/plugin-page/filabrain?tab=settings')
  const phoneUrl = phone.getByRole('textbox', { name: 'Model server URL', exact: true })
  await phoneUrl.waitFor()
  assert.equal(await phoneUrl.evaluate(el => getComputedStyle(el).fontSize), '16px', 'phone inputs avoid automatic zoom')
  assert.ok((await phoneUrl.boundingBox()).height >= 44)
  assert.ok((await phone.locator('#research-add-endpoint').boundingBox()).height >= 44)
  assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth), 390)
  await phone.getByRole('button', { name: 'Chat', exact: true }).click()
  await phone.getByRole('textbox', { name: 'Your question' }).waitFor()
  assert.ok((await phone.locator('[data-chat-new]').boundingBox()).height >= 44)
  assert.ok((await phone.locator('.chat-header').boundingBox()).height <= 64)
  await touch.close()
  assert.deepEqual(errors, [])
  console.log('Full-height chat, responsive guide, compact endpoints, prompt icons, scrollbars, and picker surface passed.')
} finally {
  await browser.close()
}
