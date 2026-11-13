export function mountDocs() {
  const panel = document.querySelector<HTMLDialogElement>('#filabrain-docs')!
  const buttons = document.querySelectorAll<HTMLButtonElement>('[data-open-docs]')
  const compact = matchMedia('(max-width: 800px)')
  // Keep room for the settings form (52rem), guide (19rem), and page gutters (3.5rem).
  const compactSettings = matchMedia('(width < 74.5rem)')
  const topics = panel.querySelectorAll<HTMLElement>('[data-guide-topic]')
  const settings = panel.querySelectorAll<HTMLElement>('[data-guide-settings]')
  let opener: HTMLButtonElement | undefined

  function needsModal() {
    return compact.matches || (panel.dataset.guideTopic === 'settings' && compactSettings.matches)
  }

  function show() {
    if (needsModal()) panel.showModal()
    else panel.show()
    for (const button of buttons) button.setAttribute('aria-expanded', 'true')
  }

  for (const button of buttons) {
    button.hidden = false
    button.addEventListener('click', () => {
      if (panel.open) {
        panel.close()
        return
      }
      opener = button
      show()
    })
  }

  panel.addEventListener('close', () => {
    if (panel.open) return
    for (const button of buttons) button.setAttribute('aria-expanded', 'false')
    if (opener?.getClientRects().length) opener.focus()
  })
  panel.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !panel.matches(':modal')) panel.close()
  })
  function updateLayout() {
    if (!panel.open || panel.matches(':modal') === needsModal()) return
    panel.close()
    show()
  }
  compact.addEventListener('change', updateLayout)
  compactSettings.addEventListener('change', updateLayout)

  return (topic: string) => {
    for (const section of topics) section.hidden = section.dataset.guideTopic !== topic
    for (const section of settings) section.hidden = topic !== 'settings' &&
      !(topic === 'chat' && section === settings[0])
    panel.dataset.guideTopic = topic
    updateLayout()
    panel.scrollTop = 0
  }
}
