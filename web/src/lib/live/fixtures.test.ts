// Unit tests for the parts of `./fixtures.ts` that the shared case file cannot
// pin down: rounding, and when a fixture stops being able to deliver points.
// The whole-view agreement with Python lives in `./parity.test.ts`.

import { describe, expect, it } from 'vitest'
import {
  classifyFixture, countsAsPlayed, round2, STATE_POSTPONED, type RawFixture,
} from './fixtures'

// The same table as `ROUND2_TABLE` in tests/test_live.py, which asserts that
// CPython really does return these. Every input is one where scaling by 100 and
// then rounding parts company with `round(x, 2)`, or a tie where the rule
// itself decides.
const ROUND2_TABLE: [number, number][] = [
  [2.675, 2.67], [-2.675, -2.67], [1.115, 1.11], [3.145, 3.15],
  [0.615, 0.61], [10.235, 10.23], [0.005, 0.01], [-0.005, -0.01],
  [0.125, 0.12], [0.375, 0.38], [-0.125, -0.12], [-0.375, -0.38],
  [8.835, 8.84], [7.625, 7.62], [2.5, 2.5], [0, 0],
]

describe('round2 is what Python round(x, 2) returns', () => {
  for (const [value, expected] of ROUND2_TABLE) {
    it(`${value} rounds to ${expected}`, () => {
      expect(round2(value)).toBe(expected)
    })
  }

  it('leaves a non-finite value alone', () => {
    expect(round2(Number.NaN)).toBeNaN()
    expect(round2(Infinity)).toBe(Infinity)
  })

  it('never returns -0, which JSON.stringify would write as 0 anyway', () => {
    expect(Object.is(round2(-0.001), 0)).toBe(true)
  })
})

const KO = '2026-08-22T14:00:00Z'
const NOW = new Date('2026-08-22T16:00:00Z')

function fx(over: Partial<RawFixture> = {}): RawFixture {
  return {
    id: 1, event: 1, team_h: 1, team_a: 2, minutes: 0, started: false,
    finished: false, finished_provisional: false, kickoff_time: KO, stats: [],
    ...over,
  }
}

describe('countsAsPlayed', () => {
  it('is false while the match could still put a substitute on', () => {
    expect(countsAsPlayed(classifyFixture(fx({ started: true, minutes: 23 }), NOW)))
      .toBe(false)
  })

  it('is true once the match is over', () => {
    expect(countsAsPlayed(
      classifyFixture(fx({ started: true, minutes: 90, finished: true }), NOW)))
      .toBe(true)
  })

  it('is true for a postponed fixture, which nobody will score in', () => {
    const s = classifyFixture(fx({ kickoff_time: null }), NOW)
    expect(s.state).toBe(STATE_POSTPONED)
    expect(countsAsPlayed(s)).toBe(true)
  })
})
