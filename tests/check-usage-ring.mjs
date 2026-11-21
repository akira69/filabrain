import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const { build } = await import(process.env.ESBUILD_MODULE || 'esbuild')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const bundle = await build({ entryPoints: [fileURLToPath(new URL('../frontend/context-usage.ts', import.meta.url))], bundle: true,
  format: 'iife', globalName: 'ContextUsage', write: false, target: ['es2022'] })
const css = await readFile(new URL('../frontend/context-usage.css', import.meta.url), 'utf8')
const html = `<style>:root{--text:#eee;--text-muted:#aaa;--accent:#f80;--border:#666;--bg-elevated:#222}
  body{margin:0} [data-chat-usage]{position:fixed;right:0;bottom:0} ${css}</style>
  <div data-chat-usage></div><script>${bundle.outputFiles[0].text}
  window.usageRing=ContextUsage.mountContextUsage(document.querySelector('[data-chat-usage]'))</script>`
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 800 } })
  page.setDefaultTimeout(5000)
  await page.setContent(html)
  const host = page.locator('[data-chat-usage]')
  const button = host.locator('button')
  const menu = host.locator('[popover]')
  async function update(usage, options = {}) {
    await page.evaluate(({ usage, options }) => window.usageRing.update(usage, { visible: true, model: 'local-model', ...options }), { usage, options })
  }
  async function open() {
    await button.click()
    await page.waitForFunction(() => document.querySelector('button').getAttribute('aria-expanded') === 'true')
  }
  async function assertFits() {
    await page.waitForFunction(() => document.querySelector('[popover]').style.top)
    const bounds = await menu.boundingBox()
    const viewport = page.viewportSize()
    assert.ok(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= viewport.width && bounds.y + bounds.height <= viewport.height,
      `popover fits ${viewport.width}×${viewport.height}: ${JSON.stringify(bounds)}`)
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
  }

  assert.equal(await host.isVisible(), false, 'ring is hidden until caller shows it')
  await update({ prompt_tokens: 300, completion_tokens: 100, context_window: 10000 })
  assert.equal(await button.getAttribute('aria-label'), 'Context: 4% · 400 / 10,000 tokens')
  assert.equal(await host.locator('.chat-context-usage-arc').evaluate(el => el.style.strokeDashoffset), '96')
  await button.focus()
  await page.keyboard.press('Enter')
  await page.waitForFunction(() => document.querySelector('button').getAttribute('aria-expanded') === 'true')
  assert.match(await menu.innerText(), /Prompt: 300 · Reply: 100 tokens/)
  assert.match(await menu.innerText(), /Last request model: local-model/)
  await assertFits()
  await page.keyboard.press('Escape')
  assert.equal(await menu.evaluate(el => el.matches(':popover-open')), false)

  await update({ prompt_tokens: 90, completion_tokens: 10 })
  assert.equal(await button.getAttribute('aria-label'), 'Context: 100 tokens')
  assert.equal(await button.getAttribute('data-known'), 'false')
  await open()
  assert.match(await menu.innerText(), /did not report its context limit/)
  assert.doesNotMatch(await menu.innerText(), /%/)
  await update(undefined)
  assert.equal(await menu.evaluate(el => el.matches(':popover-open')), false, 'usage changes close details')
  assert.equal(await button.getAttribute('aria-label'), 'Context usage not reported')
  await open()
  assert.equal(await menu.locator('[data-usage-breakdown]').isVisible(), false)
  await update(undefined, { visible: false })
  assert.equal(await host.isVisible(), false)
  assert.equal(await menu.evaluate(el => el.matches(':popover-open')), false, 'hiding closes top-layer details')
  await update({ prompt_tokens: 400, context_window: 10000 })
  await open()
  await update({ prompt_tokens: 400, context_window: 10000 }, { model: 'other-model' })
  assert.equal(await menu.evaluate(el => el.matches(':popover-open')), false, 'model changes close details')
  await open()
  assert.match(await menu.innerText(), /Last request model: other-model/)

  await update({ prompt_tokens: 12000, completion_tokens: 1000, context_window: 10000 })
  assert.match(await button.getAttribute('aria-label'), /130%/)
  assert.equal(await host.locator('.chat-context-usage-arc').evaluate(el => el.style.strokeDashoffset), '0', 'only the arc is clamped')
  await update({ prompt_tokens: 100, context_window: 0 })
  assert.equal(await button.getAttribute('data-known'), 'false', 'zero is not a usable active limit')

  await page.setViewportSize({ width: 240, height: 320 })
  await update({ prompt_tokens: 300, completion_tokens: 100, context_window: 10000 }, { model: 'very-long-model-name-'.repeat(20) })
  await open()
  await assertFits()
  await update({ prompt_tokens: 300, context_window: 10000 })
  await host.evaluate(el => { el.style.top = '0'; el.style.bottom = 'auto'; el.style.left = '0'; el.style.right = 'auto' })
  await open()
  await assertFits()
  const mobile = await browser.newContext({ viewport: { width: 320, height: 480 }, isMobile: true, hasTouch: true })
  const phone = await mobile.newPage()
  await phone.setContent(`<meta name="viewport" content="width=device-width,initial-scale=1">${html}`)
  await phone.evaluate(() => window.usageRing.update({ prompt_tokens: 100 }, { visible: true }))
  const touchBounds = await phone.locator('button').boundingBox()
  assert.ok(touchBounds.width >= 44 && touchBounds.height >= 44, 'touch target is at least 44px')
  await mobile.close()
  console.log('Usage ring percentage, token-only, unavailable, keyboard, updates, overflow and touch target checks passed.')
} finally { await browser.close() }
