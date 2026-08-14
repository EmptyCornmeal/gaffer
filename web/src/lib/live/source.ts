// Where the Live page's data comes from.
//
// The pipeline publishes `live.json` three times a day. That is fine for a
// fallback and useless during a match, so this fetches the live endpoints
// through the read-only proxy and scores them in the browser with the same rules
// the pipeline uses (`./assemble`).
//
// Two requests per tick, and no more: your picks and your rivals' picks lock at
// the deadline, so they are fetched once per gameweek and reused. Only the live
// stats and the fixture states actually change while you are watching.

import { getEntryId, getLeagueIds } from '../config'
import { loadLiveSnapshot } from '../data'
import { fpl } from '../fpl'
import type { Player } from '../types'
import type { LiveState } from '../weekly'
import { assembleLive, type LiveRivalInput, type LiveSquadInput } from './assemble'

/** How many rivals to score. A live view that walks a 200-player league turns a
 *  scoreboard into a rate-limit problem. */
export const MAX_RIVALS = 12

export type LiveSourceName = 'proxy' | 'artifact'

export interface LiveResult {
  state: unknown
  source: LiveSourceName
  /** Why we fell back, when we did. Shown, never swallowed. */
  fallbackReason: string | null
}

interface PicksLike {
  picks?: { element: number; position: number; multiplier: number;
            is_captain: boolean; is_vice_captain: boolean }[]
  active_chip?: string | null
}

function squadFromPicks(payload: PicksLike | null | undefined): LiveSquadInput | null {
  const picks = payload?.picks
  if (!picks?.length) return null
  const ordered = [...picks].sort((a, b) => a.position - b.position)
  return {
    starting: ordered.filter((p) => p.position <= 11).map((p) => p.element),
    bench: ordered.filter((p) => p.position > 11).map((p) => p.element),
    captain: ordered.find((p) => p.is_captain)?.element ?? null,
    vice: ordered.find((p) => p.is_vice_captain)?.element ?? null,
  }
}

/**
 * Season points carried INTO `gw`, and the transfer cost paid FOR it.
 * Mirrors `live.entry_baseline_and_hits`: the cumulative total at the previous
 * event is the only figure that is exactly "before this gameweek".
 */
export function baselineAndHits(
  history: { current?: { event?: number; points?: number; total_points?: number;
                        event_transfers_cost?: number }[] } | null | undefined,
  gw: number,
): { baseline: number; hits: number } {
  const rows = (history?.current ?? []).filter((r) => typeof r?.event === 'number')
  const prior = rows.filter((r) => (r.event as number) < gw)
  let baseline = 0
  if (prior.length) {
    const last = prior.reduce((a, b) => ((a.event as number) > (b.event as number) ? a : b))
    baseline = typeof last.total_points === 'number'
      ? last.total_points
      : prior.reduce((s, r) => s + (r.points ?? 0) - (r.event_transfers_cost ?? 0), 0)
  }
  const here = rows.find((r) => r.event === gw)
  return { baseline, hits: here?.event_transfers_cost ?? 0 }
}

/** Per-gameweek cache for the things that cannot change once the deadline has
 *  passed. Keyed so switching entry or gameweek refetches. */
const locked = new Map<string, Promise<{
  squad: LiveSquadInput | null
  activeChip: string | null
  rivals: LiveRivalInput[]
  baseline: number
  hits: number
}>>()

async function lockedState(entryId: number, gw: number, leagueIds: number[]) {
  const key = `${entryId}:${gw}:${leagueIds.join(',')}`
  const hit = locked.get(key)
  if (hit) return hit
  const task = (async () => {
    const picks = await fpl.picks(entryId, gw) as PicksLike
    const squad = squadFromPicks(picks)
    let baseline = 0
    let hits = 0
    try {
      const history = await fpl.entryHistory(entryId)
      const b = baselineAndHits(history, gw)
      baseline = b.baseline
      hits = b.hits
    } catch {
      // A missing baseline costs a season total, not the live score.
    }
    const rivals: LiveRivalInput[] = []
    if (leagueIds.length) {
      try {
        const standings = await fpl.league(leagueIds[0], 1)
        const rows: { entry: number; entry_name?: string; player_name?: string;
                      total?: number }[] =
          standings?.standings?.results ?? []
        for (const row of rows.slice(0, MAX_RIVALS)) {
          if (row.entry === entryId) continue
          try {
            const rp = await fpl.picks(row.entry, gw) as PicksLike
            const rs = squadFromPicks(rp)
            if (!rs) continue
            rivals.push({
              ...rs,
              entry_id: row.entry,
              name: row.player_name || row.entry_name || String(row.entry),
              // Mirrors the pipeline: FPL's standings `total` already moves
              // during a gameweek. Diverging here would break parity with
              // gaffer.live; it is a pre-existing question, not a new one.
              total: row.total ?? 0,
              hits: 0,
              active_chip: rp.active_chip ?? null,
            })
          } catch {
            // One unreadable rival must not cost the whole scoreboard.
          }
        }
      } catch {
        // No league data is a thinner view, not a broken one.
      }
    }
    return { squad, activeChip: picks?.active_chip ?? null, rivals, baseline, hits }
  })()
  locked.set(key, task)
  task.catch(() => locked.delete(key))   // a failure must be retryable
  return task
}

/** Drop the per-gameweek cache. Exposed for tests and for a manual refresh. */
export function resetLiveCache(): void {
  locked.clear()
}

/**
 * One live fetch. Returns the artifact instead whenever the proxy cannot answer,
 * and always says which source it used — a stale number presented as live is the
 * exact failure this page exists to avoid.
 */
export async function fetchLive(
  gw: number,
  players: Player[],
): Promise<LiveResult> {
  const fallback = async (reason: string): Promise<LiveResult> => {
    const state = await loadLiveSnapshot()
    if (state == null) throw new Error(reason)
    return { state, source: 'artifact', fallbackReason: reason }
  }

  const entryId = getEntryId()
  if (!fpl.configured()) return fallback('no live proxy is configured')
  if (entryId == null) return fallback('no FPL entry id is set')
  if (!players.length) return fallback('player data has not loaded yet')

  const positions = new Map<number, string>()
  const teamOf = new Map<number, number>()
  const names = new Map<number, string>()
  const predictions = new Map<number, number>()
  for (const p of players) {
    positions.set(p.id, p.pos)
    teamOf.set(p.id, p.team_id)
    names.set(p.id, p.name)
    const forGw = p.gw_xp?.find((g) => g.gw === gw)
    if (forGw) predictions.set(p.id, forGw.xp)
  }

  try {
    const lock = await lockedState(entryId, gw, getLeagueIds())
    const [livePayload, fixturesPayload] = await Promise.all([
      fpl.live(gw),
      fpl.fixtures(gw),
    ])
    const state = assembleLive({
      gw,
      livePayload,
      fixturesPayload,
      squad: lock.squad,
      positions,
      teamOf,
      now: new Date(),
      predictions,
      rivals: lock.rivals,
      names,
      entryId,
      baseline: lock.baseline,
      hits: lock.hits,
      activeChip: lock.activeChip,
      asOf: new Date().toISOString(),
    }) as LiveState
    return { state, source: 'proxy', fallbackReason: null }
  } catch (e) {
    return fallback(e instanceof Error ? e.message : String(e))
  }
}
