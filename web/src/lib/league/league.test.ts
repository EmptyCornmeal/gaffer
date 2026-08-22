// Unit tests for `./squads`, `./grid` and `./ledger` — the League page's
// arithmetic. The reconciliation gate in `ledger` is the one that matters most:
// it is what stops a plausible-looking attribution that does not add up to FPL's
// own number from reaching the screen.

import { describe, expect, it } from 'vitest'
import {
  gatherSquads, grossPoints, reconciles, squadFromPicks,
  type ManagerSquad, type PicksPayload,
} from './squads'
import { columnLabels, edges, overlap, ownershipGrid } from './grid'
import { concentration, rivalLedger, type GwPoints } from './ledger'

// ---------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------

function pick(element: number, multiplier: number, over: Record<string, unknown> = {}) {
  return { element, multiplier, is_captain: false, is_vice_captain: false, ...over }
}

/** A plain squad: 1-11 starting, 12-15 benched, 9 captained. */
function payload(over: Partial<PicksPayload> = {}): PicksPayload {
  return {
    picks: [
      ...[1, 2, 3, 4, 5, 6, 7, 8, 10, 11].map((p) => pick(p, 1)),
      pick(9, 2, { is_captain: true }),
      pick(12, 0), pick(13, 0), pick(14, 0),
      pick(15, 0, { is_vice_captain: true }),
    ],
    automatic_subs: [],
    active_chip: null,
    entry_history: { points: 0, event_transfers_cost: 0 },
    ...over,
  }
}

/** Every player scores `n`, unless overridden. */
function pts(over: Record<number, number> = {}, n = 2): Map<number, number> {
  const m = new Map<number, number>()
  for (let i = 1; i <= 15; i++) m.set(i, over[i] ?? n)
  return m
}

function squad(entry: number, gw: number, over: Partial<PicksPayload> = {}) {
  const s = squadFromPicks(entry, gw, payload(over))
  if (!s) throw new Error('builder produced no squad')
  return s
}

// ---------------------------------------------------------------------------
// Reading a squad
// ---------------------------------------------------------------------------

describe('squadFromPicks', () => {
  it('takes the weight from FPL rather than re-deriving it', () => {
    const s = squad(1, 1)
    expect(s.weights.get(9)).toBe(2)      // captain
    expect(s.weights.get(1)).toBe(1)      // XI
    expect(s.weights.get(12)).toBe(0)     // bench
    expect(s.captain).toBe(9)
    expect(s.vice).toBe(15)
  })

  it('keeps the bench in `held` even though it scores nothing', () => {
    const s = squad(1, 1)
    expect(s.held.size).toBe(15)
    expect(s.held.has(12)).toBe(true)
    expect(s.weights.get(12)).toBe(0)
  })

  // The real shape of a Bench Boost payload, checked against a live one before
  // this was written: FPL sets multiplier 1 on all fifteen. Nothing in this file
  // needs to know what the chip is called.
  it('needs no special case for Bench Boost, because FPL has none', () => {
    const boosted = squadFromPicks(1, 1, payload({
      active_chip: 'bboost',
      picks: [
        ...[1, 2, 3, 4, 5, 6, 7, 8, 10, 11].map((p) => pick(p, 1)),
        pick(9, 2, { is_captain: true }),
        ...[12, 13, 14, 15].map((p) => pick(p, 1)),
      ],
    }))!
    expect(boosted.weights.get(12)).toBe(1)
    expect(grossPoints(boosted, pts())).toBe(2 * 14 + 4)
  })

  it('reads a Triple Captain at three copies', () => {
    const s = squadFromPicks(1, 1, payload({
      active_chip: '3xc',
      picks: [
        ...[1, 2, 3, 4, 5, 6, 7, 8, 10, 11].map((p) => pick(p, 1)),
        pick(9, 3, { is_captain: true }),
        pick(12, 0), pick(13, 0), pick(14, 0), pick(15, 0),
      ],
    }))!
    expect(s.weights.get(9)).toBe(3)
  })

  it('applies an automatic substitution in the direction FPL made it', () => {
    const s = squad(1, 1, { automatic_subs: [{ element_in: 12, element_out: 5 }] })
    expect(s.weights.get(5)).toBe(0)
    expect(s.weights.get(12)).toBe(1)
  })

  it('never lets a substitution carry the armband across', () => {
    // The captain blanking passes the armband to the VICE; it does not double
    // whoever came on for him.
    const s = squad(1, 1, { automatic_subs: [{ element_in: 12, element_out: 9 }] })
    expect(s.weights.get(12)).toBe(1)
    expect(s.weights.get(9)).toBe(0)
  })

  it('ignores a substitution naming a player this manager never held', () => {
    const s = squad(1, 1, { automatic_subs: [{ element_in: 99, element_out: 98 }] })
    expect(s.weights.has(99)).toBe(false)
  })

  it('is null before the deadline, when there are no picks to publish', () => {
    expect(squadFromPicks(1, 2, { picks: [] })).toBeNull()
    expect(squadFromPicks(1, 2, null)).toBeNull()
  })

  it('distinguishes an absent official score from a zero one', () => {
    expect(squad(1, 1, { entry_history: { points: 0 } }).officialPoints).toBe(0)
    expect(squad(1, 1, { entry_history: null }).officialPoints).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Reconciliation
// ---------------------------------------------------------------------------

describe('the reconciliation gate', () => {
  it('passes when our arithmetic equals FPL published score', () => {
    // Ten starters at 2, captain 9 doubled to 4, bench excluded.
    const s = squad(1, 1, { entry_history: { points: 24, event_transfers_cost: 0 } })
    expect(grossPoints(s, pts())).toBe(24)
    expect(reconciles(s, pts())).toBe(true)
  })

  it('fails when the official score lags, as it does mid-gameweek', () => {
    // Exactly the shape measured live in GW1: our figure carries provisional
    // bonus FPL has not yet written into the history row.
    const s = squad(1, 1, { entry_history: { points: 21, event_transfers_cost: 0 } })
    expect(reconciles(s, pts())).toBe(false)
  })

  it('fails rather than assumes when there is no official score at all', () => {
    expect(reconciles(squad(1, 1, { entry_history: null }), pts())).toBe(false)
  })

  it('compares gross of hits, because that is what FPL publishes', () => {
    const s = squad(1, 1, { entry_history: { points: 24, event_transfers_cost: 4 } })
    expect(reconciles(s, pts())).toBe(true)
    expect(s.hits).toBe(4)
  })
})

// ---------------------------------------------------------------------------
// The grid
// ---------------------------------------------------------------------------

describe('the ownership grid', () => {
  const mine = squad(1, 1)
  const sameXi = squad(2, 1)
  const differentCaptain = squadFromPicks(3, 1, payload({
    picks: [
      ...[2, 3, 4, 5, 6, 7, 8, 9, 10, 11].map((p) => pick(p, 1)),
      pick(1, 2, { is_captain: true }),
      pick(12, 0), pick(13, 0), pick(14, 0), pick(15, 0),
    ],
  }))!

  it('counts captaincy as two copies, not one owner', () => {
    const rows = ownershipGrid([mine, sameXi, differentCaptain], 1)
    const nine = rows.find((r) => r.playerId === 9)!
    expect(nine.held).toBe(3)
    expect(nine.ownership).toBeCloseTo(100)
    // two managers at x2 and one at x1 = 5 copies across 3 managers
    expect(nine.effectiveOwnership).toBeCloseTo((5 / 3) * 100)
    expect(nine.captains).toBe(2)
  })

  it('separates holding a player from him scoring for you', () => {
    const rows = ownershipGrid([mine], 1)
    const benched = rows.find((r) => r.playerId === 12)!
    expect(benched.held).toBe(1)
    expect(benched.scoring).toBe(0)
    expect(benched.effectiveOwnership).toBe(0)
  })

  it('names a player only you hold as your differential', () => {
    const other = squadFromPicks(2, 1, payload({
      picks: [
        ...[2, 3, 4, 5, 6, 7, 8, 10, 11, 20].map((p) => pick(p, 1)),
        pick(9, 2, { is_captain: true }),
        pick(12, 0), pick(13, 0), pick(14, 0), pick(15, 0),
      ],
    }))!
    const rows = ownershipGrid([mine, other], 1)
    expect(rows.find((r) => r.playerId === 1)!.yourDifferential).toBe(true)
    expect(rows.find((r) => r.playerId === 20)!.yourDifferential).toBe(false)
    expect(rows.find((r) => r.playerId === 20)!.yours).toBe(false)
  })

  it('orders by effective ownership and breaks every tie deterministically', () => {
    const a = ownershipGrid([mine, sameXi, differentCaptain], 1).map((r) => r.playerId)
    const b = ownershipGrid([differentCaptain, sameXi, mine], 1).map((r) => r.playerId)
    expect(a).toEqual(b)
    expect(a[0]).toBe(9)      // the most-captained player leads
  })

  it('is empty for an empty league rather than throwing', () => {
    expect(ownershipGrid([], 1)).toEqual([])
  })
})

describe('columnLabels', () => {
  // The case that made this exist: three-letter truncation put both Nats under
  // "NAT", in the one table whose job is telling managers apart.
  it('separates two managers who share a first name', () => {
    const out = columnLabels(new Map([[1, 'Nat Uttley'], [2, 'Nat Stubbs']]))
    expect(out.get(1)).toBe('NU')
    expect(out.get(2)).toBe('NS')
  })

  it('separates two managers who share a surname', () => {
    const out = columnLabels(new Map([[1, 'Myles Colling'], [2, 'Kevin Colling']]))
    expect(out.get(1)).toBe('MC')
    expect(out.get(2)).toBe('KC')
  })

  it('uses the first and last word of a three-part name', () => {
    expect(columnLabels(new Map([[1, 'Dylan Llyr Morgan']])).get(1)).toBe('DM')
  })

  it('widens the surname when initials alone still collide', () => {
    const out = columnLabels(new Map([[1, 'Sam Smith'], [2, 'Sara Sutton'],
                                      [3, 'Steve Sanchez']]))
    const labels = [...out.values()]
    expect(new Set(labels).size).toBe(3)
    expect(labels.every((l) => l.length === labels[0].length)).toBe(true)
  })

  it('falls back to a suffix for two managers with the identical name', () => {
    const out = columnLabels(new Map([[1, 'Alex Kim'], [2, 'Alex Kim']]))
    expect(new Set(out.values()).size).toBe(2)
  })

  it('handles a one-word name and an empty one without throwing', () => {
    const out = columnLabels(new Map([[1, 'Pele'], [2, '   ']]))
    expect(out.get(1)).toBe('PE')
    expect(out.get(2)).toBeTruthy()
  })

  it('always returns one label per manager, all distinct', () => {
    const names = new Map([
      [1, 'Myles Colling'], [2, 'Dylan Llyr Morgan'], [3, 'Kevin Colling'],
      [4, 'Hakan Duzel'], [5, 'Nat Uttley'], [6, 'Nat Stubbs'], [7, 'Jarek Ettl'],
    ])
    const out = columnLabels(names)
    expect(out.size).toBe(7)
    expect(new Set(out.values()).size).toBe(7)
  })
})

describe('overlap and edges', () => {
  it('scores an identical squad at 100%', () => {
    expect(overlap(squad(1, 1), squad(2, 1))).toBe(100)
  })

  it('finds only the players the two of you weight differently', () => {
    const mine = squad(1, 1)
    const theirs = squadFromPicks(2, 1, payload({
      picks: [
        ...[1, 2, 3, 4, 5, 6, 7, 8, 9, 11].map((p) => pick(p, 1)),
        pick(10, 2, { is_captain: true }),
        pick(12, 0), pick(13, 0), pick(14, 0), pick(15, 0),
      ],
    }))!
    const e = edges(mine, theirs)
    expect(e.get(9)).toBe(1)     // you captained him, they only own him
    expect(e.get(10)).toBe(-1)   // and the reverse
    expect(e.has(1)).toBe(false) // identical treatment is not an edge
  })
})

// ---------------------------------------------------------------------------
// The ledger
// ---------------------------------------------------------------------------

describe('the season ledger', () => {
  /** Two managers who differ only in the armband: mine on 9, theirs on 10. */
  function pair(gw: number, points: Map<number, number>) {
    const gross = (weights: Map<number, number>) => {
      let t = 0
      for (const [id, w] of weights) t += (points.get(id) ?? 0) * w
      return t
    }
    const mine = squad(1, gw)
    const theirs = squadFromPicks(2, gw, payload({
      picks: [
        ...[1, 2, 3, 4, 5, 6, 7, 8, 9, 11].map((p) => pick(p, 1)),
        pick(10, 2, { is_captain: true }),
        pick(12, 0), pick(13, 0), pick(14, 0), pick(15, 0),
      ],
    }))!
    // Stamp each side with the score our own arithmetic produces, so the pair
    // reconciles by construction and the test is about attribution.
    mine.officialPoints = gross(mine.weights)
    theirs.officialPoints = gross(theirs.weights)
    return { mine, theirs }
  }

  it('attributes the gap to the players that actually made it', () => {
    const points = pts({ 9: 12, 10: 3 })
    const { mine, theirs } = pair(1, points)
    const led = rivalLedger(2, new Map([[1, mine]]), new Map([[1, theirs]]),
                            new Map([[1, { gw: 1, points } as GwPoints]]))
    expect(led.counted).toEqual([1])
    expect(led.dropped).toEqual([])
    // You had one extra copy of a 12 and one fewer of a 3.
    expect(led.lines.find((l) => l.playerId === 9)!.delta).toBe(12)
    expect(led.lines.find((l) => l.playerId === 10)!.delta).toBe(-3)
    expect(led.gap).toBe(9)
  })

  it('reproduces the gap as the sum of its lines', () => {
    const points = pts({ 9: 12, 10: 3 })
    const { mine, theirs } = pair(1, points)
    const led = rivalLedger(2, new Map([[1, mine]]), new Map([[1, theirs]]),
                            new Map([[1, { gw: 1, points } as GwPoints]]))
    const summed = led.lines.reduce((s, l) => s + l.delta, 0)
    expect(summed).toBe(led.gap)
  })

  it('carries hits into the gap without inventing a player to blame', () => {
    const points = pts({ 9: 12, 10: 3 })
    const { mine, theirs } = pair(1, points)
    theirs.hits = 8
    const led = rivalLedger(2, new Map([[1, mine]]), new Map([[1, theirs]]),
                            new Map([[1, { gw: 1, points } as GwPoints]]))
    expect(led.gap).toBe(9 + 8)
    expect(led.lines.reduce((s, l) => s + l.delta, 0)).toBe(9)
  })

  it('drops a gameweek neither side can reconcile, and says which', () => {
    const points = pts({ 9: 12, 10: 3 })
    const { mine, theirs } = pair(1, points)
    mine.officialPoints = 999            // the lagging feed
    const led = rivalLedger(2, new Map([[1, mine]]), new Map([[1, theirs]]),
                            new Map([[1, { gw: 1, points } as GwPoints]]))
    expect(led.counted).toEqual([])
    expect(led.dropped).toEqual([1])
    expect(led.gap).toBe(0)
    expect(led.lines).toEqual([])
  })

  it('drops a gameweek when only ONE side reconciles', () => {
    const points = pts({ 9: 12, 10: 3 })
    const { mine, theirs } = pair(1, points)
    theirs.officialPoints = 999
    const led = rivalLedger(2, new Map([[1, mine]]), new Map([[1, theirs]]),
                            new Map([[1, { gw: 1, points } as GwPoints]]))
    expect(led.dropped).toEqual([1])
  })

  it('skips a gameweek with no points map rather than counting it as zero', () => {
    const points = pts({ 9: 12 })
    const { mine, theirs } = pair(1, points)
    const led = rivalLedger(2, new Map([[1, mine]]), new Map([[1, theirs]]), new Map())
    expect(led.counted).toEqual([])
    expect(led.dropped).toEqual([])
  })

  it('accumulates across gameweeks and keeps the biggest first', () => {
    const weeks = new Map<number, GwPoints>()
    const mineByGw = new Map<number, ManagerSquad>()
    const theirsByGw = new Map<number, ManagerSquad>()
    for (const gw of [1, 2, 3]) {
      const points = pts({ 9: 10, 10: 1 })
      const { mine, theirs } = pair(gw, points)
      weeks.set(gw, { gw, points })
      mineByGw.set(gw, mine)
      theirsByGw.set(gw, theirs)
    }
    const led = rivalLedger(2, mineByGw, theirsByGw, weeks)
    expect(led.counted).toEqual([1, 2, 3])
    expect(led.lines[0].playerId).toBe(9)
    expect(led.lines[0].delta).toBe(30)
    expect(led.lines[0].weeks).toBe(3)
    expect(led.gap).toBe(27)
  })

  it('does not count a week a differential blanked as a week he mattered', () => {
    const points = pts({ 9: 0, 10: 0 })
    const { mine, theirs } = pair(1, points)
    const led = rivalLedger(2, new Map([[1, mine]]), new Map([[1, theirs]]),
                            new Map([[1, { gw: 1, points } as GwPoints]]))
    expect(led.lines).toEqual([])
    expect(led.gap).toBe(0)
  })

  it('measures concentration against the lines, never above 100%', () => {
    const points = pts({ 9: 12, 10: 3 })
    const { mine, theirs } = pair(1, points)
    theirs.hits = 8
    const led = rivalLedger(2, new Map([[1, mine]]), new Map([[1, theirs]]),
                            new Map([[1, { gw: 1, points } as GwPoints]]))
    expect(concentration(led, 5)).toBe(100)
    expect(concentration(led, 1)).toBeCloseTo((12 / 15) * 100)
  })

  it('reports zero concentration for an empty ledger instead of dividing by it', () => {
    expect(concentration({ entry: 2, gap: 0, lines: [], counted: [], dropped: [] }, 3))
      .toBe(0)
  })
})

// ---------------------------------------------------------------------------
// Fetching
// ---------------------------------------------------------------------------

describe('gatherSquads', () => {
  it('keeps league order even when a manager in the middle fails', async () => {
    const out = await gatherSquads([10, 20, 30], 1, async (entry) => {
      if (entry === 20) throw new Error('500')
      return payload()
    })
    expect(out.squads.map((s) => s.entry)).toEqual([10, 30])
    expect(out.wanted).toBe(3)
    expect(out.unread).toBe(1)
  })

  it('counts a manager with no picks as read, not as missing', async () => {
    const out = await gatherSquads([10], 1, async () => ({ picks: [] }))
    expect(out.squads).toEqual([])
    expect(out.unread).toBe(0)
  })

  it('stops starting new reads once the phase budget is spent', async () => {
    let started = 0
    const out = await gatherSquads(
      [1, 2, 3, 4], 1,
      async () => {
        started += 1
        await new Promise((r) => setTimeout(r, 20))
        return payload()
      },
      { budgetMs: 5, concurrency: 1 },
    )
    expect(started).toBe(1)
    expect(out.unread).toBe(3)
  })
})
