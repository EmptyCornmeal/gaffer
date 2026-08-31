// Regressions in `./assemble.ts` that live in the SHAPE of what the fetch layer
// hands over rather than in the football: the manager's own entry arriving among
// his rivals, and the keys the published artifact has to carry. Whole-view
// agreement with the Python reference lives in `./parity.test.ts`.

import { describe, expect, it } from 'vitest'
import { assembleLive, type AssembleInput, type LiveRivalInput } from './assemble'
import type { RawFixture } from './fixtures'

const KO = '2026-08-22T14:00:00Z'
const NOW = new Date('2026-08-22T16:00:00Z')

const XI = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
const BENCH = [12, 13, 14, 15]
const POS: Record<number, string> = {
  1: 'GKP', 12: 'GKP',
  2: 'DEF', 3: 'DEF', 4: 'DEF', 13: 'DEF',
  5: 'MID', 6: 'MID', 7: 'MID', 8: 'MID', 14: 'MID',
  9: 'FWD', 10: 'FWD', 11: 'FWD', 15: 'FWD',
}
const POSITIONS = new Map<number, string>(
  Object.entries(POS).map(([k, v]) => [Number(k), v]))
const SQUAD_IDS = [...XI, ...BENCH]
const TEAM_OF = new Map<number, number>(SQUAD_IDS.map((p) => [p, 1]))
const NAMES = new Map<number, string>(SQUAD_IDS.map((p) => [p, `P${p}`]))

// The real entry ids from `data/live.json`, where this was found: eight rival
// rows for seven managers, 1066421 twice, and `largest_swing: null`.
const MINE = 1066421
const THEIRS = 3557534

const FIXTURE: RawFixture = {
  id: 1, event: 1, team_h: 1, team_a: 2, minutes: 70, started: true,
  finished: false, finished_provisional: false, kickoff_time: KO,
  stats: [{ identifier: 'bps', h: [{ value: 40, element: 9 }], a: [] }],
}

/** The manager's own entry, exactly as his mini-league standings hand it back. */
const SELF_AS_RIVAL: LiveRivalInput = {
  entry_id: MINE, name: 'Myles', starting: XI, bench: BENCH, captain: 9,
  vice: 10, total: 100, hits: 0, active_chip: null,
}
/** A real rival: he does not own the captained haul, and owns 15 instead. */
const RIVAL: LiveRivalInput = {
  entry_id: THEIRS, name: 'Rival',
  starting: [...XI.filter((p) => p !== 9), 15], bench: [12, 13, 14, 9],
  captain: 1, vice: 2, total: 95, hits: 0, active_chip: null,
}

/** A whole live view: one match in play, player 9 hauling and captained. */
function assembled(over: Partial<AssembleInput> = {}): Record<string, any> {
  return assembleLive({
    gw: 1,
    livePayload: {
      elements: SQUAD_IDS.map((p) => ({
        id: p, stats: { minutes: 90, total_points: p === 9 ? 12 : 2 },
      })),
    },
    fixturesPayload: [FIXTURE],
    squad: { starting: XI, bench: BENCH, captain: 9, vice: 10 },
    positions: POSITIONS, teamOf: TEAM_OF, now: NOW, names: NAMES,
    entryId: MINE, baseline: 100, hits: 0,
    rivals: [SELF_AS_RIVAL, RIVAL],
    ...over,
  }) as unknown as Record<string, any>
}

describe('the manager is not a rival to himself', () => {
  it('lists him in his own league table exactly once', () => {
    // He is a member of his own mini-league, so the standings return him, and
    // `assembleLive` prepends a synthetic "You" row on top of that.
    const table = assembled().rivals as Record<string, any>[]
    expect(table.filter((r) => r.entry_id === MINE)).toHaveLength(1)
    expect(table.filter((r) => r.you)).toHaveLength(1)
    expect(table).toHaveLength(2)
    expect(table.map((r) => r.provisional_position)).toEqual([1, 2])
  })

  it('still finds a swing, which the duplicate silently killed', () => {
    // `largestSwing` measures against the CLOSEST rival, and a duplicate of
    // yourself sits at distance zero. It won that contest every time, every
    // player then scored identically for both squads, `edge` was zero for all
    // of them, and the function returned null. Only `gatherRivals` in
    // ./source.ts dropping him kept the browser out of this.
    const withSelf = assembled()
    const clean = assembled({ rivals: [RIVAL] })

    expect(withSelf.largest_swing).not.toBeNull()
    expect(withSelf.largest_swing.player_id).toBe(9)
    expect(withSelf.largest_swing.against).toBe(THEIRS)
    expect(withSelf.largest_swing).toEqual(clean.largest_swing)
  })

  it('produces a swing for a league of genuinely differing squads', () => {
    // The property that can only fail silently, asserted on its own.
    // 12 confirmed + 3 provisional bonus, captained by him alone: 15 x (2 - 0).
    const swing = assembled({ rivals: [RIVAL] }).largest_swing
    expect(swing.swing).toBe(30)
    expect(swing.in_your_xi).toBe(true)
  })

  it('filters nobody when there is no entry to compare against', () => {
    // Matches `assemble` in src/gaffer/live.py, which guards on `entry_id is
    // not None`. Without an id, "himself" is not a thing that can be known.
    const table = assembled({ entryId: null }).rivals as Record<string, any>[]
    expect(table).toHaveLength(3)
  })
})

describe('the published view carries the manager own row', () => {
  it('agrees with his row in the league table', () => {
    // A6. `mcp_server.publish` reads `live["me"]`, and nothing ever wrote it —
    // so `me`, `autosubs` and `players_yet_to_play` all published null while
    // the numbers sat two lines away in `rivals`.
    const state = assembled()
    const me = state.me as Record<string, any>
    const row = (state.rivals as Record<string, any>[]).find((r) => r.you)!

    expect(Object.keys(me).sort()).toEqual([
      'current', 'entry_id', 'gw_points', 'projected', 'provisional_position',
      'substitutions', 'yet_to_play',
    ])
    expect(me.entry_id).toBe(MINE)
    expect(me.current).toBe(row.current)
    expect(me.projected).toBe(row.projected)
    expect(me.gw_points).toBe(row.gw_points)
    expect(me.yet_to_play).toBe(row.yet_to_play)
    expect(me.provisional_position).toBe(1)
    expect(me.substitutions).toEqual(state.squad.autosubs)
  })

  it('exists without a league at all', () => {
    const me = assembled({ rivals: [] }).me
    expect(me.entry_id).toBe(MINE)
    expect(me.provisional_position).toBe(1)
  })

  it('is absent when the view has no scores to publish', () => {
    const state = assembled({ squad: null })
    expect(state.available).toBe(false)
    expect(state.me).toBeUndefined()
  })
})

// B5. `rivals` was aggregates and nothing else, and no table anywhere holds a
// rival's picks — so "how are they doing", asked as often as "how am I doing",
// could only ever be answered with a number nobody could check. These mirror
// tests/test_live.py; whole-view agreement lives in ./parity.test.ts.
describe('every rival is published with the fifteen his total is made of', () => {
  it('carries a row per player, in the shape the artifact promises', () => {
    const squads = assembled().rival_squads as Record<string, any>[]
    expect(squads.map((s) => s.entry_id)).toEqual([THEIRS])
    expect(squads[0].players).toHaveLength(15)
    expect(Object.keys(squads[0].players[0]).sort()).toEqual([
      'confirmed', 'element', 'minutes', 'multiplier', 'name', 'pos',
      'predicted', 'product', 'provisional', 'yet_to_play',
    ])
  })

  it('adds up to the total published beside his name', () => {
    // The whole reason to spend the bytes: multiply, sum, subtract the hits,
    // and land exactly on the number in the league table.
    const state = assembled()
    const row = (state.rivals as Record<string, any>[]).find((r) => !r.you)!
    const rival = (state.rival_squads as Record<string, any>[])
      .find((s) => s.entry_id === row.entry_id)!
    const products = rival.players.reduce(
      (n: number, p: Record<string, any>) => n + p.product, 0)
    expect(products - rival.hits).toBe(rival.gw_points)
    expect(rival.gw_points).toBe(row.gw_points)
    expect(rival.yet_to_play).toBe(row.yet_to_play)
    expect(rival.provisional_position).toBe(row.provisional_position)
  })

  it('multiplies his captain and not yours', () => {
    // The multiplier is a fact about the MANAGER, not about the player: one
    // piece of football, two different contributions.
    const state = assembled()
    const rival = (state.rival_squads as Record<string, any>[])[0]
    const nine = rival.players.find((p: Record<string, any>) => p.element === 9)
    expect(nine.multiplier).toBe(0)
    expect(nine.product).toBe(0)
    const one = rival.players.find((p: Record<string, any>) => p.element === 1)
    expect(one.multiplier).toBe(2)
    const mine = (state.players as Record<string, any>[]).find((p) => p.id === 9)!
    expect(mine.is_captain).toBe(true)
  })

  it('publishes fifteen scoring rows for a rival on Bench Boost', () => {
    // `SquadLive.scoring` already knows, which is why the rows are built from
    // it rather than from the XI — the chip needs no case of its own.
    const rival = (assembled({ rivals: [{ ...RIVAL, active_chip: 'bboost' }] })
      .rival_squads as Record<string, any>[])[0]
    expect(rival.players.every((p: Record<string, any>) => p.multiplier >= 1))
      .toBe(true)
    const products = rival.players.reduce(
      (n: number, p: Record<string, any>) => n + p.product, 0)
    expect(products - rival.hits).toBe(rival.gw_points)
  })

  it('names only the players who score the two of you differently', () => {
    const rival = (assembled().rival_squads as Record<string, any>[])[0]
    expect(rival.differential).toContain(9)     // you captain him, he benched him
    expect(rival.differential).toContain(15)    // he starts him, you do not
    expect(rival.differential).not.toContain(2) // both of you, uncaptained
  })

  it('prices the swing player exactly as the rows price him', () => {
    // One rulebook, not two: `largestSwing` and the rows both go through
    // `effectiveMultiplier`, so a swing can never name a player the rows
    // disagree about.
    const state = assembled()
    const swing = state.largest_swing as Record<string, any>
    const rival = (state.rival_squads as Record<string, any>[])
      .find((s) => s.entry_id === swing.against)!
    const theirs = rival.players.find(
      (p: Record<string, any>) => p.element === swing.player_id)
    const mine = (state.players as Record<string, any>[])
      .find((p) => p.id === swing.player_id)!
    const myMult = mine.is_captain
      ? state.squad.autosubs.multiplier
      : (mine.in_xi ? 1 : 0)
    expect(swing.swing).toBe(
      (mine.confirmed + mine.provisional) * (myMult - (theirs?.multiplier ?? 0)))
  })

  it('is ordered by the table it takes its positions from', () => {
    const state = assembled({
      rivals: [RIVAL, { ...SELF_AS_RIVAL, entry_id: 999, name: 'Third', total: 1 }],
    })
    const squads = state.rival_squads as Record<string, any>[]
    const places = squads.map((s) => s.provisional_position)
    expect(places).toEqual([...places].sort((a, b) => a - b))
    const table = new Map((state.rivals as Record<string, any>[])
      .map((r) => [r.entry_id, r.provisional_position]))
    for (const s of squads) {
      expect(s.provisional_position).toBe(table.get(s.entry_id))
    }
  })

  it('is absent when the view has no scores to publish', () => {
    expect(assembled({ squad: null }).rival_squads).toBeUndefined()
  })
})
