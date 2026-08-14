// Live FPL data via the Cloudflare Worker proxy (friendly /api/* paths).
// Only used for per-user data the static artifacts can't hold: your picks,
// entry history, classic-league standings, live points, player photos.

import { apiBase } from './config'

export class ProxyError extends Error {}

const _cache = new Map<string, { t: number; v: unknown }>()
const TTL = 5 * 60 * 1000

async function get<T>(path: string, ttl = TTL): Promise<T> {
  const base = apiBase()
  if (!base) throw new ProxyError('No proxy configured')
  const key = `${base}${path}`
  const hit = _cache.get(key)
  if (hit && Date.now() - hit.t < ttl) return hit.v as T
  const res = await fetch(`${base}${path}`)
  if (!res.ok) throw new ProxyError(`${path} → ${res.status}`)
  const v = (await res.json()) as T
  _cache.set(key, { t: Date.now(), v })
  return v
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

export const fpl = {
  configured: () => !!apiBase(),
  bootstrap: () => get<any>('/bs', 60 * 60 * 1000),
  entry: (id: number) => get<any>(`/en/${id}`),
  entryHistory: (id: number) => get<any>(`/en/${id}/history`),
  picks: (id: number, gw: number) => get<PicksResponse>(`/ep/${id}/${gw}/picks`),
  league: (id: number, page = 1) => get<any>(`/lc/${id}/${page}`),
  live: (gw: number) => get<any>(`/ev/${gw}/live`, 60 * 1000),
  // Fixture states and live BPS, for scoring a gameweek in the browser.
  fixtures: (gw: number) => get<any>(`/fx/${gw}`, 60 * 1000),
  photoUrl: (code: number | null | undefined) =>
    code ? `${apiBase()}/player-photo/${code}` : '',
}
