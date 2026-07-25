// User settings (entry/league IDs) + Worker API base resolution.
// Live per-user data goes through the Cloudflare proxy; the base resolves from
// (1) ?api= query param, (2) window.__GAFFER_API__, (3) localStorage, (4) none.

const LS = {
  entry: 'gaffer.entryId',
  leagues: 'gaffer.leagueIds',
  api: 'gaffer.apiBase',
  theme: 'gaffer.theme',
}

// localStorage throws in private mode / when storage is disabled. These wrappers
// degrade to "no persisted setting" instead of crashing the whole app on mount.
const safeLS = {
  get(key: string): string | null {
    try {
      return localStorage.getItem(key)
    } catch {
      return null
    }
  },
  set(key: string, val: string) {
    try {
      localStorage.setItem(key, val)
    } catch {
      /* ignore */
    }
  },
  remove(key: string) {
    try {
      localStorage.removeItem(key)
    } catch {
      /* ignore */
    }
  },
}

declare global {
  interface Window {
    __GAFFER_API__?: string
  }
}

export function apiBase(): string | null {
  try {
    const q = new URLSearchParams(location.search).get('api')
    if (q) safeLS.set(LS.api, q)
  } catch {
    /* ignore */
  }
  if (typeof window !== 'undefined' && window.__GAFFER_API__) return window.__GAFFER_API__
  return safeLS.get(LS.api)
}

export function setApiBase(v: string) {
  safeLS.set(LS.api, v.replace(/\/$/, ''))
}

/** Extract a numeric id from a raw value or a pasted FPL URL. */
export function parseId(raw: string): number | null {
  const m = raw.match(/\d{2,}/)
  return m ? Number(m[0]) : null
}

export function getEntryId(): number | null {
  const v = safeLS.get(LS.entry)
  return v ? Number(v) : null
}
export function setEntryId(v: number | null) {
  if (v) safeLS.set(LS.entry, String(v))
  else safeLS.remove(LS.entry)
}

export function getLeagueIds(): number[] {
  const v = safeLS.get(LS.leagues)
  return v ? v.split(',').map(Number).filter(Boolean) : []
}
export function setLeagueIds(ids: number[]) {
  safeLS.set(LS.leagues, ids.join(','))
}

export function getTheme(): 'dark' | 'light' {
  return (safeLS.get(LS.theme) as 'dark' | 'light') || 'dark'
}
export function setTheme(t: 'dark' | 'light') {
  safeLS.set(LS.theme, t)
  document.documentElement.setAttribute('data-theme', t)
}
