import { getLang, t } from './shared/i18n'

export type ChatUsage = { prompt_tokens: number; completion_tokens?: number; context_window?: number }

let nextId = 0

export function mountContextUsage(host: HTMLElement) {
  host.classList.add('chat-context-usage')
  host.innerHTML = `<button type="button" class="chat-context-usage-trigger" aria-expanded="false">
    <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
      <circle class="chat-context-usage-track" cx="12" cy="12" r="9" />
      <circle class="chat-context-usage-arc" cx="12" cy="12" r="9" pathLength="100" />
    </svg></button>
    <div class="chat-context-usage-popover" popover><h3></h3><p data-usage-summary></p>
      <p data-usage-model></p><p data-usage-breakdown></p><p data-usage-hint></p></div>`
  const trigger = host.querySelector('button')!
  const menu = host.querySelector<HTMLElement>('[popover]')!
  const arc = host.querySelector<SVGCircleElement>('.chat-context-usage-arc')!
  const summary = menu.querySelector<HTMLElement>('[data-usage-summary]')!
  const model = menu.querySelector<HTMLElement>('[data-usage-model]')!
  const breakdown = menu.querySelector<HTMLElement>('[data-usage-breakdown]')!
  const hint = menu.querySelector<HTMLElement>('[data-usage-hint]')!
  menu.id = `chat-context-usage-${++nextId}`
  trigger.setAttribute('popovertarget', menu.id)
  trigger.setAttribute('aria-controls', menu.id)
  menu.setAttribute('aria-labelledby', `${menu.id}-title`)
  menu.querySelector('h3')!.id = `${menu.id}-title`
  host.hidden = true

  function positionMenu() {
    const bounds = trigger.getBoundingClientRect()
    const above = Math.max(0, bounds.top - 14)
    const below = Math.max(0, window.innerHeight - bounds.bottom - 14)
    const openAbove = above >= Math.min(260, menu.scrollHeight) || above >= below
    menu.style.maxHeight = `${openAbove ? above : below}px`
    menu.style.left = `${Math.max(8, Math.min(bounds.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8))}px`
    menu.style.top = `${Math.max(8, Math.min(openAbove ? bounds.top - menu.offsetHeight - 6 : bounds.bottom + 6, window.innerHeight - menu.offsetHeight - 8))}px`
  }

  menu.addEventListener('toggle', () => {
    const open = menu.matches(':popover-open')
    trigger.setAttribute('aria-expanded', String(open))
    if (open) positionMenu()
  })
  window.addEventListener('resize', () => {
    if (menu.matches(':popover-open')) positionMenu()
  })
  window.addEventListener('scroll', () => {
    if (menu.matches(':popover-open')) positionMenu()
  }, true)

  return {
    update(usage: ChatUsage | undefined, options: { visible: boolean; model?: string }) {
      if (menu.matches(':popover-open')) menu.hidePopover()
      trigger.setAttribute('aria-expanded', 'false')
      host.hidden = !options.visible
      const validCount = (value: number | undefined): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0
      const reported = validCount(usage?.prompt_tokens)
      const completion = validCount(usage?.completion_tokens) ? usage.completion_tokens : undefined
      const count = reported ? usage!.prompt_tokens + (completion ?? 0) : 0
      const limit = reported && validCount(usage?.context_window) && usage.context_window > 0 ? usage.context_window : undefined
      const percent = limit ? count / limit * 100 : undefined
      const number = new Intl.NumberFormat(getLang(), { maximumFractionDigits: 1 })
      summary.textContent = percent !== undefined
        ? t('filaBrain.contextPercent', { percent: number.format(percent), count: number.format(count), limit: number.format(limit!) })
        : reported ? t('filaBrain.contextTokens', { count: number.format(count) }) : t('filaBrain.contextUsageUnavailable')
      menu.querySelector('h3')!.textContent = t('filaBrain.contextUsageDetails')
      model.textContent = options.model ? t('filaBrain.contextUsageModel', { model: options.model }) : ''
      model.hidden = !options.model
      breakdown.textContent = reported ? t('filaBrain.contextUsageBreakdown', {
        prompt: number.format(usage!.prompt_tokens), completion: completion === undefined ? '—' : number.format(completion),
      }) : ''
      breakdown.hidden = !reported
      hint.textContent = reported ? t(limit ? 'filaBrain.contextUsageHint' : 'filaBrain.contextUsageUnknown') : ''
      hint.hidden = !reported
      trigger.setAttribute('aria-label', summary.textContent)
      trigger.title = summary.textContent
      trigger.dataset.known = String(percent !== undefined)
      arc.style.strokeDashoffset = String(100 - Math.min(100, Math.max(0, percent ?? 0)))
    },
  }
}
