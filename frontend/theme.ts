const hostTokens = [
  '--bg', '--bg-elevated', '--bg-soft', '--input-bg',
  '--text', '--text-muted', '--border', '--accent', '--accent-2', '--accent-3',
  '--btn-primary-text', '--warning-text', '--font-sans', '--font-serif', '--radius-sm',
]

export function mountTheme() {
  const root = document.documentElement
  let host: HTMLElement | undefined
  try {
    if (window.parent !== window) host = window.parent.document.documentElement
  } catch {
    // Direct or cross-origin embedding uses the saved FilaMan theme.
  }

  function sync() {
    let saved = 'brand'
    try { saved = localStorage.getItem('theme') || saved }
    catch { /* A blocked storage area still has a usable default theme. */ }
    root.dataset.theme = host?.dataset.theme || saved
    if (!host) return
    const style = window.parent.getComputedStyle(host)
    for (const token of hostTokens) {
      const value = style.getPropertyValue(token).trim()
      if (value) root.style.setProperty(token, value)
      else root.style.removeProperty(token)
    }
    if (style.colorScheme !== 'normal') root.style.colorScheme = style.colorScheme
    else root.style.removeProperty('color-scheme')
  }

  sync()
  if (host) {
    new MutationObserver(sync).observe(host, {
      attributes: true, attributeFilter: ['data-theme', 'class', 'style'],
    })
  }
  window.addEventListener('storage', event => {
    if (event.key === 'theme' || event.key === null) sync()
  })
  window.addEventListener('pageshow', sync)
}
