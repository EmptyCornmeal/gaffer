import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Player } from '../types'
import {
  baselineAndHits, baselineFromRow, fetchLive, gatherRivals, MAX_RIVALS,
  resetLiveCache, retryDelay,
} from './source'

// One controllable proxy for the whole file. `vi.hoisted` because the mock
// factories below are lifted above the imports and would otherwise close over
// a binding that does not exist yet.
const P = vi.hoisted(() => ({
  configured: true,
  entryId: 1 as number | null,
  leagueIds: [] as number[],
  picks: {} as Record<number, unknown>,
  history: null as unknown,
  historyError: null as Error | null,
  standings: null as unknown,
  standingsError: null as Error | null,
  live: null as unknown,
  fixtures: null as unknown,
  snapshot: null as unknown,
}))

vi.mock('../config', () => ({
  getEntryId: () => P.entryId,
  getLeagueIds: () => P.leagueIds,
}))
vi.mock('../data', () => ({ loadLiveSnapshot: async () => P.snapshot }))
vi.mock('../fpl', () => ({
  fpl: {
    configured: () => P.configured,
    picks: async (id: number) => {
      const v = P.picks[id]
      if (v === undefined) throw new Error(`no picks for entry ${id}`)
      return v
    },
    entryHistory: async () => {
      if (P.historyError) throw P.historyError
      return P.history
    },
    league: async () => {
      if (P.standingsError) throw P.standingsError
      return P.standings
    },
    live: async () => P.live,
    fixtures: async () => P.fixtures,
  },
}))

// Mirrors tests/test_live.py: `summary_overall_points` cannot supply the
// baseline, because once the gameweek starts scoring it already contains the
// points the live view is computing.
const HISTORY = {
  current: [
    { event: 1, points: 62, total_points: 62, event_transfers_cost: 0 },
    { event: 2, points: 51, total_points: 109, event_transfers_cost: 4 },
    { event: 3, points: 70, total_points: 179, event_transfers_cost: 0 },
    { event: 4, points: 40, total_points: 211, event_transfers_cost: 8 },
  ],
}

describe('baselineAndHits', () => {
  it('takes the cumulative total at the PREVIOUS event', () => {
    expect(baselineAndHits(HISTORY, 4)).toEqual({ baseline: 179, hits: 8 })
  })

  it('reads hits rather than assuming zero', () => {
    expect(baselineAndHits(HISTORY, 2).hits).toBe(4)
    expect(baselineAndHits(HISTORY, 3).hits).toBe(0)
  })

  it('has no baseline in the first gameweek', () => {
    expect(baselineAndHits(HISTORY, 1)).toEqual({ baseline: 0, hits: 0 })
  })

  it('rebuilds net of hits when there is no cumulative column', () => {
    const partial = {
      current: [
        { event: 1, points: 62, event_transfers_cost: 0 },
        { event: 2, points: 51, event_transfers_cost: 4 },
      ],
    }
    expect(baselineAndHits(partial, 3).baseline).toBe(109)
  })

  it('survives missing history', () => {
    expect(baselineAndHits(null, 5)).toEqual({ baseline: 0, hits: 0 })
    expect(baselineAndHits({}, 5)).toEqual({ baseline: 0, hits: 0 })
    expect(baselineAndHits({ current: [] }, 5)).toEqual({ baseline: 0, hits: 0 })
  })
})

// The same two numbers from the single row a picks payload carries, so a failed
// history read has an exact answer to fall back on instead of a plausible one.
// Mirrors `_baseline_from_row` in src/gaffer/pipeline.py.
describe('baselineFromRow', () => {
  it('undoes the gameweek that the cumulative total already contains', () => {
    // GW4 of the history above: 211 carried in, 40 scored, 8 paid.
    expect(baselineFromRow({ points: 40, total_points: 211, event_transfers_cost: 8 }))
      .toEqual({ baseline: 179, hits: 8 })
    expect(baselineAndHits(HISTORY, 4)).toEqual({ baseline: 179, hits: 8 })
  })

  it('refuses to guess when the row is not a history row', () => {
    expect(baselineFromRow(null)).toBeNull()
    expect(baselineFromRow({})).toBeNull()
    expect(baselineFromRow({ points: 40 })).toBeNull()
    expect(baselineFromRow({ total_points: 211 })).toBeNull()
  })

  it('treats an absent transfer cost as no hit, not as no row', () => {
    expect(baselineFromRow({ points: 70, total_points: 179 }))
      .toEqual({ baseline: 109, hits: 0 })
  })
})

// U33: twelve rival reads, each individually bounded at 12s by `fpl.ts`, used to
// run in series — ~144s of nothing on the first live load. These pin the phase
// deadline, the concurrency ceiling and, most importantly, what survives when
// only some of the reads land.

const SQUAD = Array.from({ length: 15 }, (_, i) => i + 1)

function payload(ids: number[]) {
  return {
    picks: ids.map((element, i) => ({
      element,
      position: i + 1,
      multiplier: i === 0 ? 2 : 1,
      is_captain: i === 0,
      is_vice_captain: i === 1,
    })),
    active_chip: null,
  }
}

describe('gatherRivals', () => {
  it('keeps standings order and skips your own entry', async () => {
    // Order is load-bearing: `largestSwing` takes the closest rival with a
    // strict `<`, so the first of two equally close managers is the one named.
    const rows = [
      { entry: 5, player_name: 'Ada', entry_name: 'Ada FC', total: 400 },
      { entry: 1, player_name: 'You', total: 390 },
      { entry: 9, entry_name: 'Nameless FC', total: 380 },
    ]
    const out = await gatherRivals(rows, 1, async () => payload(SQUAD))
    expect(out.rivals.map((r) => [r.entry_id, r.name, r.total])).toEqual([
      [5, 'Ada', 400],
      [9, 'Nameless FC', 380],
    ])
    expect(out).toMatchObject({ wanted: 2, unread: 0 })
  })

  it('never walks further than MAX_RIVALS into the table', async () => {
    const rows = Array.from({ length: 40 }, (_, i) => ({ entry: i + 2 }))
    let calls = 0
    const out = await gatherRivals(rows, 1, async () => {
      calls += 1
      return payload(SQUAD)
    })
    expect(calls).toBe(MAX_RIVALS)
    expect(out.rivals).toHaveLength(MAX_RIVALS)
  })

  it('keeps the rivals it could read when one of them fails', async () => {
    const rows = [{ entry: 2 }, { entry: 3 }, { entry: 4 }]
    const out = await gatherRivals(rows, 1, async (entry) => {
      if (entry === 3) throw new Error('500')
      return payload(SQUAD)
    })
    expect(out.rivals.map((r) => r.entry_id)).toEqual([2, 4])
    expect(out).toMatchObject({ wanted: 3, unread: 1 })
  })

  it('abandons the phase on its own deadline rather than twelve times over',
    async () => {
      const rows = Array.from({ length: 12 }, (_, i) => ({ entry: i + 2 }))
      const started = Date.now()
      const out = await gatherRivals(
        rows,
        1,
        async (entry) => (entry < 4 ? payload(SQUAD) : new Promise<never>(() => {})),
        { budgetMs: 30, concurrency: 4 },
      )
      // Four requests that never answer would hang forever without the phase
      // deadline; the two that did answer still have to come back.
      expect(Date.now() - started).toBeLessThan(2_000)
      expect(out.rivals.map((r) => r.entry_id)).toEqual([2, 3])
      expect(out).toMatchObject({ wanted: 12, unread: 10 })
    })

  it('holds the proxy to a few requests at a time', async () => {
    const rows = Array.from({ length: 10 }, (_, i) => ({ entry: i + 2 }))
    let inFlight = 0
    let peak = 0
    const out = await gatherRivals(rows, 1, async () => {
      inFlight += 1
      peak = Math.max(peak, inFlight)
      await new Promise((r) => setTimeout(r, 2))
      inFlight -= 1
      return payload(SQUAD)
    }, { concurrency: 3 })
    expect(peak).toBe(3)
    expect(out.rivals).toHaveLength(10)
  })

  it('counts a manager with no picks as answered, not as missing', async () => {
    // C14 in miniature: calling this "unread" would leave the locked snapshot
    // permanently incomplete, and the whole league would be re-read every few
    // minutes on behalf of someone who simply has no team this week.
    const rows = [{ entry: 2 }, { entry: 3 }]
    const out = await gatherRivals(rows, 1, async (entry) =>
      (entry === 3 ? { picks: [], active_chip: null } : payload(SQUAD)))
    expect(out.rivals.map((r) => r.entry_id)).toEqual([2])
    expect(out).toMatchObject({ wanted: 2, unread: 0 })
  })

  // C10. The standings `total` moves during the gameweek, so using it as the
  // season baseline adds this week's points to a figure that already holds
  // them; and a rival's hits were assumed to be zero, which makes a -8 week
  // read eight points better than it was. Both corrupt the same three things
  // at once: the table, the closest-rival choice and the swing.
  it('carries the total INTO the gameweek, not the one that moves during it',
    async () => {
      const rows = [{ entry: 5, player_name: 'Ada', total: 400, event_total: 60 }]
      const out = await gatherRivals(rows, 1, async () => payload(SQUAD))
      expect(out.rivals[0].total).toBe(340)
    })

  it('reads a rival hit from his own picks rather than assuming none',
    async () => {
      const rows = [{ entry: 5, player_name: 'Ada', total: 400, event_total: 56 }]
      const out = await gatherRivals(rows, 1, async () => ({
        ...payload(SQUAD),
        entry_history: { points: 64, total_points: 400, event_transfers_cost: 8 },
      }))
      expect(out.rivals[0].hits).toBe(8)
    })
})

// ---------------------------------------------------------------------------
// The whole fetch path.
//
// C9, C13 and C14 are three faces of one failure: a read did not land and the
// page carried on as though it had. They are exercised end to end against a
// proxy whose endpoints can be broken one at a time, because the substitution
// happens between the fetch and the assembly and is invisible to either alone.
// ---------------------------------------------------------------------------

const GW = 5
const KO = '2026-09-19T14:00:00Z'
const POS: Record<number, string> = {
  1: 'GKP', 2: 'DEF', 3: 'DEF', 4: 'DEF', 5: 'DEF', 6: 'MID', 7: 'MID',
  8: 'MID', 9: 'MID', 10: 'MID', 11: 'FWD', 12: 'GKP', 13: 'DEF', 14: 'MID',
  15: 'FWD',
}

const PLAYERS = SQUAD.map((id) => ({
  id,
  name: `P${id}`,
  pos: POS[id],
  team_id: id <= 8 ? 1 : 2,
  gw_xp: [{ gw: GW, xp: 3 }],
})) as unknown as Player[]

function liveElements(ids: number[]) {
  return {
    elements: ids.map((id) => ({
      id, stats: { minutes: 90, total_points: 2, bps: 0 },
    })),
  }
}

function fixturesPayload() {
  return [{
    id: 1, event: GW, team_h: 1, team_a: 2, minutes: 60, started: true,
    finished: false, finished_provisional: false, kickoff_time: KO, stats: [],
  }]
}

function myPicks(over: Record<string, unknown> = {}) {
  return { ...payload(SQUAD), ...over }
}

// GW5 in progress. 211 banked through GW4 and a -4 taken for this one, so the
// cumulative 237 already contains everything the live view is computing.
const HISTORY5 = {
  current: [
    ...HISTORY.current,
    { event: 5, points: 30, total_points: 237, event_transfers_cost: 4 },
  ],
}

beforeEach(() => {
  resetLiveCache()
  P.configured = true
  P.entryId = 1
  P.leagueIds = []
  P.picks = { 1: myPicks() }
  P.history = HISTORY5
  P.historyError = null
  P.standings = null
  P.standingsError = null
  P.live = liveElements(SQUAD)
  P.fixtures = fixturesPayload()
  P.snapshot = null
})

describe('fetchLive with a readable history', () => {
  it('scores on the total carried into the gameweek', async () => {
    const r = await fetchLive(GW, PLAYERS)
    const s = r.state as any
    expect(s.available).toBe(true)
    expect(s.squad.season_total_before).toBe(211)
    expect(s.squad.hits).toBe(4)
    expect(s.baseline_source).toBe('entry_history')
    expect(r.incomplete).toBeNull()
  })
})

describe('fetchLive when the entry history cannot be read (C9)', () => {
  it('recovers the exact baseline from the picks payload already in hand',
    async () => {
      P.historyError = new Error('history → 500')
      P.picks = {
        1: myPicks({
          entry_history: { points: 30, total_points: 237, event_transfers_cost: 4 },
        }),
      }
      const r = await fetchLive(GW, PLAYERS)
      const s = r.state as any
      // 237 is cumulative and net: it INCLUDES this week's 30 and its -4.
      expect(s.squad.season_total_before).toBe(211)
      expect(s.squad.hits).toBe(4)
      expect(s.baseline_source).toBe('picks_entry_history')
      expect(r.incomplete).toBeNull()
    })

  it('withholds the season total rather than reporting this gameweek as it',
    async () => {
      P.historyError = new Error('history → 500')
      const r = await fetchLive(GW, PLAYERS)
      const s = r.state as any
      expect(s.baseline_source).toBe('unavailable')
      expect(s.squad.season_total_before).toBeNull()
      expect(s.squad.season_total_projected).toBeNull()
      expect(r.incomplete).toContain('your season total so far')
    })

  it('drops a league table it cannot place you in', async () => {
    P.historyError = new Error('history → 500')
    P.leagueIds = [7]
    P.standings = {
      standings: {
        results: [
          { entry: 1, player_name: 'You', total: 237, event_total: 26 },
          { entry: 5, player_name: 'Ada', total: 400, event_total: 60 },
        ],
      },
    }
    P.picks = { 1: myPicks(), 5: myPicks() }
    const r = await fetchLive(GW, PLAYERS)
    const s = r.state as any
    expect(s.rivals).toEqual([])
    expect(s.largest_swing).toBeNull()
    expect(r.incomplete).toContain('the league table')
  })
})

describe('fetchLive when the live payload is short a squad player (C13)', () => {
  it('names the gap instead of passing a projection off as a live number',
    async () => {
      P.live = liveElements(SQUAD.filter((p) => p !== 7))
      const r = await fetchLive(GW, PLAYERS)
      const s = r.state as any
      expect(s.missing_players).toEqual([7])
      expect(r.incomplete).toContain('1 of your players')
    })

  it('says nothing when the payload is whole', async () => {
    const r = await fetchLive(GW, PLAYERS)
    expect(r.incomplete).toBeNull()
    expect((r.state as any).missing_players).toBeUndefined()
  })
})

describe('a failed read is not cached for the gameweek (C14)', () => {
  it('rebuilds an incomplete snapshot once its retry window has passed',
    async () => {
      let now = 1_700_000_000_000
      const clock = vi.spyOn(Date, 'now').mockImplementation(() => now)
      try {
        P.leagueIds = [7]
        P.standingsError = new Error('league → 500')
        expect((await fetchLive(GW, PLAYERS)).incomplete)
          .toContain('the league standings')

        // Inside the window the broken snapshot is reused, even though the
        // league would now answer — that is what stops a 60s poll re-reading
        // a league that is genuinely unreadable.
        now += 60_000
        P.standingsError = null
        P.standings = {
          standings: {
            results: [{ entry: 5, player_name: 'Ada', total: 400, event_total: 60 }],
          },
        }
        P.picks = { 1: myPicks(), 5: myPicks() }
        expect((await fetchLive(GW, PLAYERS)).incomplete)
          .toContain('the league standings')

        // Past it, and the blip has healed itself.
        now += 3 * 60_000
        const healed = await fetchLive(GW, PLAYERS)
        expect(healed.incomplete).toBeNull()
        expect((healed.state as any).rivals).toHaveLength(2)
      } finally {
        clock.mockRestore()
      }
    })

  it('widens the wait as the same read keeps failing', () => {
    // Three minutes, then six, then twelve, to a ceiling. A league that is down
    // for the afternoon then costs a handful of re-reads rather than one every
    // three minutes for five hours, on a proxy that is shared and
    // unauthenticated — and the ceiling is short enough that one which comes
    // back is picked up inside the same match.
    expect(retryDelay(0)).toBe(3 * 60_000)
    expect(retryDelay(1)).toBe(3 * 60_000)
    expect(retryDelay(2)).toBe(6 * 60_000)
    expect(retryDelay(3)).toBe(12 * 60_000)
    expect(retryDelay(9)).toBe(24 * 60_000)
  })

  it('does not re-read on the same schedule after a second failure', async () => {
    let now = 1_700_000_000_000
    const clock = vi.spyOn(Date, 'now').mockImplementation(() => now)
    try {
      P.leagueIds = [7]
      P.standingsError = new Error('league → 500')
      await fetchLive(GW, PLAYERS)          // first build: incomplete
      now += 4 * 60_000
      await fetchLive(GW, PLAYERS)          // past 3 min, rebuilt, still failing

      // The window is six minutes now, so four does not reach it even though
      // the league would answer.
      P.standingsError = null
      P.standings = {
        standings: {
          results: [{ entry: 5, player_name: 'Ada', total: 400, event_total: 60 }],
        },
      }
      P.picks = { 1: myPicks(), 5: myPicks() }
      now += 4 * 60_000
      expect((await fetchLive(GW, PLAYERS)).incomplete)
        .toContain('the league standings')

      now += 3 * 60_000
      expect((await fetchLive(GW, PLAYERS)).incomplete).toBeNull()
    } finally {
      clock.mockRestore()
    }
  })
})
