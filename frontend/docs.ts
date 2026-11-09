export function mountDocs() {
  const panel = document.querySelector<HTMLDialogElement>('#filabrain-docs')!
  const buttons = document.querySelectorAll<HTMLButtonElement>('[data-open-docs]')
  const compact = matchMedia('(max-width: 800px)')
  let opener: HTMLButtonElement | undefined

  function show() {
    if (compact.matches) panel.showModal()
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
    if (event.key === 'Escape' && !compact.matches) panel.close()
  })
  compact.addEventListener('change', () => {
    if (!panel.open) return
    panel.close()
    show()
  })
}
