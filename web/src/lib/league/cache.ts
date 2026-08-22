// A small persistent store for facts that cannot change again.
//
// `fpl.ts` caches in memory for five minutes, which is right for a league table
// and wrong for a finished gameweek: GW3's final points are the same in April as
// they were in September, and re-reading them on every visit turns a season
// ledger into ~40 requests a page load by the spring. Only gameweeks the caller
// has already established are FINAL are written here — see `meta.last_finished_gw`
// and the reconciliation gate in `./ledger`.
//
// Everything degrades to "no cache" rather than throwing. Storage is disabled in
// private mode, full on some phones, and unavailable in the test runner; none of
// those is a reason for the page not to render.

const PREFIX = 'gaffer.league.v1.'

export interface Store {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

function store(explicit?: Store): Store | null {
  if (explicit) return explicit
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null
  } catch {
    return null
  }
}

export function readCached<T>(key: string, explicit?: Store): T | null {
  const s = store(explicit)
  if (!s) return null
  try {
    const raw = s.getItem(PREFIX + key)
    return raw ? (JSON.parse(raw) as T) : null
  } catch {
    // A corrupt entry is worse than a missing one, because it comes back every
    // time. Drop it and behave as a miss.
    try {
      s.removeItem(PREFIX + key)
    } catch {
      /* ignore */
    }
    return null
  }
}

/** Returns whether it was actually stored — a full quota is not an error here. */
export function writeCached(key: string, value: unknown, explicit?: Store): boolean {
  const s = store(explicit)
  if (!s) return false
  try {
    s.setItem(PREFIX + key, JSON.stringify(value))
    return true
  } catch {
    return false
  }
}

/** A points map survives a round trip as pairs; `Map` is not JSON. */
export function packPoints(points: Map<number, number>): [number, number][] {
  // Only players who scored. A gameweek has ~700 elements and most of them
  // contribute a zero that the reader can supply for free.
  return [...points].filter(([, v]) => v !== 0)
}

export function unpackPoints(packed: unknown): Map<number, number> | null {
  if (!Array.isArray(packed)) return null
  const out = new Map<number, number>()
  for (const pair of packed) {
    if (!Array.isArray(pair) || pair.length !== 2) return null
    const [id, v] = pair
    if (typeof id !== 'number' || typeof v !== 'number') return null
    out.set(id, v)
  }
  return out
}

/** Forget everything this module has stored. Exposed for Settings and tests. */
export function clearLeagueCache(explicit?: Store): void {
  const s = store(explicit)
  if (!s) return
  try {
    const ls = s as unknown as { length?: number; key?: (i: number) => string | null }
    if (typeof ls.length !== 'number' || typeof ls.key !== 'function') return
    const doomed: string[] = []
    for (let i = 0; i < ls.length; i++) {
      const k = ls.key(i)
      if (k && k.startsWith(PREFIX)) doomed.push(k)
    }
    for (const k of doomed) s.removeItem(k)
  } catch {
    /* ignore */
  }
}
