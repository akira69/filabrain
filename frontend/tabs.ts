export function mountWorkspaceTabs(header: HTMLElement, show: (tab: string) => void) {
  const nav = header.querySelector<HTMLElement>('nav')!
  const list = header.querySelector<HTMLElement>('[role=tablist]')!
  const row = header.querySelector<HTMLElement>('.workspace-tabs')!
  const buttons = [...list.querySelectorAll<HTMLButtonElement>('[data-tab]')]
  const chatHeaders = new Map<string, HTMLElement>()
  let active = ''
  let focusTab = buttons.find(button => !button.hidden)?.dataset.tab
  document.documentElement.dataset.workspaceTabs = ''

  for (const button of buttons) {
    const tab = button.dataset.tab!
    button.id = 'filabrain-tab-' + tab
    button.setAttribute('role', 'tab')
    button.setAttribute('aria-controls', 'filabrain-view-' + tab)
    button.setAttribute('aria-selected', 'false')
  }
  for (const panel of document.querySelectorAll<HTMLElement>('[data-view]')) {
    panel.id = 'filabrain-view-' + panel.dataset.view
    if (panel.dataset.view !== 'settings') {
      panel.setAttribute('role', 'tabpanel')
      panel.setAttribute('aria-labelledby', 'filabrain-tab-' + (panel.dataset.view === 'editor' ? 'new' : panel.dataset.view))
    }
  }
  for (const id of ['library-heading', 'spool-intake-heading', 'research-heading', 'batch-title']) {
    document.getElementById(id)?.parentElement?.classList.add('workspace-title-only')
  }
  list.addEventListener('keydown', event => {
    if (!(event.target instanceof HTMLElement) || event.target.getAttribute('role') !== 'tab') return
    const visible = buttons.filter(button => !button.hidden)
    const index = visible.indexOf(event.target as HTMLButtonElement)
    let next: HTMLButtonElement | undefined
    if (event.key === 'ArrowRight') next = visible[(index + 1) % visible.length]
    else if (event.key === 'ArrowLeft') next = visible[(index + visible.length - 1) % visible.length]
    else if (event.key === 'Home') next = visible[0]
    else if (event.key === 'End') next = visible.at(-1)
    if (!next) return
    event.preventDefault()
    show(next.dataset.tab!)
    next.focus({ preventScroll: true })
  })

  return {
    attachChat(tab: string, root: HTMLElement) {
      const controls = root.querySelector<HTMLElement>('.chat-header')!
      controls.querySelector('h2')!.classList.add('research-sr-only')
      controls.hidden = active !== tab
      controls.dataset.chatPage = tab
      row.append(controls)
      chatHeaders.set(tab, controls)
    },
    select(tab: string) {
      active = tab
      const navTab = tab === 'editor' ? 'new' : tab
      if (buttons.some(button => button.dataset.tab === navTab)) focusTab = navTab
      for (const button of buttons) {
        button.setAttribute('aria-selected', String(button.dataset.tab === navTab))
        button.tabIndex = button.dataset.tab === focusTab ? 0 : -1
        if (button.dataset.tab === 'new' && (tab === 'new' || tab === 'editor')) {
          button.setAttribute('aria-controls', 'filabrain-view-' + tab)
        }
      }
      for (const [key, controls] of chatHeaders) controls.hidden = key !== tab
      nav.hidden = !buttons.some(button => !button.hidden)
      requestAnimationFrame(() => buttons.find(button => button.dataset.tab === navTab)?.scrollIntoView({ block: 'nearest', inline: 'nearest' }))
    },
  }
}
