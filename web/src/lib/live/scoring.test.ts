// Unit tests for `./scoring.ts`, kept deliberately in step with the Python ones
// in tests/test_live.py — same scenarios, same names, same numbers. The shared
// case file in ./parity.test.ts proves the two agree on a whole view; these
// prove they agree on the calendar cases that file does not reach: blanks,
// doubles, postponements and the league swing.

import { describe, expect, it } from 'vitest'
import { fixtureStates, provisionalBonus, type RawFixture } from './fixtures'
import {
  applyAutosubs, largestSwing, playerLive, scoreSquad, type PlayerLive,
} from './scoring'

const KO = '2026-08-22T14:00:00Z'
const NOW = new Date('2026-08-22T16:00:00Z')

function fx(over: Partial<RawFixture> = {}): RawFixture {
  return {
    id: 1, event: 1, team_h: 1, team_a: 2, minutes: 0, started: false,
    finished: false, finished_provisional: false, kickoff_time: KO, stats: [],
    ...over,
  }
}

function el(id: number, minutes = 0, points = 0) {
  return { id, stats: { minutes, total_points: points } }
}

const POS = new Map<number, string>([
  ...[1, 12].map((p) => [p, 'GKP'] as [number, string]),
  ...[2, 3, 4, 13].map((p) => [p, 'DEF'] as [number, string]),
  ...[5, 6, 7, 8, 14].map((p) => [p, 'MID'] as [number, string]),
  ...[9, 10, 11, 15].map((p) => [p, 'FWD'] as [number, string]),
])
const XI = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
const BENCH = [12, 13, 14, 15]

// Two clubs, exactly as tests/test_live.py splits them: 1-10 play for team 1,
// 11-20 for team 2.
const TEAMS = new Map<number, number>(
  Array.from({ length: 20 }, (_, i) => [i + 1, i + 1 <= 10 ? 1 : 2]))

function liveState(
  fixtures: RawFixture[],
  elements: ReturnType<typeof el>[],
  predictions: Record<number, number> = {},
  teams: Map<number, number> = TEAMS,
): Map<number, PlayerLive> {
  return playerLive(
    { elements }, fixtureStates(fixtures, 1, NOW), new Map(), teams,
    new Map(Object.entries(predictions).map(([k, v]) => [Number(k), v])))
}

/** Every squad player played 90 and scored 2, unless `over` says otherwise. */
function plive(over: Record<number, [number, number]> = {}): Map<number, PlayerLive> {
  const out = new Map<number, PlayerLive>()
  for (const p of [...XI, ...BENCH]) {
    const [confirmed, provisional] = over[p] ?? [2, 0]
    out.set(p, {
      id: p, minutes: 90, confirmed, provisional, predicted: 0,
      played: true, finished: true, yetToPlay: false, states: [],
    })
  }
  return out
}

// ---------------------------------------------------------------------------
// Blanks and postponements
// ---------------------------------------------------------------------------

describe('a gameweek that does not happen still ends', () => {
  it('a club without a fixture has blanked rather than stalled', () => {
    const pl = liveState(
      [fx({ started: true, minutes: 90, finished: true, team_a: 3 })],
      [el(1, 90, 6), el(11)], { 11: 4.5 })
    expect(pl.get(11)?.finished).toBe(true)
    expect(pl.get(11)?.yetToPlay).toBe(false)
    expect(pl.get(11)?.predicted).toBe(0)
  })

  it('a postponed fixture is over for the players in it', () => {
    const pl = liveState([fx({ kickoff_time: null })], [el(1)], { 1: 5 })
    expect(pl.get(1)?.finished).toBe(true)
    expect(pl.get(1)?.yetToPlay).toBe(false)
    expect(pl.get(1)?.predicted).toBe(0)
  })

  it('a blank gameweek starter is substituted', () => {
    const teams = new Map<number, number>(
      Array.from({ length: 15 }, (_, i) => [i + 1, i + 1 === 11 ? 2 : 1]))
    const pl = liveState(
      [fx({ started: true, minutes: 90, finished: true, team_a: 3 })],
      Array.from({ length: 15 }, (_, i) => el(i + 1, i + 1 === 11 ? 0 : 90, 2)),
      {}, teams)
    const a = applyAutosubs(XI, BENCH, POS, pl)
    expect(a.subs_out).toEqual([11])
    expect(a.subs_in).toEqual([13])
  })

  it('a blanking captain hands the armband over', () => {
    const pl = liveState(
      [fx({ started: true, minutes: 90, finished: true, team_a: 3 })],
      [...Array.from({ length: 10 }, (_, i) => el(i + 1, 90, 2)),
        ...Array.from({ length: 5 }, (_, i) => el(i + 11, 0, 0))])
    const a = applyAutosubs(XI, BENCH, POS, pl, { captain: 11, vice: 10 })
    expect(a.captain).toBe(10)
    expect(a.captain_source).toBe('vice')
    expect(a.multiplier).toBe(2)
  })
})

// ---------------------------------------------------------------------------
// Double gameweeks
// ---------------------------------------------------------------------------

describe('a double gameweek is two fixtures against one projection', () => {
  const played = fx({ id: 1, started: true, minutes: 90, finished: true, team_a: 3 })
  const toCome = fx({ id: 2, team_h: 4, team_a: 1 })

  it('keeps the projection for the fixture still to come', () => {
    const pl = liveState([played, toCome], [el(1, 90, 6)], { 1: 5 })
    expect(pl.get(1)?.predicted).toBeCloseTo(2.5, 10)
    expect(pl.get(1)?.yetToPlay).toBe(true)
  })

  it('does not credit a blank in the first with both fixtures', () => {
    const pl = liveState([played, toCome], [el(1, 0, 0)], { 1: 5 })
    expect(pl.get(1)?.predicted).toBeCloseTo(2.5, 10)
  })

  it('shares the projection for a player with no live row yet', () => {
    const pl = liveState([played, toCome], [], { 1: 5 })
    expect(pl.get(1)?.predicted).toBeCloseTo(2.5, 10)
  })

  it('keeps the whole projection while both fixtures are to come', () => {
    const pl = liveState(
      [fx({ id: 1, team_a: 3 }), fx({ id: 2, team_h: 4, team_a: 1 })],
      [el(1)], { 1: 5 })
    expect(pl.get(1)?.predicted).toBeCloseTo(5, 10)
  })

  it('projects nothing more for a player already on the pitch', () => {
    const pl = liveState([fx({ started: true, minutes: 60 })], [el(1, 60, 4)], { 1: 5 })
    expect(pl.get(1)?.predicted).toBe(0)
    expect(pl.get(1)?.yetToPlay).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// League swing
// ---------------------------------------------------------------------------

describe('the largest swing', () => {
  it('counts a shared player the two of you captain differently', () => {
    const st = plive({ 9: [20, 3] })
    const mine = scoreSquad(XI, BENCH, POS, st, { captain: 9, entryId: 1 })
    const theirs = scoreSquad(XI, BENCH, POS, st, { captain: 1, entryId: 2 })
    const swing = largestSwing(mine, [theirs], st, new Map([[9, 'Haaland']]))
    expect(swing).not.toBeNull()
    expect(swing?.player_id).toBe(9)
    expect(swing?.swing).toBe(23)
    expect(swing?.in_your_xi).toBe(true)
  })

  it('signs a captaincy swing against you when the rival doubled him', () => {
    const st = plive({ 9: [20, 3] })
    const mine = scoreSquad(XI, BENCH, POS, st, { captain: 1, entryId: 1 })
    const theirs = scoreSquad(XI, BENCH, POS, st, { captain: 9, entryId: 2 })
    expect(largestSwing(mine, [theirs], st)?.swing).toBe(-23)
  })

  it('reports nothing when the two of you score identically', () => {
    const st = plive({ 9: [20, 3] })
    const mine = scoreSquad(XI, BENCH, POS, st, { captain: 1, entryId: 1 })
    const theirs = scoreSquad(XI, BENCH, POS, st, { captain: 1, entryId: 2 })
    expect(largestSwing(mine, [theirs], st)).toBeNull()
  })

  // C21. A rival's Bench Boost bench used to be invisible here: the candidates
  // were `autosubs.xi`, which is eleven names however many are scoring.
  it("sees a rival's bench when he has bench-boosted and you have not", () => {
    const st = plive({ 15: [12, 0] })
    // 15 is on both benches. Only his is scoring.
    const mine = scoreSquad(XI, BENCH, POS, st, { captain: 1, entryId: 1 })
    const theirs = scoreSquad(XI, BENCH, POS, st, {
      captain: 1, entryId: 2, benchBoost: true,
    })
    const swing = largestSwing(mine, [theirs], st, new Map([[15, 'Mateta']]))
    expect(swing?.player_id).toBe(15)
    expect(swing?.swing).toBe(-12)
    expect(swing?.in_your_xi).toBe(false)
    expect(swing?.note).toBe('a differential your closest rival owns')
  })
  it('sees your own bench when you are the one who bench-boosted', () => {
    const st = plive({ 15: [12, 0] })
    const mine = scoreSquad(XI, BENCH, POS, st, {
      captain: 1, entryId: 1, benchBoost: true,
    })
    const theirs = scoreSquad(XI, BENCH, POS, st, { captain: 1, entryId: 2 })
    expect(largestSwing(mine, [theirs], st)?.swing).toBe(12)
  })
  it('reports nothing when both of you bench-boosted the same bench', () => {
    const st = plive({ 15: [12, 0] })
    const opts = { captain: 1, benchBoost: true }
    const mine = scoreSquad(XI, BENCH, POS, st, { ...opts, entryId: 1 })
    const theirs = scoreSquad(XI, BENCH, POS, st, { ...opts, entryId: 2 })
    expect(largestSwing(mine, [theirs], st)).toBeNull()
  })
  it('resolves an exact tie to the lowest player id, as Python now does', () => {
    const st = new Map<number, PlayerLive>([9, 40].map((p) => [p, {
      id: p, minutes: 90, confirmed: 5, provisional: 0, predicted: 0,
      played: true, finished: true, yetToPlay: false, states: [],
    }] as [number, PlayerLive]))
    const mine = scoreSquad([9, 40], [], POS, st, { entryId: 1 })
    const theirs = scoreSquad([], [], POS, st, { entryId: 2 })
    const swing = largestSwing(mine, [theirs], st)
    expect(swing?.swing).toBe(5)
    expect(swing?.player_id).toBe(9)
  })
})

// ==========================================================================
// W7 — provisional bonus is counted once, not twice
// ==========================================================================
//
// Observed live in GW2 on 2026-08-28, and this is the half of the rulebook the
// browser runs, so it is the half that was on screen. FPL's live `total_points`
// of 8 already contained Haaland's 3 provisional bonus (1 appearance + 4 goal +
// 3 bonus); the scorer added its own BPS-derived 3 on top and the armband
// doubled it, for 22 against an arithmetic ceiling of 16.
//
// Kept in step with test_live.py's tests of the same name, per the note at the
// top of this file.
describe('W7 — provisional bonus is counted once', () => {
  const W7_KO = '2026-08-28T19:00:00Z'
  const W7_NOW = new Date('2026-08-28T19:50:00Z')

  function _w7World(bonusInRow: number) {
    const fixture = fx({
      id: 77, event: 2, minutes: 45, started: true, kickoff_time: W7_KO,
      stats: [{
        identifier: 'bps',
        h: [{ value: 40, element: 9 }],
        a: [{ value: 10, element: 99 }],
      }],
    })
    const states = fixtureStates([fixture], 2, W7_NOW)
    const prov = provisionalBonus([fixture], states)
    const elements: ReturnType<typeof el>[] = []
    for (const pid of [...XI, ...BENCH]) {
      if (pid !== 9) elements.push(el(pid, 90, 2))
    }
    const nine = el(9, 45, 8) as ReturnType<typeof el> & {
      stats: { bonus?: number }
    }
    nine.stats.bonus = bonusInRow
    elements.push(nine)
    const teams = new Map([...XI, ...BENCH].map((p) => [p, 1]))
    return { prov, live: playerLive({ elements }, states, prov, teams) }
  }

  it('does not add bonus that is already in the live row', () => {
    const { prov, live } = _w7World(3)
    expect(prov.get(9)).toBe(3) // the BPS block does award him 3 — not the bug
    expect(live.get(9)!.confirmed).toBe(8)
    expect(live.get(9)!.provisional).toBe(0)
  })

  it('still supplies bonus the live row has not published', () => {
    const { live } = _w7World(0)
    expect(live.get(9)!.confirmed).toBe(8)
    expect(live.get(9)!.provisional).toBe(3)
  })

  it('never lets a captained total exceed the rows it is built from', () => {
    const { live } = _w7World(3)
    const subs = applyAutosubs(XI, BENCH, POS, live, { captain: 9, vice: 10 })
    const s = scoreSquad(XI, BENCH, POS, live, { captain: 9, vice: 10 })
    let expected = 0
    for (const pid of subs.xi) {
      expected += live.get(pid)!.confirmed * (pid === subs.captain ? subs.multiplier : 1)
    }
    expect(s.confirmed + s.provisional).toBe(expected)
    expect(s.confirmed + s.provisional).toBe(10 * 2 + 8 * 2) // 36; the bug shipped 42
  })
})
