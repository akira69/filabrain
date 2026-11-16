import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const html = await readFile(new URL('../filabrain/page.html', import.meta.url), 'utf8')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
const requests = []

try {
  const page = await browser.newPage()
  page.setDefaultTimeout(3_000)
  await page.route('https://fila.test/**', async route => {
    const request = route.request()
    const { pathname, searchParams } = new URL(request.url())
    if (pathname === '/plugin-page/filabrain') return route.fulfill({ contentType: 'text/html', body: html })
    if (pathname === '/api/v1/me') return route.fulfill({ json: { id: 42 } })
    if (pathname === '/api/v1/filabrain/status') return route.fulfill({ json: {
      enabled: true, can_chat: true, can_read_printers: true, chat_model: 'test:model', chat_endpoint_id: 'default',
    } })
    if (pathname === '/api/v1/filabrain/models') return route.fulfill({ json: {
      default_model_id: 'test:model', default_endpoint_id: 'default',
      groups: [{ id: 'default', name: 'Fixture endpoint', models: ['test:model'] }],
    } })
    if (pathname === '/api/v1/printers') return route.fulfill({ json: {
      items: [{ id: 21, name: 'Workshop A1', is_active: true }], total: 1,
    } })
    if (pathname === '/api/v1/filaments' && searchParams.get('search') === 'PETG HF') return route.fulfill({ json: {
      items: [{ id: 37, designation: 'PETG HF', manufacturer: { name: 'Bambu Lab' } }], total: 1,
    } })
    if (pathname === '/api/v1/filabrain/chat') {
      requests.push(request.postDataJSON())
      return route.fulfill({ json: {
        reply: 'This spool is suitable [spool-9]. Read the reference [source:uv].',
        action: null, search: 'failed', required_g: 80, notices: [],
        evidence: [
          { id: 'spool-9', kind: 'spool', title: 'Bambu Lab PETG HF', url: '/spools/9', remaining_g: 145, meets_required: true, status: 'Opened' },
          { id: 'spool-11', kind: 'spool', title: 'Unweighed PETG', url: '/spools/11', remaining_g: null, meets_required: null, status: 'New' },
          { id: 'source:uv', kind: 'guidance', title: 'Outdoor filament guide', url: 'https://maker.example/materials', text: 'PETG is suitable for many outdoor uses.',
            details: { test_conditions: ['Annealed test parts'] },
            links: [{ title: 'Measurement method', url: 'https://maker.example/method' }] },
          { id: 'source:unsafe', kind: 'guidance', title: 'Unsafe link fixture', url: 'javascript:alert(1)', text: 'Untrusted text.' },
        ],
      } })
    }
    return route.fulfill({ status: 404, json: {} })
  })

  await page.goto('https://fila.test/plugin-page/filabrain?tab=chat')
  await page.getByText('Printer and filament', { exact: true }).click()
  await page.locator('[data-chat-printer]').selectOption('21')
  await page.getByRole('searchbox', { name: 'Find a filament' }).fill('PETG HF')
  await page.locator('[data-chat-filament]').selectOption('37')
  await page.locator('[data-chat-nozzle]').selectOption('0.4')
  await page.getByRole('textbox', { name: 'Your question' }).fill('What do I have for an outdoor bracket?')
  await page.getByRole('button', { name: 'Search the web' }).click()

  const facts = page.locator('[data-chat-evidence]')
  await facts.getByText('145 g', { exact: false }).waitFor()
  await facts.getByText('Remaining weight unknown', { exact: false }).waitFor()
  await facts.getByText('Required weight · 80 g', { exact: true }).waitFor()
  await facts.getByText('Web search was unavailable.', { exact: false }).waitFor()
  assert.equal(await facts.getByRole('link', { name: 'Bambu Lab PETG HF' }).getAttribute('href'), '/spools/9')
  assert.equal(await facts.getByRole('link', { name: 'Outdoor filament guide' }).getAttribute('href'), 'https://maker.example/materials')
  assert.equal(await facts.getByRole('link', { name: 'Measurement method' }).getAttribute('href'), 'https://maker.example/method')
  await facts.getByText('Annealed test parts', { exact: false }).waitFor()
  assert.equal(await facts.getByRole('link', { name: 'Unsafe link fixture' }).count(), 0)
  const citation = page.getByRole('link', { name: '[spool-9]', exact: true })
  assert.ok(await page.locator(await citation.getAttribute('href')).count())
  assert.equal(requests.length, 1)
  assert.deepEqual(
    { web_search: requests[0].web_search, printer_id: requests[0].printer_id,
      filament_id: requests[0].filament_id, nozzle_mm: requests[0].nozzle_mm },
    { web_search: true, printer_id: 21, filament_id: 37, nozzle_mm: 0.4 },
  )
  console.log('Grounded chat renders inventory, citations, controls, and search status.')
} finally {
  await browser.close()
}
