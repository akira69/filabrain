import { api } from './api'
import { t } from './i18n'

export function mountModelPicker(form: HTMLFormElement, defaultModel?: string | null) {
  const select = form.querySelector<HTMLSelectElement>('[data-model-picker]')!
  const status = form.querySelector<HTMLElement>('[data-model-picker-status]')!
  let busy = false
  let revision = 0

  function updateDisabled() {
    select.disabled = busy || select.options.length < 2
  }

  async function load(model?: string | null) {
    const current = ++revision
    select.replaceChildren(new Option(model || t('filaBrain.researchNoModel'), ''))
    status.hidden = true
    updateDisabled()
    if (!model) return
    try {
      const { models } = await api.get<{ models: string[] }>('/filabrain/models', { cache: 'no-store' })
      if (current !== revision) return
      select.append(...models.filter(id => id !== model).map(id => new Option(id, id)))
      updateDisabled()
    } catch {
      if (current !== revision) return
      status.textContent = t('filaBrain.otherModelsUnavailable')
      status.hidden = false
    }
  }

  void load(defaultModel)
  return {
    get selected() { return select.value || undefined },
    reset() { select.value = '' },
    setDefault: load,
    setBusy(value: boolean) {
      busy = value
      updateDisabled()
    },
  }
}
