import { getLang, t } from './i18n'

export type SpoolProfile = {
  id: string; title: string; source: string; url: string; photo?: string
  empty_weight_g?: number; outer_diameter_mm?: number; width_mm?: number
  hole_diameter_mm?: number; net_weight_g?: number; package_weights_g?: number[]
}

const spoolMeasures = [
  ['empty_weight_g', 'chatSpoolEmpty', 'g'], ['outer_diameter_mm', 'chatSpoolOutside', 'mm'],
  ['width_mm', 'chatSpoolWidth', 'mm'], ['hole_diameter_mm', 'chatSpoolHole', 'mm'],
  ['net_weight_g', 'chatSpoolFilament', 'g'],
] as const
const localImage = '/api/v1/filamentdb/spool-profile-image'

export function spoolSource(value: unknown) {
  if (typeof value !== 'string' || /[\s\\]/.test(value)) return null
  try {
    if (value.startsWith(`${localImage}?`)) {
      const url = new URL(value, location.origin)
      const file = url.searchParams.get('image_file') || ''
      return url.pathname === localImage && url.searchParams.size === 1 && !url.hash && !file.includes('..')
        && /^[a-z0-9][a-z0-9_.-]{0,494}\.(?:jpe?g|png|webp)$/i.test(file) ? url : null
    }
    const url = new URL(value)
    if (url.protocol !== 'https:' || url.username || url.password) return null
    if (url.host === 'db.filaman.app') return /^\/api\/v1\/spool-profiles\/[1-9][0-9]*$/.test(url.pathname) ? url : null
    return ['onlyspoolz.com', 'www.onlyspoolz.com', 'theemptyspool.cc', 'www.theemptyspool.cc'].includes(url.host) ? url : null
  } catch { return null }
}

export function spoolProfileCards(
  profiles: SpoolProfile[], selectedId: string | undefined, choose: (profile?: SpoolProfile) => void,
) {
  const picker = document.createElement('div')
  picker.className = 'chat-spool-picker'
  const number = new Intl.NumberFormat(getLang(), { maximumFractionDigits: 2 })
  const list = document.createElement('ul')
  list.className = 'chat-spool-profiles'
  list.setAttribute('aria-label', t('filaBrain.chatSpoolMatches'))
  const selection = document.createElement('p')
  selection.className = 'chat-spool-selection'
  selection.setAttribute('role', 'status')
  const choices: { profile: SpoolProfile; button: HTMLButtonElement }[] = []

  function showChoice() {
    selection.textContent = ''
    for (const { profile, button } of choices) {
      const selected = selectedId === profile.id
      const label = t(selected ? 'filaBrain.chatSpoolSelected' : 'filaBrain.chatSpoolChoose')
      button.textContent = label
      button.setAttribute('aria-pressed', String(selected))
      button.setAttribute('aria-label', `${label}: ${profile.title}`)
      if (selected) selection.textContent = t('filaBrain.chatSpoolReview', { name: profile.title })
    }
  }

  for (const profile of profiles.slice(0, 4)) {
    if (!profile || typeof profile.id !== 'string' || typeof profile.title !== 'string' || typeof profile.source !== 'string') continue
    const source = spoolSource(profile.url)
    if (!source) continue
    const card = document.createElement('li')
    const photo = spoolSource(profile.photo)
    const versionedPhoto = photo?.hostname === 'theemptyspool.cc' && /^\?v=[a-f0-9]{8,40}$/i.test(photo.search)
    const localPhoto = photo?.origin === location.origin && photo?.pathname === localImage
    if (photo && (localPhoto || ((!photo.search || versionedPhoto) && !photo.hash && /\.(?:jpe?g|png|webp)$/i.test(photo.pathname)))) {
      const image = document.createElement('img')
      image.className = 'chat-spool-photo'
      image.alt = t('filaBrain.chatSpoolPhoto', { name: profile.title })
      image.referrerPolicy = 'no-referrer'
      image.loading = 'lazy'
      image.addEventListener('error', () => image.remove(), { once: true })
      image.src = photo.href
      card.append(image)
    }
    const body = document.createElement('div')
    body.className = 'chat-spool-card-body'
    const heading = document.createElement('strong')
    heading.textContent = profile.title
    const measures = document.createElement('dl')
    for (const [key, label, unit] of spoolMeasures) {
      const values = key === 'net_weight_g' && Array.isArray(profile.package_weights_g)
        ? profile.package_weights_g : [profile[key]]
      const formatted = values.flatMap(value => typeof value === 'number' && Number.isFinite(value) && value > 0
        ? [`${number.format(value)} ${unit}`] : [])
      if (!formatted.length) continue
      const term = document.createElement('dt')
      term.textContent = t(`filaBrain.${label}`)
      const detail = document.createElement('dd')
      detail.textContent = formatted.join(', ')
      measures.append(term, detail)
    }
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'fm-btn fm-btn-outline'
    button.addEventListener('click', () => {
      const selected = selectedId === profile.id ? undefined : profile
      selectedId = selected?.id
      showChoice()
      choose(selected)
    })
    choices.push({ profile, button })
    const credit = document.createElement('a')
    credit.textContent = profile.source
    credit.href = source.href
    credit.target = '_blank'
    credit.rel = 'noopener noreferrer'
    body.append(heading, measures, credit, button)
    card.append(body)
    list.append(card)
  }
  if (!choices.length) return
  showChoice()
  picker.append(list, selection)
  return picker
}
