import { t } from './shared/i18n'
import { swatchChip, type Reference } from './shared/swatch-review'

type Fields = { name: string; hex_code: string }
type ColorContext = { mode: 'create' | 'edit'; color_id: number | null; usage_count: number; fields: Fields; capabilities?: { color_staging?: boolean } }
type Request = <T>(type: string, payload?: Record<string, unknown>, timeoutMs?: number) => Promise<T>
const tr = (key: string, vars?: Record<string, string | number>) => t('colorAssistant.' + key, vars)
const validFields = (value: Fields) => value && typeof value.name === 'string' && typeof value.hex_code === 'string' && /^#[0-9a-f]{6}(?:[0-9a-f]{2})?$/i.test(value.hex_code)

// Keep the native color bridge in one place; older hosts never mount this tool.
export async function connectColorHost(request: Request) {
  const context = await request<ColorContext>('filaman:color-context')
  if (!['create', 'edit'].includes(context.mode) || !validFields(context.fields) || context.capabilities?.color_staging !== true ||
      !Number.isSafeInteger(context.usage_count) || context.usage_count < 0) throw new Error(tr('unavailable'))
  return {
    context,
    async stage(fields: Fields) {
      const result = await request<{ staged: string[]; skipped: string[]; fields?: Fields }>('filaman:color-stage', { expected: { ...context.fields }, fields })
      if (!Array.isArray(result.staged) || !['name', 'hex_code'].every(key => result.staged.includes(key)) || !result.fields || !validFields(result.fields)) throw new Error(tr('changed'))
      context.fields = result.fields
    },
    async open(color_id: number) { await request('filaman:color-open', { color_id }, 0) },
  }
}
export type ColorHost = Awaited<ReturnType<typeof connectColorHost>>

export function colorContextNote(host: ColorHost) {
  const note = document.createElement('p')
  note.className = 'fm-field-hint color-context-note'
  note.textContent = [host.context.usage_count ? tr('usage', { count: host.context.usage_count }) : '', tr('scope')].filter(Boolean).join(' · ')
  return note
}

export function colorReview(references: Array<Omit<Reference, 'saved' | 'token'>>, host: ColorHost, busy: () => boolean, setBusy: (value: boolean) => void) {
  const panel = document.createElement('section')
  panel.className = 'color-assistant-review'
  const current = document.createElement('div')
  current.className = 'swatch-current'
  const label = document.createElement('p')
  label.className = 'swatch-label'
  label.textContent = tr('currentDraft')
  current.append(label)
  const chip = document.createElement('div')
  chip.innerHTML = swatchChip(host.context.fields.name || tr('newColor'), host.context.fields.hex_code)
  current.append(chip)
  panel.append(current)
  for (const reference of references) {
    const card = document.createElement('div')
    card.className = 'swatch-preview'
    const proposed = document.createElement('div')
    const name = reference.canonical_name || reference.code
    proposed.innerHTML = reference.hex_code ? swatchChip(name, reference.hex_code) : ''
    if (!reference.hex_code) proposed.textContent = reference.code + ' · ' + tr('unknown')
    card.append(proposed)
    const sources = document.createElement('div')
    sources.className = 'swatch-sources'
    for (const [url, key] of [[reference.source, 'paletteSource'], [reference.manufacturer_source, 'manufacturerSource']]) {
      if (!url || !/^https?:\/\//i.test(url)) continue
      const link = document.createElement('a')
      link.href = url
      link.target = '_blank'
      link.rel = 'noopener noreferrer'
      link.textContent = t('swatchReview.' + key)
      sources.append(link)
    }
    card.append(sources)
    const actions = document.createElement('div')
    actions.className = 'research-match-actions'
    const status = document.createElement('p')
    status.className = 'fm-field-hint'
    status.setAttribute('role', 'status')
    const use = document.createElement('button')
    use.type = 'button'
    use.className = 'fm-btn fm-btn-outline'
    use.textContent = tr('use')
    use.disabled = !reference.hex_code
    const run = async (action: () => Promise<void>, success = '') => {
      if (busy()) return
      setBusy(true)
      actions.inert = true
      status.textContent = ''
      try { await action(); status.textContent = success }
      catch (error) { status.textContent = error instanceof Error ? error.message : tr('changed') }
      finally { actions.inert = false; setBusy(false) }
    }
    use.addEventListener('click', () => {
      if (reference.hex_code) void run(async () => {
        await host.stage({ name, hex_code: reference.hex_code! })
        chip.innerHTML = swatchChip(host.context.fields.name, host.context.fields.hex_code)
      }, tr('staged'))
    })
    actions.append(use)
    for (const match of reference.matches.filter(match => match.id !== host.context.color_id)) {
      const open = document.createElement('button')
      open.type = 'button'
      open.className = 'fm-btn fm-btn-outline'
      open.textContent = tr('open', { name: match.name })
      open.addEventListener('click', () => { void run(() => host.open(match.id)) })
      actions.append(open)
    }
    card.append(actions, status)
    panel.append(card)
  }
  return panel
}
