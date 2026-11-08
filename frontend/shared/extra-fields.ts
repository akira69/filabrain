export function escapeHtml(s: string | null | undefined): string {
  if (s == null) return ''
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;')
}

function finiteNumber(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string' || value.trim() === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function formatNumericRange(value: unknown, decimalPlaces: number | null = null): string {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    const scalar = finiteNumber(value)
    return scalar === null ? '' : decimalPlaces === null ? String(scalar) : scalar.toFixed(decimalPlaces)
  }
  const range = value as Record<string, unknown>
  const min = finiteNumber(range.min)
  const max = finiteNumber(range.max)
  const format = (number: number) => (decimalPlaces === null ? String(number) : number.toFixed(decimalPlaces))
  if (min === null) return max === null ? '' : format(max)
  if (max === null || min === max) return format(min)
  return `${format(min)}–${format(max)}`
}
