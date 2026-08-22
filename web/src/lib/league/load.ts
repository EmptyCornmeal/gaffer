// I/O for the Rivals view, kept out of the component so every partial-failure
// path is reachable from a test rather than requiring a bad Saturday.
//
// Two very different reads live here. The grid needs ONE gameweek — the newest
// one whose picks FPL has published — and is cheap. The ledger needs every
// finished gameweek, which by spring is forty live payloads and three hundred
// picks; that one is cached to disk, because a final gameweek is final.

import {
  packPoints, readCached, unpackPoints, writeCached,
} from './cache'
import {
  gatherSquads, squadFromPicks, type ManagerSquad, type PicksPayload,
} from './squads'
import type { GwPoints } from './ledger'

export interface Reader {
  picks(entry: number, gw: number): Promise<PicksPayload>
  live(gw: number): Promise<{ elements?: { id?: unknown; stats?: { total_points?: unknown } }[] }>
}

/** Final points for every player who scored in one gameweek. */
export async function pointsForGw(
  gw: number, read: Reader, final: boolean,
): Promise<Map<number, number>> {
  if (final) {
    const hit = unpackPoints(readCached(`pts.${gw}`))
    if (hit) return hit
  }
  const payload = await read.live(gw)
  const out = new Map<number, number>()
  for (const el of payload?.elements ?? []) {
    if (typeof el?.id !== 'number') continue
    const v = Number(el?.stats?.total_points ?? 0)
    if (Number.isFinite(v) && v !== 0) out.set(el.id, v)
  }
  if (final) writeCached(`pts.${gw}`, packPoints(out))
  return out
}

interface PackedSquad {
  e: number; g: number; w: [number, number][]; h: number[]
  c: number | null; v: number | null; ch: string | null; hi: number
  op: number | null
}

function pack(s: ManagerSquad): PackedSquad {
  return {
    e: s.entry, g: s.gw, w: [...s.weights], h: [...s.held],
    c: s.captain, v: s.vice, ch: s.chip, hi: s.hits, op: s.officialPoints,
  }
}

function unpack(raw: unknown): ManagerSquad | null {
  const p = raw as PackedSquad | null
  if (!p || typeof p.e !== 'number' || !Array.isArray(p.w) || !Array.isArray(p.h)) {
    return null
  }
  return {
    entry: p.e, gw: p.g, weights: new Map(p.w), held: new Set(p.h),
    captain: p.c ?? null, vice: p.v ?? null, chip: p.ch ?? null,
    hits: p.hi ?? 0, officialPoints: typeof p.op === 'number' ? p.op : null,
  }
}

/**
 * One manager's squad for one gameweek, from disk when the gameweek is over.
 *
 * Only final gameweeks are written. Caching a live one would freeze a score that
 * is still moving, and the ledger's reconciliation gate would then reject the
 * week forever on stale numbers rather than accepting it once FPL settles.
 */
export async function squadFor(
  entry: number, gw: number, read: Reader, final: boolean,
): Promise<ManagerSquad | null> {
  const key = `sq.${gw}.${entry}`
  if (final) {
    const hit = unpack(readCached(key))
    if (hit) return hit
  }
  const squad = squadFromPicks(entry, gw, await read.picks(entry, gw))
  if (squad && final) writeCached(key, pack(squad))
  return squad
}

export interface GridLoad {
  gw: number
  squads: ManagerSquad[]
  /** Managers whose squad could not be read. Named on screen. */
  unread: number
}

export async function loadGrid(
  entries: number[], gw: number, read: Reader,
): Promise<GridLoad> {
  const out = await gatherSquads(entries, gw, (e, g) => read.picks(e, g))
  return { gw, squads: out.squads, unread: out.unread }
}

export interface LedgerLoad {
  /** gameweek → entry → squad */
  byGw: Map<number, Map<number, ManagerSquad>>
  points: Map<number, GwPoints>
  /** Gameweeks that could not be read at all, as opposed to not reconciling. */
  unreadable: number[]
}

/**
 * Every finished gameweek, for every manager.
 *
 * Gameweeks run in sequence and managers concurrently within one: the alternative
 * fans a whole season at an unauthenticated shared proxy in a single burst, and
 * restraint here is a correctness property rather than manners (see
 * `lib/refresh.ts`). Cached weeks cost nothing, so after the first visit this is
 * one gameweek's worth of work however long the season has run.
 */
export async function loadLedger(
  entries: number[], finalGws: number[], read: Reader,
): Promise<LedgerLoad> {
  const byGw = new Map<number, Map<number, ManagerSquad>>()
  const points = new Map<number, GwPoints>()
  const unreadable: number[] = []

  for (const gw of finalGws) {
    try {
      const pts = await pointsForGw(gw, read, true)
      const squads = await Promise.all(
        entries.map((e) => squadFor(e, gw, read, true).catch(() => null)),
      )
      const forGw = new Map<number, ManagerSquad>()
      for (const s of squads) if (s) forGw.set(s.entry, s)
      // A gameweek nobody could be read for is not a gameweek of zeroes.
      if (!forGw.size) {
        unreadable.push(gw)
        continue
      }
      byGw.set(gw, forGw)
      points.set(gw, { gw, points: pts })
    } catch {
      unreadable.push(gw)
    }
  }
  return { byGw, points, unreadable }
}

/**
 * The newest gameweek whose picks anyone can see.
 *
 * FPL publishes a manager's squad at the deadline and not one second before, so
 * while `current_gw`'s deadline is still ahead of us the newest visible squads
 * belong to the gameweek before it. Returns null in pre-season, when there is no
 * such gameweek at all.
 */
export function visibleGw(
  currentGw: number, deadline: string | null | undefined, now: number,
): number | null {
  const at = deadline ? Date.parse(deadline) : NaN
  const gw = Number.isFinite(at) && now < at ? currentGw - 1 : currentGw
  return gw >= 1 ? gw : null
}
