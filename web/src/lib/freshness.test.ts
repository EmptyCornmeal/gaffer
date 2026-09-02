import { describe, expect, it } from 'vitest'
import {
  CRITICAL_BARS, classifyFreshness, DEADLINE_MAX_AGE_MS, FALLBACK_POLICY,
  freshnessWindow, LAGGING_BARS, SKEW_MS, STALE_BARS,
} from './freshness'

// Fixed reference time — never Date.now(), so these can't go green/red with the
// wall clock.
const NOW = Date.parse('2026-08-06T12:00:00Z')
const at = (msAgo: number) => new Date(NOW - msAgo).toISOString()

const HOUR = 3_600_000

// P0.6 -- the bands are multiples of the window's own bar, published by the
// pipeline, not flat wall-clock hours. The old model called 12 hours "fresh"
// with a refresh asked for every 15 minutes: 48 missed cycles, green. On
// 2026-09-01 that rendered 26 hours of consecutive publish failures as a mild
// amber chip beside a live recommendation.
const IDLE_BAR = FALLBACK_POLICY.max_age_min.idle * 60_000 // 6h

describe('classifyFreshness bands are multiples of the published bar', () => {
  it('is fresh inside one bar', () => {
    const f = classifyFreshness(at(IDLE_BAR - 60_000), NOW)
    expect(f.state).toBe('fresh')
  })

  it('is lagging just past one bar', () => {
    expect(classifyFreshness(at(IDLE_BAR * LAGGING_BARS + 60_000), NOW).state)
      .toBe('lagging')
  })

  it('is stale past two bars', () => {
    expect(classifyFreshness(at(IDLE_BAR * STALE_BARS + 60_000), NOW).state)
      .toBe('stale')
  })

  it('is critical past six bars', () => {
    expect(classifyFreshness(at(IDLE_BAR * CRITICAL_BARS + 60_000), NOW).state)
      .toBe('critical')
  })

  it('reports the real outage as critical', () => {
    const f = classifyFreshness('2026-07-26T15:53:50+00:00', NOW)
    expect(f.state).toBe('critical')
    expect(f.label).toBe('Updated 10d ago')
    expect(f.title).toContain('2026-07-26')
  })

  it('would NOT have called the 2026-09-01 outage merely stale', () => {
    // 26 hours, no deadline nearby: four idle bars.
    const f = classifyFreshness(at(26 * HOUR), NOW)
    expect(f.state).toBe('stale')
    // ...and the same age one hour before a deadline is not survivable.
    const f2 = classifyFreshness(at(26 * HOUR), NOW,
      new Date(NOW + HOUR).toISOString())
    expect(f2.state).toBe('critical')
    expect(f2.label).toMatch(/do not act/i)
  })

  it('names the window it judged against', () => {
    expect(classifyFreshness(at(HOUR), NOW).title).toMatch(/idle window/i)
  })
})

describe('freshnessWindow', () => {
  it('is idle with no deadline, and LOCKED just after one', () => {
    // Changed deliberately in 5.4. This case used to assert `idle` an hour
    // after a deadline, which is RM-G27 written down as an expectation: the
    // squad is locked, no football has started, and a six-hour staleness bar
    // sat over advice nobody could act on. A passed deadline is now its own
    // window until the gap closes.
    expect(freshnessWindow(NOW, null, FALLBACK_POLICY)).toBe('idle')
    expect(freshnessWindow(NOW, NOW - HOUR, FALLBACK_POLICY)).toBe('locked')
    expect(freshnessWindow(NOW, NOW - 12 * HOUR, FALLBACK_POLICY)).toBe('idle')
  })

  it('tightens as the deadline closes in', () => {
    expect(freshnessWindow(NOW, NOW + 72 * HOUR, FALLBACK_POLICY)).toBe('idle')
    expect(freshnessWindow(NOW, NOW + 5 * HOUR, FALLBACK_POLICY)).toBe('pre_deadline')
    expect(freshnessWindow(NOW, NOW + HOUR, FALLBACK_POLICY)).toBe('final_approach')
  })
})

describe('classifyFreshness degenerate inputs', () => {
  it('handles a missing timestamp', () => {
    for (const v of [null, undefined, '', '   ']) {
      const f = classifyFreshness(v as string | null | undefined, NOW)
      expect(f.state).toBe('unknown')
      expect(f.ageMs).toBeNull()
    }
  })

  it('handles an invalid timestamp', () => {
    const f = classifyFreshness('not-a-date', NOW)
    expect(f.state).toBe('unknown')
    expect(f.title).toContain('Unparseable')
  })

  it('tolerates small clock skew as fresh', () => {
    const f = classifyFreshness(at(-(SKEW_MS - 1000)), NOW)
    expect(f.state).toBe('fresh')
  })

  it('flags a future timestamp beyond skew rather than claiming freshness', () => {
    const f = classifyFreshness(at(-(SKEW_MS + 60_000)), NOW)
    expect(f.state).toBe('unknown')
    expect(f.label).toBe('Clock mismatch')
  })

  it('never labels stale data as current', () => {
    const f = classifyFreshness(at(50 * HOUR), NOW)
    expect(f.state).toBe('critical')
    expect(f.label).not.toMatch(/live|current|fresh/i)
  })
})

// The GW1 failure mode, exactly: the scheduled refresh drifts by up to an hour,
// so the last publish before a 17:30 deadline is realistically the 11:45 one —
// five and three quarter hours old, comfortably inside the 12h "fresh" band, and
// predating every team announcement that matters.
describe('deadline awareness', () => {
  const inHours = (h: number) => new Date(NOW + h * HOUR).toISOString()

  it('does not call five-hour-old data fresh half an hour before a deadline', () => {
    // final_approach bar is 20 minutes, so 5.75h is seventeen bars late.
    const f = classifyFreshness(at(5.75 * HOUR), NOW, inHours(0.5))
    expect(f.state).toBe('critical')
    expect(f.title).toMatch(/team-news/i)
    expect(f.label).toMatch(/do not act/i)
  })

  it('still calls recent data fresh near a deadline', () => {
    // Inside the 20-minute final-approach bar.
    expect(classifyFreshness(at(15 * 60_000), NOW, inHours(0.5)).state)
      .toBe('fresh')
  })

  it('calls half-hour-old data lagging in the final approach, not fresh', () => {
    // The pipeline targets a 20-minute bar here and runs every 15 minutes, so
    // 30 minutes IS late by the system's own standard. The old flat model
    // called this fresh because 30 minutes is nothing against a 12-hour band.
    // It is one and a half bars, and it reads quietly rather than in amber.
    const f = classifyFreshness(at(0.5 * HOUR), NOW, inHours(0.5))
    expect(f.state).toBe('lagging')
    expect(f.label).toMatch(/do not act/i)
  })

  it('applies the final-approach bar, not the idle one', () => {
    const soon = inHours(1)
    const bar = FALLBACK_POLICY.max_age_min.final_approach * 60_000 // 20m
    expect(classifyFreshness(at(bar - 60_000), NOW, soon).state).toBe('fresh')
    expect(classifyFreshness(at(bar + 60_000), NOW, soon).state).toBe('lagging')
    // The old flat model called anything under DEADLINE_MAX_AGE_MS (3h) fresh
    // here. Three hours is nine final-approach bars.
    expect(classifyFreshness(at(DEADLINE_MAX_AGE_MS), NOW, soon).state)
      .toBe('critical')
  })

  it('leaves the ordinary bands alone when the deadline is far away', () => {
    const f = classifyFreshness(at(5.75 * HOUR), NOW, inHours(72))
    expect(f.state).toBe('fresh')
    expect(f.label).not.toMatch(/do not act/i)
  })

  it('marks advice for a deadline that has already passed as expired', () => {
    const f = classifyFreshness(at(1 * HOUR), NOW, inHours(-1))
    expect(f.state).toBe('expired')
    expect(f.label).toBe('Deadline passed')
    expect(f.label).not.toMatch(/updated/i)
  })

  it('does not flip to expired inside the tolerated clock skew', () => {
    const f = classifyFreshness(at(1 * HOUR), NOW, new Date(NOW - SKEW_MS / 2).toISOString())
    expect(f.state).not.toBe('expired')
  })

  it('ignores an unparseable or absent deadline', () => {
    expect(classifyFreshness(at(1 * HOUR), NOW, 'not-a-date').state).toBe('fresh')
    expect(classifyFreshness(at(1 * HOUR), NOW, null).state).toBe('fresh')
    expect(classifyFreshness(at(1 * HOUR), NOW).state).toBe('fresh')
  })
})

describe("the locked window (RM-G27)", () => {
  const P = FALLBACK_POLICY
  const D = Date.parse("2026-09-05T11:00:00Z")

  it("names the gap between the deadline and the first kick-off", () => {
    // The client half of the same regression: `until <= 0` fell straight to
    // `idle`, so the browser put a six-hour bar over advice for a squad that
    // was already locked.
    expect(freshnessWindow(D + 60_000, D, P)).toBe("locked")
    expect(freshnessWindow(D + 89 * 60_000, D, P)).toBe("locked")
  })

  it("ends rather than running forever", () => {
    const past = D + (P.locked_window_min! + 1) * 60_000
    expect(freshnessWindow(past, D, P)).toBe("idle")
  })

  it("leaves the pre-deadline windows exactly as they were", () => {
    expect(freshnessWindow(D - 30 * 60_000, D, P)).toBe("final_approach")
    expect(freshnessWindow(D - 3 * 60 * 60_000, D, P)).toBe("pre_deadline")
    expect(freshnessWindow(D - 10 * 60 * 60_000, D, P)).toBe("idle")
  })

  it("carries a tighter bar than idle, which is the whole point", () => {
    expect(P.max_age_min.locked).toBeLessThan(P.max_age_min.idle)
  })

  it("falls back safely when the artifact predates the published window", () => {
    const old = { ...P, locked_window_min: undefined }
    expect(freshnessWindow(D + 60_000, D, old)).toBe("locked")
  })
})
