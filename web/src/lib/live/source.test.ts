import { describe, expect, it } from 'vitest'
import { baselineAndHits, gatherRivals, MAX_RIVALS } from './source'

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
})
