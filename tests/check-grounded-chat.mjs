import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const html = await readFile(new URL('../filabrain/page.html', import.meta.url), 'utf8')
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright-core')
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE_PATH })
const requests = []

try {
  const page = await browser.newPage()
  page.setDefaultTimeout(3_000)
  const photos = 'https://www.onlyspoolz.com/img/portfolio/'
  const photo = photos + 'spool_elegoo_pla_basic_1kg.jpg'
  const catalogPhoto = 'https://theemptyspool.cc/static/spools/elegoo--1-kg-cardboard.webp?v=1a336b7279ec'
  await page.route(photos + '**', route => route.fulfill({
    contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="150" height="150"/>',
  }))
  await page.route(catalogPhoto, route => route.fulfill({
    contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"/>',
  }))
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
      items: [
        { id: 37, designation: 'PETG HF', manufacturer: { name: 'Bambu Lab' } },
        { id: 38, designation: 'PETG HF Red', manufacturer: { name: 'Bambu Lab' } },
      ], total: 2,
    } })
    if (pathname === '/api/v1/filabrain/chat') {
      requests.push(request.postDataJSON())
      if (requests.at(-1).messages.at(-1).content.startsWith('What does my Elegoo')) return route.fulfill({ json: {
        reply: 'Which spool matches yours?', action: null,
        spool_profiles: [
          { id: 'onlyspoolz:elegoo-basic', title: 'Elegoo PLA Basic 1 kg', source: 'OnlySpoolz',
            url: 'https://www.onlyspoolz.com/portfolio/spool-elegoo-pla-basic/', photo,
            empty_weight_g: 233, outer_diameter_mm: 200, width_mm: 66, hole_diameter_mm: 58, net_weight_g: 1000 },
          { id: 'onlyspoolz:elegoo-rapid', title: 'Elegoo Rapid PLA+', source: 'OnlySpoolz',
            url: 'https://www.onlyspoolz.com/portfolio/spool-elegoo-rapid-pla-plus/',
            photo: photos + 'spool_elegoo_rapid_pla_plus.jpg', empty_weight_g: 156, width_mm: 56 },
          { id: 'unsafe', title: 'Untrusted card', source: 'Unknown', url: 'javascript:alert(1)',
            photo: 'https://tracker.example/spool.png' },
          { id: 'empty-spool:cardboard', title: 'Elegoo cardboard', source: 'The Empty Spool',
            url: 'https://theemptyspool.cc/brand/elegoo', photo: catalogPhoto, empty_weight_g: 158 },
        ],
      } })
      const question = requests.at(-1).messages.at(-1).content
      if (question === 'How wide is it?' && requests.at(-1).selected_spool_profile_id === 'onlyspoolz:elegoo-rapid') {
        return route.fulfill({ json: {
          reply: 'The newly selected spool is 56 mm wide.', action: null,
          selected_spool_profile_id: 'onlyspoolz:elegoo-rapid',
          spool_profiles: [{ id: 'onlyspoolz:elegoo-rapid', title: 'Elegoo Rapid PLA+', source: 'OnlySpoolz',
            url: 'https://www.onlyspoolz.com/portfolio/spool-elegoo-rapid-pla-plus/', empty_weight_g: 156, width_mm: 56 }],
        } })
      }
      if (question === 'What is the tare of the one I selected?' || question === 'How wide is it?') {
        return route.fulfill({ json: {
          reply: question.startsWith('What') ? 'The selected spool weighs 233 g.' : 'The selected spool is 66 mm wide.',
          action: null, selected_spool_profile_id: 'onlyspoolz:elegoo-basic',
          spool_profiles: [{ id: 'onlyspoolz:elegoo-basic', title: 'Elegoo PLA Basic 1 kg', source: 'OnlySpoolz',
            url: 'https://www.onlyspoolz.com/portfolio/spool-elegoo-pla-basic/', photo, empty_weight_g: 233, width_mm: 66 }],
        } })
      }
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
  await page.getByRole('button', { name: 'Send message', exact: true }).click()

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
    { web_search: undefined, printer_id: 21, filament_id: 37, nozzle_mm: 0.4 },
  )

  await page.getByRole('textbox', { name: 'Your question' }).fill('What does my Elegoo spool weigh empty?')
  await page.getByRole('button', { name: 'Send' }).click()
  await page.getByText('Which spool matches yours?', { exact: true }).waitFor()
  const cards = page.getByRole('list', { name: 'Spool matches' })
  const basic = cards.getByRole('listitem').filter({ hasText: 'Elegoo PLA Basic 1 kg' })
  await basic.getByText('233 g', { exact: true }).waitFor()
  await basic.getByText('66 mm', { exact: true }).waitFor()
  await basic.getByText('58 mm', { exact: true }).waitFor()
  assert.equal(await basic.locator('img').getAttribute('src'), photo)
  assert.equal(await cards.getByRole('img', { name: 'Photo of Elegoo cardboard' }).getAttribute('src'), catalogPhoto)
  assert.equal(await basic.getByRole('link', { name: 'OnlySpoolz' }).getAttribute('target'), '_blank')
  assert.equal(await cards.getByText('Untrusted card').count(), 0)
  const choice = basic.getByRole('button', { name: 'This matches: Elegoo PLA Basic 1 kg' })
  await choice.click()
  assert.equal(await basic.locator('button').getAttribute('aria-pressed'), 'true')
  assert.equal(requests.length, 2, 'choosing a spool reference is a local review action')
  await page.setViewportSize({ width: 320, height: 780 })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 320)
  await page.reload()
  await page.locator('[data-chat-history] summary').click()
  await page.getByRole('button', { name: 'What do I have for an outdoor bracket?' }).click()
  assert.equal(await basic.locator('button').getAttribute('aria-pressed'), 'true')
  await basic.getByRole('button', { name: 'Selected: Elegoo PLA Basic 1 kg' }).click()
  assert.equal(await basic.locator('button').getAttribute('aria-pressed'), 'false')
  await choice.click()
  await page.getByRole('textbox', { name: 'Your question' }).fill('What is the tare of the one I selected?')
  await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/filabrain/chat'),
    page.getByRole('button', { name: 'Send' }).click(),
  ])
  assert.equal(requests.at(-1).selected_spool_profile_id, 'onlyspoolz:elegoo-basic')
  assert.equal(requests.at(-1).spool_lookup_filament_id, 37)
  assert.equal(requests.at(-1).filament_id, undefined)
  assert.ok(requests.at(-1).messages.every(message => Object.keys(message).length === 2))
  await page.getByText('The selected spool weighs 233 g.', { exact: true }).waitFor()

  for (let index = 0; index < 6; index++) {
    await page.getByRole('textbox', { name: 'Your question' }).fill('How wide is it?')
    await page.getByRole('button', { name: 'Send' }).click()
    await page.getByText('The selected spool is 66 mm wide.', { exact: true }).nth(index).waitFor()
    assert.equal(requests.at(-1).selected_spool_profile_id, 'onlyspoolz:elegoo-basic')
    assert.ok(requests.at(-1).messages.some(message => message.content === 'What does my Elegoo spool weigh empty?'))
    assert.equal(requests.at(-1).messages.at(-2).confirmed_spool_profile_id, 'onlyspoolz:elegoo-basic')
  }
  await page.getByRole('button', { name: 'Selected: Elegoo PLA Basic 1 kg' }).nth(5).click()
  await page.getByRole('button', { name: 'This matches: Elegoo PLA Basic 1 kg' }).click()
  await page.reload()
  await page.locator('[data-chat-history] summary').click()
  await page.getByRole('button', { name: 'What do I have for an outdoor bracket?' }).click()
  await page.getByRole('textbox', { name: 'Your question' }).fill('How wide is it?')
  await page.getByRole('button', { name: 'Send' }).click()
  await page.getByText('The selected spool is 66 mm wide.', { exact: true }).nth(6).waitFor()
  const reselected = requests.at(-1)
  assert.equal(reselected.messages[reselected.spool_lookup_message_index].content, 'Which spool matches yours?')
  assert.equal(reselected.messages[reselected.spool_lookup_message_index - 1].content, 'What does my Elegoo spool weigh empty?')
  assert.equal(reselected.spool_lookup_filament_id, 37)
  await page.getByText('Printer and filament', { exact: true }).click()
  await page.getByRole('searchbox', { name: 'Find a filament' }).fill('PETG HF')
  await page.locator('[data-chat-filament]').selectOption('38')
  await page.getByRole('button', { name: 'This matches: Elegoo Rapid PLA+' }).click()
  for (let index = 0; index < 2; index++) {
    await page.getByRole('textbox', { name: 'Your question' }).fill('How wide is it?')
    await page.getByRole('button', { name: 'Send' }).click()
    await page.getByText('The newly selected spool is 56 mm wide.', { exact: true }).nth(index).waitFor()
    const payload = requests.at(-1)
    assert.equal(payload.selected_spool_profile_id, 'onlyspoolz:elegoo-rapid')
    assert.equal(payload.spool_lookup_filament_id, 37)
    assert.equal(payload.filament_id, 38)
    assert.equal(payload.messages[payload.spool_lookup_message_index].content, 'Which spool matches yours?')
    assert.ok(payload.messages.length <= 11)
  }
  console.log('Grounded chat renders inventory, citations, search, and selectable spool matches.')
} finally {
  await browser.close()
}
