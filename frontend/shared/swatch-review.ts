import { api, ApiError } from './api'
import { CACHE_KEYS, invalidateCache } from './cache'
import { escapeHtml as escape } from './extra-fields'
import { t } from './i18n'

export type Swatch = { id: number; name: string; hex_code: string }
type NameChange = { color_id: number; current_name: string; proposed_name: string; usage_count: number; token: string }
export type Reference = {
  code: string
  name: string
  canonical_name?: string
  hex_code: string | null
  manufacturer_source: string | null
  from_product_url?: boolean
  saved_reference?: boolean
  source: string | null
  saved: boolean
  matches: Swatch[]
  token: string
  name_changes?: NameChange[]
  rename_conflicts?: string[]
}
export type Review = { current: Swatch[]; can_save: boolean; can_assign: boolean; can_create: boolean; can_update?: boolean; references: Reference[] }
const tr = (key: string, vars?: Record<string, string | number>) => t('swatchReview.' + key, vars)
const label = (key: string) => escape(tr(key))

export function swatchChip(name: string, hex: string) {
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

  function confirmRename(change: NameChange) {
    const dialog = document.createElement('dialog')
    dialog.className = 'swatch-rename-dialog'
    dialog.setAttribute('aria-label', tr('rename'))
    dialog.innerHTML = `<h2>${label('rename')}</h2>
      <p class="swatch-name-change"><span>${escape(change.current_name)}</span> <span aria-hidden="true">→</span> <strong>${escape(change.proposed_name)}</strong></p>
      <p>${escape(tr('renameUsage', { count: change.usage_count }))}</p>
      <p>${label('renameScope')}</p>
      <div class="fm-modal-actions"><button type="button" class="fm-btn fm-btn-outline" data-cancel>${escape(t('common.cancel'))}</button>
      <button type="button" class="fm-btn fm-btn-primary" data-confirm>${label('renameConfirm')}</button></div>`
    panel.append(dialog)
    return new Promise<boolean>(resolve => {
      dialog.querySelector('[data-cancel]')!.addEventListener('click', () => dialog.close())
      dialog.querySelector('[data-confirm]')!.addEventListener('click', () => dialog.close('confirm'))
      dialog.addEventListener('close', () => {
        const confirmed = dialog.returnValue === 'confirm'
        dialog.remove()
        resolve(confirmed)
      }, { once: true })
      dialog.showModal()
      dialog.querySelector<HTMLButtonElement>('[data-cancel]')!.focus()
    })
  }

  async function load() {
    const review = typeof path === 'string' ? await api.get<Review>(path, { cache: 'no-store' }) : await path.load()
    if (!panel.isConnected) return
    const current = review.current
    content.innerHTML = `
      <div class="swatch-compare">
        <div class="swatch-current">
          <p class="swatch-label">${label('current')}</p>
          ${current.length ? current.map(swatch => swatchChip(swatch.name, swatch.hex_code)).join('') : `<p>${label('noCurrent')}</p>`}
        </div>
        <div class="swatch-matches">
          ${review.references.map((reference, index) => `<div class="swatch-preview">
            <p class="swatch-label">${label('reference')}${reference.saved ? ' · ' + label('savedTag') : ''}</p>
            ${reference.hex_code ? swatchChip(reference.code === reference.name ? reference.code : reference.name + ' · ' + reference.code, reference.hex_code)
              : `<p><strong>${escape(reference.code)}</strong> · ${label('unavailable')}</p>`}
            <div class="swatch-sources">${source(reference.source, 'paletteSource')}${reference.saved_reference ? `<span>${label('savedReference')}</span>` : source(reference.manufacturer_source, reference.from_product_url ? 'manufacturerProductUrl' : 'manufacturerSource')}</div>
            ${reference.rename_conflicts?.length ? `<p class="swatch-rename-conflict">${escape(tr('renameConflicts', { codes: reference.rename_conflicts.join(', ') }))}</p>` : ''}
            ${typeof path === 'string' && review.can_update ? (reference.name_changes || []).map((change, changeIndex) => `
              <div class="swatch-rename">
                <p class="swatch-name-change"><span>${escape(change.current_name)}</span> <span aria-hidden="true">→</span> <strong>${escape(change.proposed_name)}</strong></p>
                <p>${escape(tr('renameUsage', { count: change.usage_count }))}</p>
                <p>${label('renameScope')}</p>
                <button type="button" class="fm-btn fm-btn-outline" data-rename-reference="${index}" data-rename-change="${changeIndex}">${label('rename')}</button>
              </div>`).join('') : ''}
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
        const emptyDraft = typeof path !== 'string' && !current.length
        return `<form class="swatch-actions" data-swatch-index="${index}">
          <label class="${multiple ? '' : 'research-sr-only'}" for="${control}">${multiple ? escape(reference.code) : label('choice')}</label>
          <select id="${control}" name="swatch_choice" class="fm-input">
            ${emptyDraft ? `<option value="" disabled selected>${label('choose')}</option>` : ''}
            <option value="keep">${label(emptyDraft ? 'noColor' : typeof path !== 'string' ? 'noChange' : current.length ? 'keep' : 'referenceOnly')}</option>
            ${options}
            ${canCreate ? `<option value="create">${label('create')}</option>` : ''}
          </select>
          <button type="submit" class="fm-btn fm-btn-outline" ${emptyDraft ? 'disabled' : ''}>${label(typeof path === 'string' ? 'saveReference' : 'useSwatch')}</button>
        </form>`
      }).join('') : ''}`
    content.querySelectorAll<HTMLButtonElement>('[data-rename-reference]').forEach(button => {
      const reference = review.references[Number(button.dataset.renameReference)]
      const change = reference.name_changes![Number(button.dataset.renameChange)]
      button.addEventListener('click', async () => {
        if (typeof path !== 'string' || content.inert) return
        status.textContent = ''
        if (!await confirmRename(change) || !panel.isConnected || !button.isConnected || content.inert) return
        content.inert = true
        try {
          await api.post(path, { code: reference.code, token: change.token, action: 'rename', color_id: change.color_id, color_name: change.current_name })
          for (const key of [CACHE_KEYS.COLORS, CACHE_KEYS.FILAMENTS, CACHE_KEYS.FILAMENT_FILTER_OPTIONS, CACHE_KEYS.SPOOL_FILTER_OPTIONS]) invalidateCache(key)
          // The previous token must never be reused if refreshing the review fails.
          content.replaceChildren()
          await load()
          status.textContent = tr('renamed')
        } catch (error) {
          status.textContent = error instanceof Error ? error.message : tr('saveFailed')
          if (error instanceof ApiError && [403, 409].includes(error.status)) {
            content.replaceChildren()
            try { await load() } catch { status.textContent += ' ' + tr('loadFailed') }
          }
        } finally {
          content.inert = false
          if (panel.isConnected) status.focus()
        }
      })
    })
    content.querySelectorAll<HTMLFormElement>('form').forEach(form => {
      const reference = review.references[Number(form.dataset.swatchIndex)]
      const select = form.querySelector('select')!
      const button = form.querySelector('button')!
      if (!select || !button) return
      select.addEventListener('change', () => {
        button.disabled = !select.value
        button.textContent = tr(typeof path !== 'string'
          ? select.value === 'keep' && !current.length ? 'continueWithoutColor' : 'useSwatch'
          : select.value === 'keep' ? 'saveReference' : 'applySwatch')
      })
      form.addEventListener('submit', async event => {
        event.preventDefault()
        event.stopPropagation()
        if (content.inert || !select.value) return
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
            status.textContent = tr(action === 'keep' ? current.length ? 'unchanged' : 'withoutColor' : 'draftSelected')
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
