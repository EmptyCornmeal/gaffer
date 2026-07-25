// User settings (entry/league IDs) + Worker API base resolution.
// Live per-user data goes through the Cloudflare proxy; the base resolves from
// (1) ?api= query param, (2) window.__GAFFER_API__, (3) localStorage, (4) none.

const LS = {
  entry: 'gaffer.entryId',
  leagues: 'gaffer.leagueIds',
  api: 'gaffer.apiBase',
  theme: 'gaffer.theme',
}

declare global {
  interface Window {
    __GAFFER_API__?: string
  }
}

export function apiBase(): string | null {
  try {
    const q = new URLSearchParams(location.search).get('api')
    if (q) localStorage.setItem(LS.api, q)
  } catch {
    /* ignore */
  }
  if (typeof window !== 'undefined' && window.__GAFFER_API__) return window.__GAFFER_API__
  return localStorage.getItem(LS.api)
}

export function setApiBase(v: string) {
  localStorage.setItem(LS.api, v.replace(/\/$/, ''))
}

/** Extract a numeric id from a raw value or a pasted FPL URL. */
export function parseId(raw: string): number | null {
  const m = raw.match(/\d{2,}/)
  return m ? Number(m[0]) : null
}

export function getEntryId(): number | null {
  const v = localStorage.getItem(LS.entry)
  return v ? Number(v) : null
}
export function setEntryId(v: number | null) {
  if (v) localStorage.setItem(LS.entry, String(v))
  else localStorage.removeItem(LS.entry)
}

export function getLeagueIds(): number[] {
  const v = localStorage.getItem(LS.leagues)
  return v ? v.split(',').map(Number).filter(Boolean) : []
}
export function setLeagueIds(ids: number[]) {
  localStorage.setItem(LS.leagues, ids.join(','))
}

export function getTheme(): 'dark' | 'light' {
  return (localStorage.getItem(LS.theme) as 'dark' | 'light') || 'dark'
}
export function setTheme(t: 'dark' | 'light') {
  localStorage.setItem(LS.theme, t)
  document.documentElement.setAttribute('data-theme', t)
}
