import { api } from './shared/api'
import { t } from './shared/i18n'

type Candidate = {
  id: number
  label: string
  key: string
  target_type: 'filament' | 'spool'
}
type Preview = Candidate & {
  native_field: string
  rows: { id: number; custom: unknown; native: unknown; action: 'copy' | 'same' | 'conflict' | 'discardEmpty' }[]
  blockers: string[]
  can_apply: boolean
  fingerprint: string
}

const path = '/filabrain/extra-field-cleanup'
const label = (key: string, vars?: Record<string, string | number>) => t('extraFieldCleanup.' + key, vars)
const value = (input: unknown) => input == null ? label('empty') : JSON.stringify(input)

export function mountExtraFieldCleanup(panel: HTMLElement) {
  const select = panel.querySelector<HTMLSelectElement>('[data-cleanup-field]')!
  const previewButton = panel.querySelector<HTMLButtonElement>('[data-cleanup-preview]')!
  const apply = panel.querySelector<HTMLButtonElement>('[data-cleanup-apply]')!
  const confirm = panel.querySelector<HTMLInputElement>('[data-cleanup-confirm]')!
  const review = panel.querySelector<HTMLElement>('[data-cleanup-review]')!
  const details = panel.querySelector<HTMLElement>('[data-cleanup-details]')!
  const status = panel.querySelector<HTMLElement>('[data-cleanup-status]')!
  const archives = panel.querySelector<HTMLDetailsElement>('[data-cleanup-archives]')!
  let preview: Preview | undefined
  let busy = false

  function updateControls() {
    select.disabled = busy || !select.options.length
    previewButton.disabled = busy || !select.value
    apply.hidden = review.hidden = !preview?.can_apply
    apply.disabled = busy || !confirm.checked || !preview?.can_apply
  }

  function clear() {
    preview = undefined
    confirm.checked = false
    details.replaceChildren()
    details.hidden = true
    updateControls()
  }

  function show(result: Preview) {
    preview = result
    details.hidden = false
    const destination = document.createElement('p')
    destination.textContent = label('destination', { field: result.native_field || label('empty') })
    details.append(destination)
    for (const blocker of result.blockers) {
      const notice = document.createElement('p')
      notice.textContent = label(blocker)
      details.append(notice)
    }
    if (result.rows.length) {
      const list = document.createElement('ul')
      for (const row of result.rows) {
        const item = document.createElement('li')
        const record = document.createElement('a')
        record.href = `/${result.target_type === 'spool' ? 'spools' : 'filaments'}/${row.id}/edit`
        record.target = '_blank'
        record.rel = 'noopener noreferrer'
        record.textContent = '#' + row.id
        item.append(record, ' ', label(row.action, { custom: value(row.custom), native: value(row.native) }))
        list.append(item)
      }
      details.append(list)
    }
    if (result.can_apply) {
      const note = document.createElement('p')
      note.textContent = label('retainsValues')
      details.append(note)
    }
  }

  async function load(notice = '') {
    busy = true
    clear()
    status.textContent = notice || label('loading')
    try {
      const result = await api.get<{ items: Candidate[]; archives: (Pick<Candidate, 'id' | 'target_type' | 'label'> & { created_at: string })[] }>(path, { cache: 'no-store' })
      select.replaceChildren(...result.items.map(field =>
        new Option(`${label(field.target_type)} · ${field.label} (${field.key})`, String(field.id))))
      archives.hidden = !result.archives.length
      archives.querySelector('ul')!.replaceChildren(...result.archives.map(archive => {
        const item = document.createElement('li')
        const link = document.createElement('a')
        link.href = `/api/v1${path}/archives/${archive.id}`
        link.download = ''
        link.textContent = `${label(archive.target_type)} · ${archive.label} · ${new Date(archive.created_at).toLocaleString()}`
        item.append(link)
        return item
      }))
      if (!result.items.length) status.textContent = notice || label('noMatches')
      else status.textContent = notice
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : label('failed')
    } finally {
      busy = false
      updateControls()
    }
  }

  select.addEventListener('change', clear)
  confirm.addEventListener('change', updateControls)
  previewButton.addEventListener('click', async () => {
    busy = true
    clear()
    status.textContent = label('loading')
    try {
      show(await api.get<Preview>(`${path}/${select.value}`, { cache: 'no-store' }))
      status.textContent = ''
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : label('failed')
    } finally {
      busy = false
      updateControls()
    }
  })
  apply.addEventListener('click', async () => {
    if (!preview?.can_apply || busy || !confirm.checked) return
    busy = true
    updateControls()
    try {
      const result = await api.post<{ copied_values: number }>(`${path}/${preview.id}`, { fingerprint: preview.fingerprint })
      await load(label('done', { count: result.copied_values }))
    } catch (error) {
      busy = false
      clear()
      status.textContent = error instanceof Error ? error.message : label('failed')
    }
  })
  panel.hidden = false
  void load()
}
