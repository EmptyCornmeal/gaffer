import { describe, expect, it } from 'vitest'
import { classifyFreshness, FRESH_MS, SKEW_MS, STALE_MS } from './freshness'

// Fixed reference time — never Date.now(), so these can't go green/red with the
// wall clock.
const NOW = Date.parse('2026-08-06T12:00:00Z')
const at = (msAgo: number) => new Date(NOW - msAgo).toISOString()

const HOUR = 3_600_000

describe('classifyFreshness boundaries', () => {
  it('is fresh just under 12h', () => {
    const f = classifyFreshness(at(11 * HOUR + 59 * 60_000), NOW)
    expect(f.state).toBe('fresh')
    expect(f.label).toBe('Updated 11h ago')
  })

  it('flips to stale at exactly 12h', () => {
    expect(classifyFreshness(at(FRESH_MS), NOW).state).toBe('stale')
  })

  it('is still stale just under 36h', () => {
    const f = classifyFreshness(at(35 * HOUR + 59 * 60_000), NOW)
    expect(f.state).toBe('stale')
  })

  it('flips to critical at exactly 36h', () => {
    expect(classifyFreshness(at(STALE_MS), NOW).state).toBe('critical')
  })

  it('reports the real outage as critical', () => {
    // The shipped meta.json timestamp, against the audit date: 10d 20h.
    const f = classifyFreshness('2026-07-26T15:53:50+00:00', NOW)
    expect(f.state).toBe('critical')
    expect(f.label).toBe('Updated 10d ago')
    expect(f.title).toContain('2026-07-26')
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
