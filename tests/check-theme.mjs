import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const html = await readFile(new URL('../filabrain/page.html', import.meta.url), 'utf8')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
const origin = 'https://fila.test'
const pageUrl = origin + '/plugin-page/filabrain'

function contrast(a, b) {
  const luminance = color => color.match(/\d+/g).slice(0, 3).map(Number).reduce((sum, channel, index) => {
    const srgb = channel / 255
    const linear = srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4
    return sum + linear * [0.2126, 0.7152, 0.0722][index]
  }, 0)
  const [dark, light] = [luminance(a), luminance(b)].sort((x, y) => x - y)
  return (light + 0.05) / (dark + 0.05)
}

async function assertLightContrast(frame) {
  const { text, fill, link, card } = await frame.evaluate(() => {
    const primary = getComputedStyle(document.querySelector('.fm-btn-primary'))
    return {
      text: primary.color, fill: primary.backgroundColor,
      link: getComputedStyle(document.querySelector('.docs-content a')).color,
      card: getComputedStyle(document.querySelector('.fm-card')).backgroundColor,
    }
  })
  assert.ok(contrast(text, fill) >= 4.5, 'Light primary action text meets AA')
  assert.ok(contrast(link, card) >= 4.5, 'Light links on cards meet AA')
}

try {
  const context = await browser.newContext()
  const page = await context.newPage()
  page.setDefaultTimeout(5000)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await context.route(origin + '/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/host') return route.fulfill({
      contentType: 'text/html',
      body: `<!doctype html><html data-theme="dark"><head><style>
        :root {
          color-scheme: dark;
          --bg: #111827; --bg-elevated: #1f2937; --bg-soft: #374151;
          --text: #f9fafb; --text-muted: #9ca3af; --border: #4b5563;
          --accent: #38bdf8; --accent-2: #a78bfa; --accent-3: #fb923c;
          --input-bg: #374151; --btn-primary-text: #111827; --warning-text: #fdba74;
        }
        :root[data-theme="light"] {
          color-scheme: light;
          --bg: #fafafa; --bg-elevated: #ffffff; --bg-soft: #f3f4f6;
          --text: #18181b; --text-muted: #52525b; --border: #d4d4d8;
          --accent: #1c9b6b; --input-bg: #f3f4f6; --btn-primary-text: #ffffff;
        }
        :root.custom { --accent: #d946ef; }
      </style></head><body><iframe
        sandbox="allow-scripts allow-same-origin allow-forms"
        src="${pageUrl}${new URL(route.request().url()).search}"></iframe></body></html>`,
    })
    if (path === '/plugin-page/filabrain') return route.fulfill({ contentType: 'text/html', body: html })
    if (path === '/api/v1/filabrain/status') return route.fulfill({ json: { enabled: false } })
    return route.fulfill({ status: 404, json: {} })
  })

  async function colors(frame) {
    return frame.evaluate(() => {
      const root = getComputedStyle(document.documentElement)
      const primary = document.querySelector('.fm-btn-primary')
      return {
        theme: document.documentElement.dataset.theme,
        background: getComputedStyle(document.body).backgroundColor,
        accent: root.getPropertyValue('--accent').trim(),
        text: root.getPropertyValue('--text').trim(),
        border: root.getPropertyValue('--border').trim(),
        input: root.getPropertyValue('--input-bg').trim(),
        primaryText: getComputedStyle(primary).color,
        scheme: root.colorScheme,
        iconStroke: getComputedStyle(document.querySelector('.filabrain-mark g')).stroke,
        guideHeader: getComputedStyle(document.querySelector('.docs-header')).backgroundColor,
      }
    })
  }

  for (const query of ['', '?filament_host=editor', '?filament_host=selection']) {
    // The visible host theme must win over a stale saved preference.
    await page.goto(pageUrl)
    await page.evaluate(() => localStorage.setItem('theme', 'brand'))
    await page.goto(origin + '/host' + query)
    const frame = page.frameLocator('iframe')
    await frame.locator('#plugin-status').getByText('FilaBrain is disabled.').waitFor()
    const embedded = page.frames().find(item => item !== page.mainFrame())
    const initial = await colors(embedded)
    assert.equal(initial.theme, 'dark')
    assert.equal(initial.background, 'rgb(17, 24, 39)')
    assert.equal(initial.accent, '#38bdf8')
    assert.equal(initial.primaryText, 'rgb(17, 24, 39)')
    assert.equal(initial.scheme, 'dark')
    assert.equal(initial.iconStroke, 'rgb(249, 250, 251)', 'the monochrome icon follows text contrast')
    assert.equal(initial.guideHeader, 'rgb(55, 65, 81)', 'the compact Guide header uses the host surface')

    await page.evaluate(() => { document.documentElement.dataset.theme = 'light' })
    await frame.locator('html[data-theme=light]').waitFor()
    assert.equal((await colors(embedded)).background, 'rgb(250, 250, 250)')
    assert.equal((await colors(embedded)).primaryText, 'rgb(255, 255, 255)')
    assert.equal((await colors(embedded)).scheme, 'light')
    assert.equal((await colors(embedded)).iconStroke, 'rgb(24, 24, 27)')

    assert.equal((await colors(embedded)).accent, '#10734f')
    await assertLightContrast(embedded)

    await page.evaluate(() => document.documentElement.classList.add('custom'))
    await embedded.waitForFunction(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() === '#d946ef')
    await page.evaluate(() => document.documentElement.style.setProperty('--accent', '#ef4444'))
    await embedded.waitForFunction(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() === '#ef4444')
    const after = await colors(embedded)
    assert.equal(after.text, '#18181b')
    assert.equal(after.border, '#d4d4d8')
    assert.equal(after.input, '#f3f4f6')

    // A cached or replaced plugin page must release its observer on the host.
    await embedded.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true })))
    await page.evaluate(async () => {
      document.documentElement.style.setProperty('--accent', '#34d399')
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    })
    assert.equal((await colors(embedded)).accent, '#ef4444', 'hidden pages stop observing the host')
    await embedded.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })))
    await embedded.waitForFunction(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() === '#34d399')
    await page.evaluate(() => document.documentElement.style.setProperty('--accent', '#ef4444'))
    await embedded.waitForFunction(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() === '#ef4444')

    // Native events from localStorage changes in the host must not reset the visible theme.
    await page.evaluate(() => {
      localStorage.setItem('theme', 'brand')
      localStorage.setItem('theme', 'dark')
    })
    await embedded.waitForFunction(() => document.documentElement.dataset.theme === 'light')
    assert.equal((await colors(embedded)).accent, '#ef4444')
  }

  await page.goto(pageUrl)
  for (const [theme, background, accent] of [
    ['brand', 'rgb(12, 22, 18)', '#4ee3a2'],
    ['light', 'rgb(248, 246, 241)', '#10734f'],
    ['dark', 'rgb(11, 13, 18)', '#73d1ff'],
  ]) {
    await page.evaluate(value => localStorage.setItem('theme', value), theme)
    await page.reload()
    await page.getByText('FilaBrain is disabled.').waitFor()
    const actual = await colors(page)
    assert.equal(actual.background, background)
    if (theme === 'light') await assertLightContrast(page)
    assert.equal(actual.accent, accent)
  }
  const other = await context.newPage()
  await other.goto(pageUrl)
  await other.evaluate(() => localStorage.setItem('theme', 'light'))
  await page.locator('html[data-theme=light]').waitFor()
  await other.evaluate(() => localStorage.clear())
  await page.locator('html[data-theme=brand]').waitFor()
  assert.deepEqual(errors, [])
  console.log('FilaBrain follows host colors and live changes in sidebar, editor, selection, and standalone views.')
} finally {
  await browser.close()
}
