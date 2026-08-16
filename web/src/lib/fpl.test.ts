import { afterEach, describe, expect, it } from 'vitest'
import { displayName, fpl, ProxyError, ProxyTimeoutError } from './fpl'

// U15: before the timeouts existed, a proxy that accepted a connection and then
// went quiet left these promises pending forever, so the pages' error branches
// were unreachable code. Every test here uses a distinct entry id, because
// fpl.ts caches by URL for five minutes and results would otherwise leak between
// them.

const realFetch = globalThis.fetch
afterEach(() => {
  globalThis.fetch = realFetch
})

/** What AbortSignal.timeout() rejects a fetch with, without waiting 12s for it. */
function hangs() {
  globalThis.fetch = (() =>
    Promise.reject(new DOMException('signal timed out', 'TimeoutError'))) as typeof fetch
}

function answers(status: number, body: unknown) {
  let calls = 0
  globalThis.fetch = (async () => {
    calls++
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    })
  }) as typeof fetch
  return () => calls
}

describe('proxy request timeouts', () => {
  it('turns a hung request into a rejection a caller can name', async () => {
    hangs()
    await expect(fpl.entry(9001)).rejects.toBeInstanceOf(ProxyTimeoutError)
  })

  it('is still a ProxyError, so callers testing only for that need no change', async () => {
    hangs()
    await expect(fpl.entry(9002)).rejects.toBeInstanceOf(ProxyError)
  })

  it('leaves an HTTP failure distinguishable from a timeout', async () => {
    answers(404, { detail: 'Not found' })
    const err = await fpl.entry(9003).catch((e) => e)
    expect(err).toBeInstanceOf(ProxyError)
    expect(err).not.toBeInstanceOf(ProxyTimeoutError)
  })

  it('does not cache a timeout as if it were a result', async () => {
    hangs()
    await expect(fpl.entry(9004)).rejects.toThrow()
    const calls = answers(200, { id: 9004 })
    await expect(fpl.entry(9004)).resolves.toEqual({ id: 9004 })
    expect(calls()).toBe(1)
  })
})

describe('displayName', () => {
  // League 271619 really serves this: FPL's own bytes carry the replacement
  // character, so the manager's original emoji is already gone upstream.
  it('drops a replacement character FPL baked into a team name', () => {
    expect(displayName('Mikel Farteta \uFFFD')).toBe('Mikel Farteta')
  })

  it('leaves a name FPL managed to keep intact alone', () => {
    expect(displayName('The \u00D8deyssey')).toBe('The \u00D8deyssey')
  })

  it('would rather show the raw name than an empty cell', () => {
    expect(displayName('\uFFFD')).toBe('\uFFFD')
  })
})
