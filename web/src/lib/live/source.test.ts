import { describe, expect, it } from 'vitest'
import { baselineAndHits } from './source'

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
