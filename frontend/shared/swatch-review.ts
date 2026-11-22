import { api, ApiError } from './api'
import { CACHE_KEYS, invalidateCache } from './cache'
import { escapeHtml as escape } from './extra-fields'
import { t } from './i18n'

export type Swatch = { id: number; name: string; hex_code: string }
export type Reference = {
  code: string
  name: string
  hex_code: string | null
  manufacturer_source: string | null
  from_product_url?: boolean
  source: string | null
  saved: boolean
  matches: Swatch[]
  token: string
}
export type Review = { current: Swatch[]; can_save: boolean; can_assign: boolean; can_create: boolean; references: Reference[] }
const tr = (key: string) => t('swatchReview.' + key)
const label = (key: string) => escape(tr(key))

function chip(name: string, hex: string) {
  const color = /^#[0-9a-f]{6}(?:[0-9a-f]{2})?$/i.test(hex) ? hex : 'transparent'
  return `<div class="swatch-review-chip">
    <span class="swatch-review-color" style="background-color:${color}" aria-hidden="true"></span>
    <span><strong>${escape(name)}</strong><code>${escape(hex)}</code></span>
  </div>`
}

function source(url: string | null, key: string) {
  return url && /^https?:\/\//i.test(url)
    ? `<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${label(key)}</a>` : ''
}

type DraftSwatches = {
  load: () => Promise<Review>
  choose: (reference: Reference, action: string, swatch?: Swatch) => Promise<void>
}

export async function mountSwatchReview(panel: HTMLElement, path: string | DraftSwatches) {
  const content = panel.querySelector<HTMLElement>('[data-swatch-content]')!
  const status = panel.querySelector<HTMLElement>('[data-swatch-status]')!

  async function load() {
    const review = typeof path === 'string' ? await api.get<Review>(path, { cache: 'no-store' }) : await path.load()
    if (!panel.isConnected) return
    const current = review.current
    content.innerHTML = `
      <div class="swatch-compare">
        <div class="swatch-current">
          <p class="swatch-label">${label('current')}</p>
          ${current.length ? current.map(swatch => chip(swatch.name, swatch.hex_code)).join('') : `<p>${label('noCurrent')}</p>`}
        </div>
        <div class="swatch-matches">
          ${review.references.map(reference => `<div class="swatch-preview">
            <p class="swatch-label">${label('reference')}${reference.saved ? ' · ' + label('savedTag') : ''}</p>
            ${reference.hex_code ? chip(reference.code === reference.name ? reference.code : reference.name + ' · ' + reference.code, reference.hex_code)
              : `<p><strong>${escape(reference.code)}</strong> · ${label('unavailable')}</p>`}
            <div class="swatch-sources">${source(reference.source, 'paletteSource')}${source(reference.manufacturer_source, reference.from_product_url ? 'manufacturerProductUrl' : 'manufacturerSource')}</div>
          </div>`).join('')}
        </div>
      </div>
      ${review.can_save ? review.references.map((reference, index) => {
        const canAssign = review.can_assign && !!reference.hex_code
        const canCreate = canAssign && review.can_create && !current.some(swatch => swatch.hex_code.toUpperCase() === reference.hex_code)
        const options = canAssign ? reference.matches.map(swatch =>
          `<option value="reuse:${swatch.id}">${label('reuse')} · ${escape(swatch.name)}</option>`).join('') : ''
        const control = 'swatch-choice-' + index
        const multiple = review.references.length > 1
        return `<form class="swatch-actions" data-swatch-index="${index}">
          <label class="${multiple ? '' : 'research-sr-only'}" for="${control}">${multiple ? escape(reference.code) : label('choice')}</label>
          <select id="${control}" name="swatch_choice" class="fm-input">
            <option value="keep">${label(typeof path !== 'string' ? 'noChange' : current.length ? 'keep' : 'referenceOnly')}</option>
            ${options}
            ${canCreate ? `<option value="create">${label('create')}</option>` : ''}
          </select>
          <button type="submit" class="fm-btn fm-btn-outline">${label(typeof path === 'string' ? 'saveReference' : 'useSwatch')}</button>
        </form>`
      }).join('') : ''}`
    content.querySelectorAll<HTMLFormElement>('form').forEach(form => {
      const reference = review.references[Number(form.dataset.swatchIndex)]
      const select = form.querySelector('select')!
      const button = form.querySelector('button')!
      if (!select || !button) return
      select.addEventListener('change', () => {
        button.textContent = tr(typeof path !== 'string' ? 'useSwatch' : select.value === 'keep' ? 'saveReference' : 'applySwatch')
      })
      form.addEventListener('submit', async event => {
        event.preventDefault()
        event.stopPropagation()
        if (content.inert) return
        const [action, id] = select.value.split(':')
        const match = reference.matches.find(swatch => swatch.id === Number(id))
        content.inert = true
        status.textContent = ''
        try {
          if (typeof path === 'string') {
            await api.post(path, {
              code: reference.code, token: reference.token, action, color_id: match?.id, color_name: match?.name,
            })
            invalidateCache(CACHE_KEYS.FILAMENTS)
            invalidateCache(CACHE_KEYS.FILAMENT_FILTER_OPTIONS)
            status.textContent = tr('saved')
            await load()
          } else {
            await path.choose(reference, action, match)
            status.textContent = tr(action === 'keep' ? 'unchanged' : 'draftSelected')
          }
        } catch (error) {
          status.textContent = error instanceof Error ? error.message : tr('saveFailed')
        } finally {
          content.inert = false
          if (panel.isConnected && !form.isConnected) status.focus()
        }
      })
    })
  }

  try {
    await load()
  } catch (error) {
    status.textContent = error instanceof ApiError && error.status === 409 ? error.message : tr('loadFailed')
  }
}
