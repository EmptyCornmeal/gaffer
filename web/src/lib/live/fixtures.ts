// Fixture state and provisional bonus — a port of `src/gaffer/live.py`.
//
// The Live page used to poll `data/live.json`, a static artifact regenerated
// three times a day, so it could not move while football was being played. These
// functions let the browser compute the same thing from the live endpoints once
// a minute.
//
// Python is the reference implementation. Both are driven from
// `tests/fixtures/live/cases.json` and must agree exactly — see
// `tests/test_live_parity.py` and `parity.test.ts`. Keep the function names and
// the order of the checks identical to the Python so a reader can diff them.

import type { FixtureStateName } from '../weekly'

export const LIVE_VERSION = 'live-1.0'

export const STATE_SCHEDULED = 'scheduled'
export const STATE_LIVE = 'live'
export const STATE_HALF_TIME = 'half_time'
export const STATE_AWAITING_BONUS = 'awaiting_bonus'
export const STATE_FINISHED = 'finished'
export const STATE_POSTPONED = 'postponed'
export const STATE_ABANDONED = 'abandoned'

/** A started fixture still incomplete this long after kick-off is not "live". */
export const ABANDON_AFTER_MS = 4 * 60 * 60 * 1000

/** FPL's bonus ladder. Ties share the higher award and consume the ranks below. */
export const BONUS_LADDER = [3, 2, 1] as const

export const UNAVAILABLE_NO_GAMEWEEK = 'no_gameweek'
export const UNAVAILABLE_NOT_STARTED = 'not_started'
export const UNAVAILABLE_NO_SQUAD = 'no_squad'
export const UNAVAILABLE_NO_LIVE_DATA = 'no_live_data'

export interface RawFixture {
  id?: number
  event?: number | null
  team_h?: number
  team_a?: number
  minutes?: number | null
  started?: boolean
  finished?: boolean
  finished_provisional?: boolean
  kickoff_time?: string | null
  stats?: { identifier?: string; h?: BpsRow[]; a?: BpsRow[] }[]
}
interface BpsRow {
  value?: number | null
  element?: number
}

export interface FixtureState {
  id: number
  event: number | null
  team_h: number
  team_a: number
  state: FixtureStateName
  minutes: number
  kickoff: string | null
  started: boolean
  finished: boolean
  finishedProvisional: boolean
}

/**
 * `round(x, 2)` as CPython does it — which is not "scale by 100, then round".
 *
 * Python rounds the *binary* value the double actually holds, half to even.
 * `Math.round` is half-away-from-zero, so `round(0.125, 2)` is 0.12 in Python
 * and 0.13 in JS: exactly the kind of one-pence disagreement that makes two
 * implementations look broken when only their rounding differs.
 *
 * Scaling by 100 first fixes the tie rule and introduces a worse error, because
 * the multiplication rounds too. The literal 2.675 is really
 * 2.67499999999999982…, so Python answers 2.67; `2.675 * 100` is exactly 267.5,
 * so a half-to-even rule on the scaled value answers 2.68. Over 220,000 sampled
 * values the scaled form disagreed with CPython on about 4% of them.
 *
 * `toFixed` is specified to round the exact value of the double, so it settles
 * every case except a genuine tie, where it takes the larger n and Python takes
 * the even one. At two decimals a genuine tie is only possible when the double
 * is an exact odd multiple of an eighth (0.125, 0.375, 7.625 …); every other
 * value is strictly above or below the halfway point. So that is the only case
 * handled separately, and `x * 100` inside it is exact for anything this
 * application will ever hold.
 *
 * One disagreement is left, and is a limit of the boundary rather than of this
 * function: for a value in (-0.005, 0) Python returns -0.0 and `json.dumps`
 * writes `-0.0`, while `JSON.stringify(-0)` writes `0`. Nothing here can make
 * those agree, so -0 is normalised to +0 and the gap is stated rather than
 * claimed away.
 */
export function round2(x: number): number {
  if (!Number.isFinite(x)) return x
  const eighths = x * 8
  if (Number.isInteger(eighths) && Math.abs(eighths % 2) === 1) {
    const lower = Math.floor(x * 100)          // exactly halfway: go to even
    return (lower % 2 === 0 ? lower : lower + 1) / 100 + 0
  }
  return Number(x.toFixed(2)) + 0
}

export function parseTime(raw: unknown): Date | null {
  if (typeof raw !== 'string' || !raw.trim()) return null
  const ms = Date.parse(raw.trim())
  return Number.isNaN(ms) ? null : new Date(ms)
}

/**
 * True once this match's bonus is settled and inside `total_points`.
 *
 * Not `finished` alone. A1: FPL flips a fixture's `finished` only when the WHOLE
 * event is processed, so the flag is per-gameweek wearing a per-fixture name.
 * Read live on 2026-08-31: GW1's ten fixtures were all
 * `(finished=true, finished_provisional=true)`, while GW2's nine played fixtures
 * were all `(finished=false, finished_provisional=true)` three days after they
 * were played, held there by one straggler still to come. So matches sat in
 * AWAITING_BONUS for days while `provisionalBonus` kept computing a BPS award
 * for bonus FPL had settled long before and already folded into the live row.
 *
 * Kept identical to `FixtureState.bonus_final` in src/gaffer/live.py.
 */
export function bonusFinal(s: FixtureState): boolean {
  return s.finished || s.finishedProvisional
}

/**
 * Has this fixture reached a point where a 0-minute player is out?
 * Only once the match is over — mid-match a benched player may still come on, so
 * autosubbing him would be guessing.
 *
 * A postponed fixture counts too. It is over in the only sense this question
 * asks about: it will not be played inside this gameweek, so a player left in it
 * has blanked and FPL substitutes him. Leaving postponed out kept those players
 * permanently mid-match, which is why a postponed captain never handed the
 * armband to the vice.
 */
export function countsAsPlayed(s: FixtureState): boolean {
  return s.state === STATE_AWAITING_BONUS || s.state === STATE_FINISHED
    || s.state === STATE_POSTPONED
}

/**
 * Derive a single, unambiguous state from FPL's four overlapping flags.
 * Same order of checks as `classify_fixture` in live.py.
 */
export function classifyFixture(raw: RawFixture, now: Date): FixtureState {
  const event = raw.event ?? null
  const ko = parseTime(raw.kickoff_time)
  const started = !!raw.started
  const finished = !!raw.finished
  const prov = !!raw.finished_provisional
  const minutes = Number(raw.minutes ?? 0) || 0

  let state: FixtureStateName
  if (event === null || ko === null) state = STATE_POSTPONED
  else if (finished) state = STATE_FINISHED
  else if (prov || (started && minutes >= 90)) state = STATE_AWAITING_BONUS
  else if (started) {
    if (now.getTime() - ko.getTime() > ABANDON_AFTER_MS) state = STATE_ABANDONED
    else if (minutes === 45) state = STATE_HALF_TIME
    else state = STATE_LIVE
  } else state = STATE_SCHEDULED

  return {
    id: Number(raw.id ?? 0) || 0,
    event,
    team_h: Number(raw.team_h ?? 0) || 0,
    team_a: Number(raw.team_a ?? 0) || 0,
    state,
    minutes,
    kickoff: ko === null ? null : ko.toISOString().replace(/\.\d{3}Z$/, '+00:00'),
    started,
    finished,
    finishedProvisional: prov,
  }
}

/** Every fixture belonging to `gw`, keyed by fixture id. */
export function fixtureStates(
  fixtures: RawFixture[] | null | undefined,
  gw: number,
  now: Date,
): Map<number, FixtureState> {
  const out = new Map<number, FixtureState>()
  for (const f of fixtures ?? []) {
    if (f.event !== gw) continue
    out.set(Number(f.id ?? 0) || 0, classifyFixture(f, now))
  }
  return out
}

export function fixtureSummary(states: Map<number, FixtureState>) {
  const counts: Record<string, number> = {}
  for (const s of states.values()) counts[s.state] = (counts[s.state] ?? 0) + 1
  const all = [...states.values()]
  return {
    total: states.size,
    by_state: counts,
    all_finished:
      all.length > 0 &&
      all.every((s) => s.state === STATE_FINISHED || s.state === STATE_POSTPONED),
    bonus_final:
      all.length > 0 &&
      all.every(
        (s) =>
          bonusFinal(s) || s.state === STATE_POSTPONED || s.state === STATE_SCHEDULED,
      ),
  }
}

/**
 * Official BPS -> bonus allocation, ties included.
 *
 * The rule is not "top three get 3/2/1". Ties share the higher award and
 * *consume* the places below them: two tied on top both get 3 and the next
 * player gets 1, with no 2 awarded. Getting this wrong systematically
 * over-credits whoever is second — the player a live tool is most often asked
 * about.
 */
export function bonusFromBps(bps: Map<number, number>): Map<number, number> {
  const out = new Map<number, number>()
  if (bps.size === 0) return out
  const groups = new Map<number, number[]>()
  for (const [pid, score] of bps) {
    const key = Math.trunc(score)
    const g = groups.get(key)
    if (g) g.push(pid)
    else groups.set(key, [pid])
  }
  let rank = 0
  for (const score of [...groups.keys()].sort((a, b) => b - a)) {
    if (rank >= BONUS_LADDER.length) break
    const award = BONUS_LADDER[rank]
    const tied = groups.get(score) as number[]
    for (const pid of tied) out.set(pid, award)
    rank += tied.length // a tie of N consumes N places
  }
  return out
}

/** Extract per-player BPS from a fixture's `stats` block. */
export function fixtureBps(raw: RawFixture): Map<number, number> {
  for (const block of raw.stats ?? []) {
    if (block.identifier !== 'bps') continue
    const out = new Map<number, number>()
    for (const side of ['h', 'a'] as const) {
      for (const row of block[side] ?? []) {
        if (typeof row.element === 'number') {
          out.set(row.element, Number(row.value ?? 0) || 0)
        }
      }
    }
    return out
  }
  return new Map()
}

/**
 * Bonus FPL has NOT yet awarded, computed from live BPS.
 *
 * Skips fixtures whose bonus is already final — those points are in the live
 * endpoint's `total_points` and adding ours would double-count them.
 */
export function provisionalBonus(
  fixtures: RawFixture[] | null | undefined,
  states: Map<number, FixtureState>,
): Map<number, number> {
  const out = new Map<number, number>()
  for (const raw of fixtures ?? []) {
    const st = states.get(Number(raw.id ?? 0) || 0)
    if (!st || bonusFinal(st) || !st.started) continue
    for (const [pid, award] of bonusFromBps(fixtureBps(raw))) {
      out.set(pid, (out.get(pid) ?? 0) + award)
    }
  }
  return out
}
