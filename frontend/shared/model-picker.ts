import { api } from './api'
import { t } from './i18n'

export function mountModelPicker(form: HTMLFormElement, defaultModel?: string | null) {
  const trigger = form.querySelector<HTMLButtonElement>('[data-model-trigger]')!
  const name = form.querySelector<HTMLElement>('[data-model-name]')!
  const menu = form.querySelector<HTMLElement>('[data-model-menu]')!
  const options = form.querySelector<HTMLElement>('[data-model-options]')!
  const status = form.querySelector<HTMLElement>('[data-model-picker-status]')!
  let busy = false
  let revision = 0
  let activeDefault = defaultModel
  let selected: string | undefined

  menu.id = `${form.id || 'filabrain-chat'}-model-menu`
  trigger.setAttribute('popovertarget', menu.id)
  trigger.setAttribute('aria-controls', menu.id)

  function updateSelection() {
    const model = selected || activeDefault || t('filaBrain.researchNoModel')
    name.textContent = model
    trigger.setAttribute('aria-label', `${t('filaBrain.modelForConversation')}, ${model}`)
    trigger.title = model
    for (const item of options.querySelectorAll<HTMLButtonElement>('button')) {
      item.setAttribute('aria-checked', String(item.value === (selected || '')))
    }
  }

  function addOption(model: string, value: string) {
    const item = document.createElement('button')
    item.type = 'button'
    item.value = value
    item.tabIndex = -1
    item.className = 'chat-model-option'
    item.setAttribute('role', 'menuitemradio')
    const label = document.createElement('span')
    label.className = 'chat-model-option-name'
    label.textContent = model
    item.append(label)
    if (!value) {
      const badge = document.createElement('span')
      badge.className = 'chat-model-default'
      badge.textContent = t('filaBrain.modelDefault')
      item.append(badge)
    }
    const check = document.createElement('span')
    check.className = 'chat-model-check'
    check.textContent = '✓'
    check.setAttribute('aria-hidden', 'true')
    item.append(check)
    item.addEventListener('click', () => {
      selected = value || undefined
      updateSelection()
      menu.hidePopover()
      trigger.focus()
    })
    options.append(item)
  }

  function updateDisabled() {
    trigger.disabled = busy || options.childElementCount < 2
    if (trigger.disabled && menu.matches(':popover-open')) menu.hidePopover()
  }

  function positionMenu() {
    const bounds = trigger.getBoundingClientRect()
    const above = bounds.top - 14
    const below = window.innerHeight - bounds.bottom - 14
    const openAbove = above >= Math.min(320, menu.scrollHeight) || above >= below
    menu.style.maxHeight = `${Math.max(0, Math.min(320, openAbove ? above : below))}px`
    const left = Math.max(8, Math.min(bounds.left, window.innerWidth - menu.offsetWidth - 8))
    menu.style.left = `${left}px`
    menu.style.top = `${openAbove ? bounds.top - menu.offsetHeight - 6 : bounds.bottom + 6}px`
  }

  menu.addEventListener('toggle', () => {
    const open = menu.matches(':popover-open')
    trigger.setAttribute('aria-expanded', String(open))
    if (!open) return
    positionMenu()
    options.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus()
  })
  window.addEventListener('resize', () => {
    if (menu.matches(':popover-open')) positionMenu()
  })
  menu.addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
    const items = [...options.querySelectorAll('button')]
    const index = items.indexOf(document.activeElement as HTMLButtonElement)
    const last = items.length - 1
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? last
      : event.key === 'ArrowDown' ? (index + 1) % items.length : index <= 0 ? last : index - 1
    event.preventDefault()
    items[next]?.focus()
  })

  async function load(model?: string | null) {
    const current = ++revision
    activeDefault = model
    selected = undefined
    options.replaceChildren()
    status.hidden = true
    updateSelection()
    updateDisabled()
    if (!model) return
    try {
      const { models } = await api.get<{ models: string[] }>('/filabrain/models', { cache: 'no-store' })
      if (current !== revision) return
      addOption(model, '')
      for (const id of models) if (id !== model) addOption(id, id)
      updateSelection()
      updateDisabled()
    } catch {
      if (current !== revision) return
      status.textContent = t('filaBrain.otherModelsUnavailable')
      status.hidden = false
    }
  }

  void load(defaultModel)
  return {
    get selected() { return selected },
    reset() {
      selected = undefined
      updateSelection()
    },
    setDefault: load,
    setBusy(value: boolean) {
      busy = value
      updateDisabled()
    },
  }
}
