import { api } from './api'
import { t } from './i18n'

export type ModelSelection = { model_id: string; endpoint_id?: string }
type ModelGroup = { id: string; name: string; models: string[]; unavailable?: boolean }

export function mountModelPicker(form: HTMLFormElement, defaultModel?: string | null, openSettings?: () => void,
  initialEndpoint?: string | null, onLoad?: () => void) {
  const trigger = form.querySelector<HTMLButtonElement>('[data-model-trigger]')!
  const name = form.querySelector<HTMLElement>('[data-model-name]')!
  const menu = form.querySelector<HTMLElement>('[data-model-menu]')!
  const options = form.querySelector<HTMLElement>('[data-model-options]')!
  const status = form.querySelector<HTMLElement>('[data-model-picker-status]')!
  const warning = status.querySelector('a')!
  if (openSettings) {
    warning.href = '?tab=settings'
    warning.addEventListener('click', event => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
      event.preventDefault()
      openSettings()
    })
  }
  let busy = false
  let revision = 0
  let activeDefault = defaultModel
  let selected: ModelSelection | undefined
  let needsSelection = false
  let groups: ModelGroup[] | undefined
  let defaultEndpoint = initialEndpoint ?? null

  menu.id = `${form.id || 'filabrain-chat'}-model-menu`
  trigger.setAttribute('popovertarget', menu.id)
  trigger.setAttribute('aria-controls', menu.id)

  function updateSelection() {
    const model = selected?.model_id || (!needsSelection && activeDefault)
      || t(needsSelection ? 'filaBrain.chooseModel' : 'filaBrain.researchNoModel')
    const endpoint = groups?.find(group => group.id === (selected?.endpoint_id ?? defaultEndpoint))
    const label = endpoint ? `${model} · ${endpoint.name}` : model
    name.textContent = model
    trigger.setAttribute('aria-label', `${t('filaBrain.modelForConversation')}, ${label}`)
    trigger.title = model
    for (const item of options.querySelectorAll<HTMLButtonElement>('button')) {
      const checked = selected ? (item.value || activeDefault) === selected.model_id && item.dataset.endpoint === (selected.endpoint_id ?? defaultEndpoint)
        : !needsSelection && !item.value
      item.setAttribute('aria-checked', String(checked))
    }
    if (groups) {
      const unavailable = groups.filter(group => group.unavailable).map(group => group.name)
      const stale = selected && !options.querySelector('[aria-checked="true"]')
      warning.textContent = stale ? t('filaBrain.modelSelectionUnavailable')
        : unavailable.length ? t('filaBrain.endpointModelsUnavailable', { names: unavailable.join(', ') }) : ''
      status.hidden = !warning.textContent
    }
  }

  function addOption(group: HTMLElement, endpointId: string, model: string, isDefault: boolean) {
    const item = document.createElement('button')
    item.type = 'button'
    item.value = isDefault ? '' : model
    item.dataset.endpoint = endpointId
    item.tabIndex = -1
    item.className = 'chat-model-option'
    item.setAttribute('role', 'menuitemradio')
    const label = document.createElement('span')
    label.className = 'chat-model-option-name'
    label.textContent = model
    item.append(label)
    if (isDefault) {
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
      selected = isDefault ? undefined : { model_id: model, endpoint_id: endpointId }
      needsSelection = false
      updateSelection()
      menu.hidePopover()
      trigger.focus()
      onLoad?.()
    })
    group.append(item)
  }

  function updateDisabled() {
    const optionsCount = options.querySelectorAll('button').length
    const selectionIsCurrent = (!needsSelection && !selected && !!activeDefault) || !!options.querySelector('[aria-checked="true"]')
    trigger.disabled = busy || optionsCount === 0 || (optionsCount === 1 && selectionIsCurrent)
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
    const first = options.querySelector<HTMLButtonElement>('[aria-checked="true"]') ?? options.querySelector('button')
    first?.focus()
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

  async function load(model?: string | null, endpoint?: string | null) {
    const current = ++revision
    activeDefault = model
    groups = undefined
    defaultEndpoint = endpoint ?? null
    options.replaceChildren()
    status.hidden = true
    updateSelection()
    updateDisabled()
    if (!model && !endpoint) return
    try {
      const result = await api.get<{ groups: ModelGroup[]; default_endpoint_id: string | null; default_model_id: string | null }>('/filabrain/models', { cache: 'no-store' })
      if (current !== revision) return
      groups = result.groups
      defaultEndpoint = result.default_endpoint_id
      activeDefault = result.default_model_id
      for (const endpoint of groups) {
        if (!endpoint.models.length) continue
        const group = document.createElement('div')
        group.setAttribute('role', 'group')
        group.setAttribute('aria-label', endpoint.name)
        const heading = document.createElement('div')
        heading.className = 'chat-model-menu-title'
        heading.textContent = endpoint.name
        heading.setAttribute('aria-hidden', 'true')
        group.append(heading)
        for (const id of endpoint.models) addOption(group, endpoint.id, id, endpoint.id === defaultEndpoint && id === activeDefault)
        options.append(group)
      }
      updateSelection()
      updateDisabled()
      onLoad?.()
    } catch {
      if (current !== revision) return
      warning.textContent = t('filaBrain.otherModelsUnavailable')
      status.hidden = false
    }
  }

  function reset(model?: string | ModelSelection, requireSelection = false) {
    // Older history entries with an explicit model refer to the migrated endpoint.
    selected = typeof model === 'string' ? { model_id: model, endpoint_id: 'default' } : model
    needsSelection = requireSelection && !selected
    updateSelection()
    updateDisabled()
  }

  void load(defaultModel, initialEndpoint)
  return {
    get selected() { return selected ?? (!needsSelection && activeDefault ? { model_id: activeDefault, endpoint_id: defaultEndpoint ?? undefined } : undefined) },
    get needsSelection() { return needsSelection },
    get hasOptions() { return !!activeDefault || !!options.querySelector('button') },
    reset,
    preserve(selection?: ModelSelection) { selected ??= selection },
    setDefault: load,
    setBusy(value: boolean) {
      busy = value
      updateDisabled()
    },
  }
}
