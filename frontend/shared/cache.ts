const CACHE_PREFIX = 'filaman-cache:'

export function invalidateCache(key: string): void {
  try {
    localStorage.removeItem(CACHE_PREFIX + key)
  } catch {
    // Ignorieren
  }
}

export const CACHE_KEYS = {
  FILAMENTS: 'filaments',
  LOCATIONS: 'locations',
  COLORS: 'colors',
  MANUFACTURERS: 'manufacturers',
  STATUSES: 'statuses',
  FILAMENT_TYPES: 'filament-types',
  FILAMENT_FILTER_OPTIONS: 'filament-filter-options',
  SPOOL_FILTER_OPTIONS: 'spool-filter-options',
} as const
