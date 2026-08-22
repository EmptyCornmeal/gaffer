// Per-player live state, autosubs and squad scoring — a port of
// `src/gaffer/live.py`. See `./fixtures.ts` for why this exists twice.

import { FORMATION_MIN, FORMATION_MAX } from '../squad'
import type { Pos } from '../types'
import {
  countsAsPlayed, round2, STATE_SCHEDULED,
  type FixtureState,
} from './fixtures'

export interface PlayerLive {
  id: number
  minutes: number
  confirmed: number
  provisional: number
  predicted: number
  played: boolean
  finished: boolean
  yetToPlay: boolean
  states: string[]
}

export interface RawLivePayload {
  elements?: { id?: unknown; stats?: { minutes?: unknown; total_points?: unknown } }[]
}

function playerRecord(over: Partial<PlayerLive> & { id: number }): PlayerLive {
  return {
    minutes: 0, confirmed: 0, provisional: 0, predicted: 0,
    played: false, finished: false, yetToPlay: false, states: [],
    ...over,
  }
}

export function playerTotal(p: PlayerLive): number {
  return p.confirmed + p.provisional + p.predicted
}

/**
 * The fixtures in `fx` that can still deliver points to this player.
 *
 * Both of the numbers a caller has are gameweek aggregates: one projection for
 * the week and one minutes figure for the week. So "has he played yet" cannot be
 * answered from the player at all in a double gameweek — only the calendar
 * knows, and it is asked here rather than inferred from his minutes.
 *
 * A fixture that has not kicked off is always still to come. One that is under
 * way counts only while he has no minutes anywhere: with an aggregate we cannot
 * tell which of his matches those minutes came from, and a player already on the
 * pitch is scoring into `confirmed` rather than into a projection.
 */
export function remainingFixtures(fx: FixtureState[], minutes: number): FixtureState[] {
  const pending = fx.filter((f) => !countsAsPlayed(f))
  if (minutes === 0) return pending
  return pending.filter((f) => f.state === STATE_SCHEDULED)
}

/**
 * The share of a gameweek projection that is still to be played.
 *
 * The projection is one number for the whole gameweek, so in a double it already
 * covers both matches. An even split across the club's fixtures is the only
 * division the data supports — neither the artifact nor the projections table
 * carries a per-fixture breakdown — and it is right in both directions that
 * matter: a player with one of two still to come is worth about half of it
 * rather than nothing, and one who was left out of the first of two is worth
 * about half of it rather than all of it.
 */
export function remainingXp(xp: number, remaining: number, total: number): number {
  if (total <= 0 || remaining <= 0) return 0
  return xp * remaining / total
}

/**
 * Fold the live endpoint into per-player state.
 *
 * A player is `yetToPlay` when one of his fixtures this gameweek can still
 * deliver points — which is NOT the same as "has zero minutes". Confusing the
 * two is what makes a live tool declare a substitution before the second half
 * has kicked off.
 *
 * `finished` is the mirror image: every fixture of his is over, INCLUDING the
 * gameweek in which his club has no fixture at all. A blank is not a gameweek
 * that never ends, and a player in one has to become substitutable — with
 * `fx.length > 0 && ...` he never did.
 */
export function playerLive(
  live: RawLivePayload | null | undefined,
  states: Map<number, FixtureState>,
  provBonus: Map<number, number>,
  teamOf: Map<number, number>,
  predictions?: Map<number, number>,
): Map<number, PlayerLive> {
  const preds = predictions ?? new Map<number, number>()
  const perTeam = new Map<number, FixtureState[]>()
  for (const st of states.values()) {
    for (const team of [st.team_h, st.team_a]) {
      const list = perTeam.get(team)
      if (list) list.push(st)
      else perTeam.set(team, [st])
    }
  }

  const out = new Map<number, PlayerLive>()
  for (const el of live?.elements ?? []) {
    const pid = el?.id
    if (typeof pid !== 'number') continue
    const stats = el.stats ?? {}
    const mins = Number(stats.minutes ?? 0) || 0
    const pts = Number(stats.total_points ?? 0) || 0
    const fx = perTeam.get(teamOf.get(pid) ?? -1) ?? []
    const remaining = remainingFixtures(fx, mins)
    out.set(pid, playerRecord({
      id: pid,
      minutes: mins,
      confirmed: pts,
      provisional: provBonus.get(pid) ?? 0,
      predicted: remainingXp(preds.get(pid) ?? 0, remaining.length, fx.length),
      played: mins > 0,
      finished: fx.every(countsAsPlayed),
      yetToPlay: remaining.length > 0,
      states: [...new Set(fx.map((f) => f.state))].sort(),
    }))
  }

  // Players with no live row yet: the endpoint lags kick-off, and it has no row
  // at all for a club that is not playing. Both are zero-minute players, so both
  // are read from the calendar alone.
  for (const [pid, team] of teamOf) {
    if (out.has(pid)) continue
    const fx = perTeam.get(team) ?? []
    const remaining = remainingFixtures(fx, 0)
    out.set(pid, playerRecord({
      id: pid,
      predicted: remainingXp(preds.get(pid) ?? 0, remaining.length, fx.length),
      finished: fx.every(countsAsPlayed),
      yetToPlay: remaining.length > 0,
      states: [...new Set(fx.map((f) => f.state))].sort(),
    }))
  }
  return out
}

// ---------------------------------------------------------------------------
// Autosubs
// ---------------------------------------------------------------------------

export interface Autosubs {
  xi: number[]
  bench: number[]
  subs_in: number[]
  subs_out: number[]
  captain: number | null
  captain_source: 'captain' | 'vice' | 'none'
  multiplier: number
  provisional: boolean
  notes: string[]
}

function legal(counts: Record<string, number>): boolean {
  return (Object.keys(FORMATION_MIN) as Pos[]).every(
    (p) => (counts[p] ?? 0) >= FORMATION_MIN[p] && (counts[p] ?? 0) <= FORMATION_MAX[p],
  )
}

function anyUnfinished(live: Map<number, PlayerLive>, ids: number[]): boolean {
  return ids.some((p) => !live.get(p)?.finished)
}

/**
 * Who wears the armband, and what it is worth.
 *
 * The multiplier is decided here and nowhere else — mirroring the Python, where
 * recomputing it downstream once left the record at 2 during a Triple Captain
 * week and understated the league swing by a third.
 */
export function armband(
  captain: number | null | undefined,
  vice: number | null | undefined,
  live: Map<number, PlayerLive>,
  tripleCaptain = false,
): [number | null, 'captain' | 'vice' | 'none', number] {
  const mult = tripleCaptain ? 3 : 2
  const blanked = (pid: number | null | undefined): boolean => {
    if (pid == null) return false
    const st = live.get(pid)
    return !!st && st.finished && st.minutes === 0
  }
  if (captain != null && !blanked(captain)) return [captain, 'captain', mult]
  if (vice != null && !blanked(vice)) return [vice, 'vice', mult]
  return [null, 'none', 1]
}

/**
 * FPL's substitution rules, in the order FPL applies them:
 *   1. a starter is only replaced once ALL his fixtures are over and he has zero
 *      minutes — a player yet to kick off is not "out";
 *   2. the goalkeeper can only be replaced by the bench goalkeeper;
 *   3. outfield subs are tried in bench order, and each is only made if the
 *      RESULTING XI is still a legal formation;
 *   4. a bench player who did not play cannot come on;
 *   5. Bench Boost plays all fifteen, so no substitutions happen at all;
 *   6. if the captain records zero minutes once his match is over, the armband
 *      passes to the vice; if the vice also blanks, nobody is multiplied.
 */
export function applyAutosubs(
  starting: number[],
  bench: number[],
  positions: Map<number, string>,
  live: Map<number, PlayerLive>,
  opts: {
    captain?: number | null
    vice?: number | null
    benchBoost?: boolean
    tripleCaptain?: boolean
  } = {},
): Autosubs {
  let xi = [...starting]
  const pen = [...bench]
  const notes: string[] = []
  const { captain = null, vice = null, benchBoost = false, tripleCaptain = false } = opts

  if (benchBoost) {
    notes.push('Bench Boost is active: all 15 score and no substitutions are made.')
    const [cap, src, mult] = armband(captain, vice, live, tripleCaptain)
    return {
      xi, bench: pen, subs_in: [], subs_out: [], captain: cap,
      captain_source: src, multiplier: mult,
      provisional: anyUnfinished(live, [...xi, ...pen]), notes,
    }
  }

  const out = (pid: number): boolean => {
    const st = live.get(pid)
    return !!st && st.finished && st.minutes === 0
  }
  const cameOn = (pid: number): boolean => (live.get(pid)?.minutes ?? 0) > 0

  const subsIn: number[] = []
  const subsOut: number[] = []

  // --- goalkeeper: like for like only ---------------------------------
  const gkStart = xi.filter((p) => positions.get(p) === 'GKP')
  const gkBench = pen.filter((p) => positions.get(p) === 'GKP')
  for (const gk of gkStart) {
    if (out(gk) && gkBench.length > 0 && cameOn(gkBench[0])) {
      const replacement = gkBench[0]
      xi[xi.indexOf(gk)] = replacement
      pen[pen.indexOf(replacement)] = gk
      subsIn.push(replacement)
      subsOut.push(gk)
      notes.push('Goalkeeper substitution: only the bench keeper is eligible.')
    }
  }

  // --- outfield: bench order, formation must stay legal ----------------
  for (const slot of pen.filter((p) => positions.get(p) !== 'GKP')) {
    const blanks = xi.filter((p) => out(p) && positions.get(p) !== 'GKP')
    if (blanks.length === 0) break
    if (!cameOn(slot)) continue
    let made = false
    for (const blank of blanks) {
      const trial = xi.map((p) => (p === blank ? slot : p))
      const counts: Record<string, number> = {}
      for (const p of trial) {
        const key = positions.get(p) ?? '?'
        counts[key] = (counts[key] ?? 0) + 1
      }
      if (legal(counts)) {
        xi = trial
        pen[pen.indexOf(slot)] = blank
        subsIn.push(slot)
        subsOut.push(blank)
        made = true
        break
      }
    }
    if (!made) {
      notes.push(
        `Bench player ${slot} could not come on: no substitution keeps the ` +
        'formation legal.',
      )
    }
  }

  if (subsIn.length === 0 && starting.some(out)) {
    notes.push('A starter blanked but no legal replacement played.')
  }

  const [cap, src, mult] = armband(captain, vice, live, tripleCaptain)
  if (src === 'vice') {
    notes.push('Captain recorded no minutes, so the armband passed to the vice-captain.')
  } else if (src === 'none') {
    notes.push(
      'Captain and vice both recorded no minutes: no player is multiplied this gameweek.',
    )
  }

  return {
    xi, bench: pen, subs_in: subsIn, subs_out: subsOut, captain: cap,
    captain_source: src, multiplier: mult,
    provisional: anyUnfinished(live, [...xi, ...pen]), notes,
  }
}

// ---------------------------------------------------------------------------
// Squad scoring
// ---------------------------------------------------------------------------

export interface SquadLive {
  entry_id: number | null
  confirmed: number
  provisional: number
  predicted: number
  benchPoints: number
  playersPlayed: number
  playersYetToPlay: number
  autosubs: Autosubs
  baseline: number
  hits: number
  /**
   * Every player whose points actually land in this total. Under Bench Boost
   * that is all fifteen, which is exactly why it exists: `autosubs.xi` is
   * eleven names whatever the chip says, so anything reading it to answer
   * "who is scoring for this manager" is wrong in the one week a bench is the
   * whole point. Recorded once, by the scorer that already knows.
   */
  scoring: number[]
}

export function squadCurrent(s: SquadLive): number {
  return s.confirmed + s.provisional - s.hits
}
export function squadProjected(s: SquadLive): number {
  return squadCurrent(s) + s.predicted
}

/** Score one entry from the live state. Used for you AND every rival — both
 *  sides must be passed the same `live` map, or the two could disagree about a
 *  goal that had just gone in. */
export function scoreSquad(
  starting: number[],
  bench: number[],
  positions: Map<number, string>,
  live: Map<number, PlayerLive>,
  opts: {
    captain?: number | null
    vice?: number | null
    benchBoost?: boolean
    tripleCaptain?: boolean
    entryId?: number | null
    baseline?: number
    hits?: number
  } = {},
): SquadLive {
  const {
    captain = null, vice = null, benchBoost = false, tripleCaptain = false,
    entryId = null, baseline = 0, hits = 0,
  } = opts
  const subs = applyAutosubs(starting, bench, positions, live, {
    captain, vice, benchBoost, tripleCaptain,
  })
  const mult = subs.multiplier

  const scoring = [...subs.xi, ...(benchBoost ? subs.bench : [])]
  let confirmed = 0
  let provisional = 0
  let predicted = 0
  for (const pid of scoring) {
    const st = live.get(pid)
    if (!st) continue
    const m = pid === subs.captain ? mult : 1
    confirmed += st.confirmed * m
    provisional += st.provisional * m
    predicted += st.predicted * m
  }

  let benchPoints = 0
  if (!benchBoost) {
    for (const pid of subs.bench) {
      const st = live.get(pid)
      if (st) benchPoints += st.confirmed + st.provisional
    }
  }

  const relevant = [...subs.xi, ...subs.bench].map((p) => live.get(p))
  return {
    entry_id: entryId,
    confirmed,
    provisional,
    predicted,
    benchPoints,
    playersPlayed: relevant.filter((s) => s?.played).length,
    playersYetToPlay: relevant.filter((s) => s?.yetToPlay).length,
    autosubs: subs,
    baseline,
    hits,
    scoring,
  }
}

/**
 * Which live player has moved the league most, and by how much.
 *
 * What moves a mini-league is not ownership but EFFECTIVE ownership: how many
 * copies of a player's points land in your total against how many land in your
 * rival's. Owning him is only one way to differ. You both own him and only one
 * of you captained him is the other, and it is the commoner of the two — the
 * ownership-only version of this reported no swing at all for the single most
 * ordinary way a week turns.
 *
 * Measured against the closest rival, so it answers "what is deciding my week",
 * not "who scored the most points".
 *
 * C21. The candidates are each manager's SCORING set, not his XI. Under Bench
 * Boost all fifteen score, so reading `autosubs.xi` here — eleven names
 * whatever the chip says — made a rival's bench invisible to this function in
 * the one week his bench decides the league. The two managers are read
 * independently: only one of them may have played the chip.
 */
export function largestSwing(
  mine: SquadLive,
  rivals: SquadLive[],
  live: Map<number, PlayerLive>,
  names?: Map<number, string>,
): { player_id: number; name: string; swing: number; in_your_xi: boolean; against: number | null; note: string } | null {
  if (rivals.length === 0) return null
  let closest = rivals[0]
  let bestGap = Infinity
  for (const r of rivals) {
    const gap = Math.abs(r.baseline + squadCurrent(r) - (mine.baseline + squadCurrent(mine)))
    if (gap < bestGap) {
      bestGap = gap
      closest = r
    }
  }
  const mineIds = new Set(mine.scoring)
  const theirIds = new Set(closest.scoring)

  /** How many copies of this player's points land in `squad`'s total. */
  const weight = (pid: number, squad: SquadLive, ids: Set<number>): number => {
    if (!ids.has(pid)) return 0
    return pid === squad.autosubs.captain ? squad.autosubs.multiplier : 1
  }

  // Ascending id, so an exact tie resolves to the lowest id in both
  // implementations. Set iteration order does not do that on either side:
  // CPython lays small ints out by value modulo the table size, so
  // `{9, 40} ^ set()` comes out [40, 9] while this sorted pass answers 9.
  const candidates = [...new Set([...mineIds, ...theirIds])].sort((a, b) => a - b)

  let bestPid: number | null = null
  let bestDelta = 0
  for (const pid of candidates) {
    const st = live.get(pid)
    if (!st || !(st.confirmed || st.provisional)) continue
    const edge = weight(pid, mine, mineIds) - weight(pid, closest, theirIds)
    if (edge === 0) continue          // he scores identically for both of you
    const delta = (st.confirmed + st.provisional) * edge
    if (Math.abs(delta) > Math.abs(bestDelta)) {
      bestPid = pid
      bestDelta = delta
    }
  }
  if (bestPid === null) return null
  const shared = mineIds.has(bestPid) && theirIds.has(bestPid)
  return {
    player_id: bestPid,
    name: names?.get(bestPid) ?? String(bestPid),
    swing: round2(bestDelta),
    // Named for the ordinary case, and it keeps that name because it is in the
    // published artifact. It means "he is scoring for you", which under Bench
    // Boost includes your bench.
    in_your_xi: mineIds.has(bestPid),
    against: closest.entry_id,
    note: shared
      ? 'a player you both own but captain differently'
      : mineIds.has(bestPid)
        ? 'a differential you own'
        : 'a differential your closest rival owns',
  }
}
