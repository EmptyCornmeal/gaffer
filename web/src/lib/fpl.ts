// Live FPL data via the Cloudflare Worker proxy (friendly /api/* paths).
// Only used for per-user data the static artifacts can't hold: your picks,
// entry history, classic-league standings, live points, player photos.

import { apiBase } from './config'

export class ProxyError extends Error {}

/**
 * The proxy took the request and never answered.
 *
 * Separate from ProxyError because the two need different words in front of a
 * user: a 404 means "that league id is wrong", a timeout means "try again". It
 * still *is* a ProxyError, so anything that only cares that the proxy failed
 * keeps working untouched.
 */
export class ProxyTimeoutError extends ProxyError {
  constructor(path: string, ms: number) {
    super(`${path} → timed out after ${Math.round(ms / 1000)}s`)
  }
}

// A request with no deadline is not slow, it is broken. fetch() never settles
// against a proxy that accepts the connection and then stops talking, so the
// promise hangs forever and every `.catch()` downstream of it is unreachable —
// the page keeps its spinner and its error state is dead code. These budgets
// turn that silence into an ordinary rejection.
//
// They cover the whole exchange, connect through body, because a response stream
// that stalls mid-download strands a page just as thoroughly as one that never
// arrives.
//
// 12s for a one-shot read: room for a cold proxy start plus a slow mobile
// handshake and the ~1MB bootstrap payload, while still answering the user while
// they are still looking at the screen.
const TIMEOUT = 12_000
// The live endpoints are polled once a minute (refresh.ts DEFAULTS.intervalMs)
// and the poller drops a tick that arrives while one is still in flight, so a
// request permitted to run near the interval would quietly halve the refresh
// rate. Bound them well inside it. A missed live tick costs sixty seconds of
// staleness and then retries itself, which is a far cheaper failure than a page
// load has available.
const LIVE_TIMEOUT = 8_000

const _cache = new Map<string, { t: number; v: unknown }>()
const TTL = 5 * 60 * 1000

async function get<T>(path: string, ttl = TTL, timeout = TIMEOUT): Promise<T> {
  const base = apiBase()
  if (!base) throw new ProxyError('No proxy configured')
  const key = `${base}${path}`
  const hit = _cache.get(key)
  if (hit && Date.now() - hit.t < ttl) return hit.v as T
  // Nothing reaches the cache until a body has parsed, so a timeout leaves no
  // entry behind to be handed out as a result on the next call.
  const v = await request<T>(`${base}${path}`, path, timeout)
  _cache.set(key, { t: Date.now(), v })
  return v
}

async function request<T>(url: string, path: string, timeout: number): Promise<T> {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(timeout) })
    if (!res.ok) throw new ProxyError(`${path} → ${res.status}`)
    return (await res.json()) as T
  } catch (e) {
    if (e instanceof ProxyError) throw e
    // AbortSignal.timeout() rejects with a DOMException named 'TimeoutError'.
    // The name is the part the spec pins down; the concrete class differs
    // between the browser and the runtime the unit tests run in.
    if ((e as { name?: string } | null)?.name === 'TimeoutError') {
      throw new ProxyTimeoutError(path, timeout)
    }
    throw e
  }
}

export interface Pick {
  element: number
  position: number
  multiplier: number
  is_captain: boolean
  is_vice_captain: boolean
}
export interface PicksResponse {
  picks: Pick[]
  active_chip: string | null
  entry_history: { bank: number; value: number; event_transfers: number; points: number }
}

export interface LeagueStanding {
  entry: number
  entry_name: string
  player_name: string
  rank: number
  last_rank: number
  total: number
  event_total: number
}

/**
 * A manager-supplied name as it can safely be shown.
 *
 * FPL's own bytes carry U+FFFD in some team names — byte-identical from
 * fantasy.premierleague.com and from the proxy, so nothing on our side mangled
 * it. Someone typed a character FPL's storage could not keep and the replacement
 * character is all that survived; the original is gone and unrecoverable here.
 * U+FFFD means "a character was lost", which the reader can do nothing with,
 * while a stray black diamond in the table reads as our bug. Strip it, but never
 * to the point of an empty cell.
 */
export function displayName(raw: string | null | undefined): string {
  const s = (raw ?? '').trim()
  const clean = s.replace(/\uFFFD/g, '').replace(/\s+/g, ' ').trim()
  return clean || s
}

export const fpl = {
  configured: () => !!apiBase(),
  bootstrap: () => get<any>('/bs', 60 * 60 * 1000),
  entry: (id: number) => get<any>(`/en/${id}`),
  entryHistory: (id: number) => get<any>(`/en/${id}/history`),
  picks: (id: number, gw: number) => get<PicksResponse>(`/ep/${id}/${gw}/picks`),
  league: (id: number, page = 1) => get<any>(`/lc/${id}/${page}`),
  live: (gw: number) => get<any>(`/ev/${gw}/live`, 60 * 1000, LIVE_TIMEOUT),
  // Fixture states and live BPS, for scoring a gameweek in the browser.
  fixtures: (gw: number) => get<any>(`/fx/${gw}`, 60 * 1000, LIVE_TIMEOUT),
  photoUrl: (code: number | null | undefined) =>
    code ? `${apiBase()}/player-photo/${code}` : '',
}
