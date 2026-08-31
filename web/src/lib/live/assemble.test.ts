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
