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
import {
  assembleLive, missingFromLive, type LiveRivalInput, type LiveSquadInput,
} from './assemble'

/** How many rivals to score. A live view that walks a 200-player league turns a
 *  scoreboard into a rate-limit problem. */
export const MAX_RIVALS = 12

// Per-request deadlines do not compose. `fpl.ts` bounds each read at 12s, which
// is correct for one read and useless for twelve of them in series: against a
// proxy that is slow but alive that is ~144s of spinner before the first live
// load gives up, and during a match a page doing nothing is the failure. So the
// rival phase gets a deadline of its own, on top of the per-request ones.
//
// 20s buys one full wave of the slowest permitted request plus most of a second,
// so a merely slow proxy still returns a complete league table. Lower and a
// 5s-per-request proxy silently loses its last wave of managers; higher and a
// dead proxy holds up a score we already have in hand — the rival squads only
// feed the league-swing view, never your own points.
const RIVAL_BUDGET_MS = 20_000
// Four in flight, not twelve. The proxy is shared and unauthenticated, so
// restraint is a correctness property rather than manners (see refresh.ts);
// four clears the whole set well inside the budget without arriving as a burst.
const RIVAL_CONCURRENCY = 4
// How long an INCOMPLETE locked snapshot is allowed to stand in for a complete
// one. Long enough that a permanently unreadable league is not re-read on every
// 60s poll; short enough that a blip during one over of injury time has healed
// before the next goal.
const INCOMPLETE_RETRY_MS = 3 * 60_000
// ...doubling from there, to a ceiling. The first retry is quick because the
// common failure really is one 500, but an endpoint that is still refusing
// after three attempts is not having a blip, and re-reading a whole league
// every three minutes for the rest of a gameweek spends a shared,
// unauthenticated proxy's budget on an answer we already have. The ceiling is
// under half an hour so a league that comes back mid-afternoon is picked up
// within the same match.
const INCOMPLETE_RETRY_MAX_MS = 24 * 60_000

/** How long to wait before rebuilding after `n` consecutive incomplete builds. */
export function retryDelay(n: number): number {
  return Math.min(INCOMPLETE_RETRY_MAX_MS,
                  INCOMPLETE_RETRY_MS * 2 ** Math.max(0, n - 1))
}

export type LiveSourceName = 'proxy' | 'artifact'

export interface LiveResult {
  state: unknown
  source: LiveSourceName
  /** Why we fell back, when we did. Shown, never swallowed. */
  fallbackReason: string | null
  /**
   * What this result could not read, when it was still worth rendering without
   * it. A rivals table quietly four managers short looks exactly like a league
   * of eight, so partial data has to say so out loud or it is just a wrong
   * answer with a confident face.
   */
  incomplete: string | null
}

export interface PicksLike {
  picks?: { element: number; position: number; multiplier: number;
            is_captain: boolean; is_vice_captain: boolean }[]
  active_chip?: string | null
  /** FPL's own history row for this gameweek, carried on the picks response. */
  entry_history?: {
    points?: number
    total_points?: number
    event_transfers_cost?: number
  } | null
}

interface StandingRow {
  entry: number
  entry_name?: string
  player_name?: string
  total?: number
  /** This gameweek's contribution to `total`. Both move during the gameweek. */
  event_total?: number
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

/**
 * The same two numbers, from ONE history row — the block a picks payload
 * carries under `entry_history`.
 *
 * `total_points` there is cumulative, net of every hit taken so far, and it
 * INCLUDES the gameweek the row belongs to; `points` is that gameweek's gross
 * score. So what was carried in is `total_points - points + this week's hit`,
 * which is the figure `baselineAndHits` reaches by walking the whole history.
 *
 * This exists because the history endpoint is a request of its own and can fail
 * while the picks request — which the live view cannot proceed without anyway —
 * succeeds. Returns null when the row is not a history row, so an unreadable
 * baseline stays unreadable instead of quietly becoming zero.
 */
export function baselineFromRow(
  row: { points?: number; total_points?: number;
         event_transfers_cost?: number } | null | undefined,
): { baseline: number; hits: number } | null {
  const total = row?.total_points
  const points = row?.points
  if (typeof total !== 'number' || typeof points !== 'number') return null
  const hits = typeof row?.event_transfers_cost === 'number'
    ? row.event_transfers_cost
    : 0
  return { baseline: total - points + hits, hits }
}

/**
 * Settle `p` no later than `ms` from now.
 *
 * The request underneath keeps running — nothing out here can abort a fetch
 * started inside `fpl.ts` — but the caller stops waiting on it, and how long the
 * caller waits is the only thing the user experiences. `Promise.race` attaches
 * its own handlers to `p`, so a loser that rejects later is already handled and
 * never surfaces as an unhandled rejection.
 */
function deadlined<T>(p: Promise<T>, ms: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const cap = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`gave up after ${Math.round(ms / 1000)}s`)), ms)
  })
  return Promise.race([p, cap]).finally(() => clearTimeout(timer))
}

/**
 * Rival squads: concurrent, and bounded as a phase rather than per request.
 *
 * Takes its reader as an argument for the same reason `assemble.ts` takes its
 * payloads as arguments — every timeout and partial-failure path is then
 * reachable from a test instead of requiring a slow Saturday.
 *
 * Results keep standings order. That is not cosmetic: `largestSwing` picks the
 * closest rival with a strict `<`, so the first of two equally-close managers
 * wins, and reordering here would change which one the page names.
 */
export async function gatherRivals(
  rows: StandingRow[],
  entryId: number,
  picksFor: (entry: number) => Promise<PicksLike>,
  opts: { budgetMs?: number; concurrency?: number } = {},
): Promise<{ rivals: LiveRivalInput[]; wanted: number; unread: number }> {
  const budgetMs = opts.budgetMs ?? RIVAL_BUDGET_MS
  const concurrency = opts.concurrency ?? RIVAL_CONCURRENCY
  const targets = rows.slice(0, MAX_RIVALS).filter((r) => r.entry !== entryId)
  const got = new Array<LiveRivalInput | null>(targets.length).fill(null)
  const deadline = Date.now() + budgetMs
  let cursor = 0
  // Counts answers, not squads. A manager who genuinely has no picks for this
  // gameweek answered the question; treating that as missing data would keep
  // the snapshot permanently "incomplete" and re-read the whole league forever.
  let read = 0

  const worker = async (): Promise<void> => {
    for (;;) {
      const i = cursor++
      if (i >= targets.length) return
      const left = deadline - Date.now()
      if (left <= 0) return
      const row = targets[i]
      try {
        const rp = await deadlined(picksFor(row.entry), left)
        read += 1
        const rs = squadFromPicks(rp)
        if (!rs) continue
        got[i] = {
          ...rs,
          entry_id: row.entry,
          name: row.player_name || row.entry_name || String(row.entry),
          // C10. `total` is a season total that MOVES during the gameweek, so
          // handing it to `scoreSquad` as a baseline adds this week's live
          // points to a figure that already contains them. `event_total` is
          // FPL's own account of what this gameweek contributed to `total` —
          // the table is built as the sum of them — so the difference is
          // exactly what the manager carried in, and it holds still while the
          // matches run. Mirrors `_live_rivals` in pipeline.py, which subtracts
          // the same pair; the two must not drift, because the swing, the
          // closest-rival choice and the league table all read this one number.
          total: (row.total ?? 0) - (row.event_total ?? 0),
          // And his hits are read rather than assumed. Hardcoding zero showed a
          // rival who took a -8 as eight points better than he was, in the
          // table and in the swing at once. The picks payload carries them and
          // we have already paid for the request.
          hits: Number(rp.entry_history?.event_transfers_cost ?? 0) || 0,
          active_chip: rp.active_chip ?? null,
        }
      } catch {
        // One unreadable rival must not cost the whole scoreboard.
      }
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(concurrency, targets.length) }, worker))
  return {
    rivals: got.filter((r): r is LiveRivalInput => r != null),
    wanted: targets.length,
    unread: targets.length - read,
  }
}

interface LockedState {
  squad: LiveSquadInput | null
  activeChip: string | null
  rivals: LiveRivalInput[]
  /** Season points carried into this gameweek, or null when nothing could
   *  supply it. Null is a value here, not a missing one — see `markLiveGaps`. */
  baseline: number | null
  hits: number
  /** Which read produced the baseline, in the words pipeline.py uses. */
  baselineSource: string
  /** Noun phrases for whatever could not be read. Empty when all of it was. */
  missing: string[]
}

interface LockedEntry {
  task: Promise<LockedState>
  settled: boolean
  complete: boolean
  at: number
  /** Consecutive incomplete builds, for the backoff. Reset by a complete one. */
  attempts: number
}

/**
 * The parts of an assembled state this file is allowed to write on.
 *
 * `LiveState` describes what the scoring rules produce; these fields describe
 * what the fetching could not read, which is a different question and belongs
 * to a different layer. Kept as a narrow view rather than an `any` so a typo
 * here is still a compile error.
 */
interface AnnotatedLiveState {
  squad?: { season_total_before: number | null
            season_total_projected: number | null }
  rivals?: unknown[]
  rival_squads?: unknown[]
  largest_swing?: unknown
  baseline_source?: string
  missing_players?: number[]
  incomplete?: string | null
}

/**
 * Write onto an assembled state what could not be read, and withhold whatever
 * cannot honestly be shown without it.
 *
 * Mirror of `_mark_live_gaps` in `src/gaffer/pipeline.py`. The Live page renders
 * whichever of the two answered — the browser during a match, the published
 * artifact when the proxy is down — so they must blank the same fields and name
 * the same gaps, or the page's honesty would depend on which one it got.
 *
 * Deliberately outside `assembleLive`: that is the scoring rulebook, pinned
 * byte-for-byte against `gaffer.live` by the parity tests, and it never sees the
 * I/O that failed. This is the layer that knows.
 */
export function markLiveGaps(
  state: AnnotatedLiveState,
  lock: { baseline: number | null; baselineSource: string; missing: string[] },
  absent: number[],
): string | null {
  const gaps: string[] = [...lock.missing]
  state.baseline_source = lock.baselineSource
  if (lock.baseline == null) {
    // C9. A baseline of zero is not a smaller answer than the real one, it is a
    // different and wrong one: the season total renders as this gameweek's
    // score. Nothing beats that.
    if (state.squad) {
      state.squad.season_total_before = null
      state.squad.season_total_projected = null
    }
    if (state.rivals?.length) {
      // The table is ordered on season totals. Without yours you sort as though
      // the season began this morning, which quietly moves every rival up a
      // place and hands `largestSwing` the wrong "closest" manager.
      state.rivals = []
      state.largest_swing = null
      // B5. And the per-rival rows go with it. Each carries the
      // `provisional_position` that table gave it, and a `differential`
      // measured against a squad whose season total is unreadable — so leaving
      // them would publish a standing nothing else on the page still claims.
      // `_mark_live_gaps` in pipeline.py drops the same key.
      state.rival_squads = []
      gaps.push('the league table, which needs it')
    }
  }
  if (absent.length) {
    state.missing_players = absent
    gaps.push(`${absent.length} of your players missing from the live feed`)
  }
  const named = [...new Set(gaps)]
  state.incomplete = named.length ? named.join(', ') : null
  return state.incomplete
}

/** Per-gameweek cache for the things that cannot change once the deadline has
 *  passed. Keyed so switching entry or gameweek refetches. */
const locked = new Map<string, LockedEntry>()

async function lockedState(
  entryId: number, gw: number, leagueIds: number[],
): Promise<LockedState> {
  const key = `${entryId}:${gw}:${leagueIds.join(',')}`
  const hit = locked.get(key)
  // Picks lock at the deadline, so a COMPLETE snapshot is good for the rest of
  // the gameweek and is never refetched. C14: an INCOMPLETE one used to be kept
  // just as permanently, so a single 500 from the league endpoint at 15:00
  // deleted the rivals table until the tab was reloaded. Now it expires, and the
  // next poll rebuilds it — on a widening delay, so a league that is genuinely
  // unreadable stops costing a full re-read every three minutes. An in-flight
  // task is always shared, expiry or not, so this cannot fan out into duplicate
  // requests.
  if (hit && (!hit.settled || hit.complete
              || Date.now() - hit.at < retryDelay(hit.attempts))) {
    return hit.task
  }

  const task = (async (): Promise<LockedState> => {
    const missing: string[] = []
    // Three independent reads. Chained they are three 12s deadlines stacked into
    // a 36s wait before the rival phase even begins; overlapped they cost one.
    // The price is that a broken entry id now also spends a league read before
    // failing, which is two extra requests a minute on a path that is already
    // misconfigured.
    const [picksR, historyR, standingsR] = await Promise.allSettled([
      fpl.picks(entryId, gw),
      fpl.entryHistory(entryId),
      leagueIds.length ? fpl.league(leagueIds[0], 1) : Promise.resolve(null),
    ])

    // Your own picks are the one thing this cannot proceed without: no squad, no
    // score, and `fetchLive` falls back to the published artifact.
    if (picksR.status === 'rejected') throw picksR.reason
    const picks = picksR.value as PicksLike
    const squad = squadFromPicks(picks)

    // C9. This used to cache `baseline = 0, hits = 0` whenever the history read
    // failed — for the whole session, because the snapshot never expired — so
    // the season total silently became "this gameweek's score" and the league
    // table put you last. Python failed the same moment differently, falling
    // back to `overall_points`, which its own docstring says already contains
    // the live gameweek: one wrong answer too low, one too high, neither
    // labelled. Both now do this, in this order.
    let baseline: number | null = null
    let hits = 0
    let baselineSource = 'unavailable'
    // A payload without a `current` list is not a history, however cheerfully
    // it arrived. `_live_baseline` in pipeline.py applies the same test in the
    // same order.
    const history = historyR.status === 'fulfilled' ? historyR.value : null
    const row = baselineFromRow(picks?.entry_history)
    if (Array.isArray(history?.current)) {
      const b = baselineAndHits(history, gw)
      baseline = b.baseline
      hits = b.hits
      baselineSource = 'entry_history'
    } else if (row) {
      // The picks response carries the same arithmetic in a single row, and it
      // is a request this path cannot proceed without anyway. So the usual
      // outcome of an unreadable history is now an exact baseline, not a
      // plausible one — and no extra call.
      baseline = row.baseline
      hits = row.hits
      baselineSource = 'picks_entry_history'
    } else {
      // Nothing could supply it. `baseline` stays null and `markLiveGaps` shows
      // the season total as unavailable; naming it here is also what keeps the
      // snapshot incomplete, so the next window retries the history endpoint.
      missing.push('your season total so far')
    }

    let rivals: LiveRivalInput[] = []
    if (leagueIds.length) {
      if (standingsR.status === 'fulfilled') {
        const rows: StandingRow[] = standingsR.value?.standings?.results ?? []
        const out = await gatherRivals(
          rows, entryId, (entry) => fpl.picks(entry, gw) as Promise<PicksLike>)
        rivals = out.rivals
        if (out.unread) {
          missing.push(`${out.unread} of ${out.wanted} rival squads`)
        }
      } else {
        // No league data is a thinner view, not a broken one.
        missing.push('the league standings')
      }
    }
    return {
      squad, activeChip: picks?.active_chip ?? null, rivals, baseline, hits,
      baselineSource, missing,
    }
  })()

  const entry: LockedEntry = {
    task, settled: false, complete: false, at: Date.now(),
    attempts: hit?.attempts ?? 0,
  }
  locked.set(key, entry)
  task.then(
    (v) => {
      entry.settled = true
      entry.complete = v.missing.length === 0
      entry.attempts = entry.complete ? 0 : entry.attempts + 1
      entry.at = Date.now()
    },
    () => {
      // A failure must be retryable, and immediately: there is nothing here to
      // keep. Guarded so a task that lost its slot cannot evict its successor.
      if (locked.get(key) === entry) locked.delete(key)
    },
  )
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
    // The artifact names its own gaps — pipeline.py writes the same field for
    // the same reasons. Dropping it here would make the fallback look more
    // complete than the live path it replaced, which is the wrong way round.
    const carried = (state as { incomplete?: unknown }).incomplete
    return {
      state,
      source: 'artifact',
      fallbackReason: reason,
      incomplete: typeof carried === 'string' && carried ? carried : null,
    }
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
    // The locked snapshot and the two live payloads share no data, so they are
    // fetched together. Chained, an expired-snapshot retry would delay the score
    // it exists to decorate; overlapped, a tick costs the slower of the two.
    const [lock, livePayload, fixturesPayload] = await Promise.all([
      lockedState(entryId, gw, getLeagueIds()),
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
      // Zero only ever reaches the scorer here; nothing downstream reads the
      // season total it produces, because `markLiveGaps` blanks it below when
      // the real baseline is unknown. Passing null instead would put a null
      // into arithmetic `assembleLive` shares with gaffer.live.
      baseline: lock.baseline ?? 0,
      hits: lock.hits,
      activeChip: lock.activeChip,
      asOf: new Date().toISOString(),
    }) as LiveState
    const incomplete = markLiveGaps(
      state as unknown as AnnotatedLiveState,
      lock,
      missingFromLive(lock.squad, livePayload),
    )
    return { state, source: 'proxy', fallbackReason: null, incomplete }
  } catch (e) {
    return fallback(e instanceof Error ? e.message : String(e))
  }
}
