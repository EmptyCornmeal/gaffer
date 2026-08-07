import { describe, it, expect, vi } from 'vitest'
import {
  nextDelay, isStale, initialState, applyResult, freshnessLabel, Poller,
  DEFAULTS,
} from './refresh'

// --------------------------------------------------------------------------
// Pacing
// --------------------------------------------------------------------------

describe('nextDelay', () => {
  const healthy = { consecutiveFailures: 0 }

  it('polls at the base interval when visible and active', () => {
    expect(nextDelay(healthy, { visible: true, active: true }))
      .toBe(DEFAULTS.intervalMs)
  })

  it('stops entirely when the tab is hidden, by default', () => {
    expect(nextDelay(healthy, { visible: false, active: true })).toBeNull()
  })

  it('polls slowly when hidden if a hidden interval is configured', () => {
    expect(nextDelay(healthy, { visible: false, active: true },
                     { hiddenIntervalMs: 300_000 })).toBe(300_000)
  })

  it('stops when there is nothing left to watch', () => {
    expect(nextDelay(healthy, { visible: true, active: false })).toBeNull()
  })

  it('backs off exponentially after failures', () => {
    const one = nextDelay({ consecutiveFailures: 1 }, { visible: true, active: true })
    const two = nextDelay({ consecutiveFailures: 2 }, { visible: true, active: true })
    expect(one).toBe(DEFAULTS.intervalMs * 2)
    expect(two).toBe(DEFAULTS.intervalMs * 4)
  })

  it('caps the backoff so it never disappears for hours', () => {
    expect(nextDelay({ consecutiveFailures: 30 }, { visible: true, active: true }))
      .toBe(DEFAULTS.maxBackoffMs)
  })
})

describe('isStale', () => {
  it('is stale before the first success', () => {
    expect(isStale(null, 1000)).toBe(true)
  })

  it('is fresh immediately after a success', () => {
    expect(isStale(1000, 1000)).toBe(false)
  })

  it('goes stale after the configured window', () => {
    expect(isStale(0, DEFAULTS.staleAfterMs + 1)).toBe(true)
    expect(isStale(0, DEFAULTS.staleAfterMs - 1)).toBe(false)
  })
})

// --------------------------------------------------------------------------
// State transitions
// --------------------------------------------------------------------------

describe('applyResult', () => {
  it('a success records the data and the timestamp', () => {
    const s = applyResult(initialState<number>(),
                          { ok: true, data: 7, at: 1000 })
    expect(s.status).toBe('ok')
    expect(s.data).toBe(7)
    expect(s.lastSuccess).toBe(1000)
    expect(s.consecutiveFailures).toBe(0)
  })

  it('a failure KEEPS the last good data and marks it stale', () => {
    const good = applyResult(initialState<number>(),
                             { ok: true, data: 7, at: 1000 })
    const bad = applyResult(good, { ok: false, error: 'boom', at: 2000 })
    expect(bad.data).toBe(7)          // <- the scoreboard is not blanked
    expect(bad.status).toBe('stale')
    expect(bad.lastError).toBe('boom')
    expect(bad.lastSuccess).toBe(1000)
  })

  it('a failure with nothing to keep is an error, not a stale blank', () => {
    const s = applyResult(initialState<number>(),
                          { ok: false, error: 'boom', at: 1000 })
    expect(s.status).toBe('error')
    expect(s.data).toBeNull()
  })

  it('failures accumulate and a success resets them', () => {
    let s = initialState<number>()
    s = applyResult(s, { ok: false, error: 'a', at: 1 })
    s = applyResult(s, { ok: false, error: 'b', at: 2 })
    expect(s.consecutiveFailures).toBe(2)
    s = applyResult(s, { ok: true, data: 1, at: 3 })
    expect(s.consecutiveFailures).toBe(0)
    expect(s.lastError).toBeNull()
  })
})

describe('freshnessLabel', () => {
  it('says so when there has never been an update', () => {
    expect(freshnessLabel(null, 1000)).toBe('never updated')
  })

  it('describes recent updates in useful units', () => {
    expect(freshnessLabel(1_000_000, 1_000_000)).toBe('just now')
    expect(freshnessLabel(0, 30_000)).toBe('30s ago')
    expect(freshnessLabel(0, 5 * 60_000)).toBe('5m ago')
    expect(freshnessLabel(0, 3 * 3_600_000)).toBe('3h ago')
  })
})

// --------------------------------------------------------------------------
// The poller
// --------------------------------------------------------------------------

describe('Poller', () => {
  it('never runs two requests at once', async () => {
    let resolve!: (v: number) => void
    let calls = 0
    const fetcher = () => {
      calls += 1
      return new Promise<number>((r) => (resolve = r))
    }
    const p = new Poller<number>(fetcher, () => {}, {}, () => true,
                                 () => true, () => 0)
    const first = p.tick()
    await p.tick()          // arrives while the first is outstanding
    expect(calls).toBe(1)   // <- dropped, not queued
    resolve(1)
    await first
    p.stop()
  })

  it('reports every transition to the consumer', async () => {
    const seen: string[] = []
    const p = new Poller<number>(async () => 5, (s) => seen.push(s.status),
                                 {}, () => true, () => true, () => 0)
    await p.tick()
    expect(seen).toEqual(['loading', 'ok'])
    p.stop()
  })

  it('a failing fetch leaves the previous data in place', async () => {
    let ok = true
    const states: Array<number | null> = []
    const p = new Poller<number>(
      async () => { if (!ok) throw new Error('down'); return 42 },
      (s) => states.push(s.data), {}, () => true, () => true, () => 0)
    await p.tick()
    ok = false
    await p.tick()
    expect(states[states.length - 1]).toBe(42)
    p.stop()
  })

  it('stop() prevents any further scheduling', async () => {
    vi.useFakeTimers()
    let calls = 0
    const p = new Poller<number>(async () => { calls += 1; return 1 },
                                 () => {}, { intervalMs: 10 })
    p.start()
    await vi.advanceTimersByTimeAsync(5)
    p.stop()
    await vi.advanceTimersByTimeAsync(1000)
    expect(calls).toBe(1)
    vi.useRealTimers()
  })

  it('does not schedule when inactive', async () => {
    vi.useFakeTimers()
    let calls = 0
    const p = new Poller<number>(async () => { calls += 1; return 1 },
                                 () => {}, { intervalMs: 10 }, () => false)
    p.start()
    await vi.advanceTimersByTimeAsync(1000)
    expect(calls).toBe(1, )   // the initial tick only
    p.stop()
    vi.useRealTimers()
  })
})
