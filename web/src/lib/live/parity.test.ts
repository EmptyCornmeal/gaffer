import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { assembleLive, type AssembleInput } from './assemble'

// The same scenarios the Python reference is pinned against. Neither
// implementation owns the truth on its own: `gaffer.live` generates
// `expected`, and this asserts the browser produces exactly the same thing.
// See tests/test_live_parity.py for how to regenerate.
const CASES = new URL('../../../../tests/fixtures/live/cases.json', import.meta.url)
const cases: { name: string; input: Record<string, any>; expected: unknown }[] =
  JSON.parse(readFileSync(CASES, 'utf-8')).cases

function numberKeyed<T>(obj: Record<string, T> | null | undefined): Map<number, T> {
  return new Map(Object.entries(obj ?? {}).map(([k, v]) => [Number(k), v]))
}

function toInput(raw: Record<string, any>): AssembleInput {
  return {
    gw: raw.gw,
    livePayload: raw.live_payload,
    fixturesPayload: raw.fixtures_payload,
    squad: raw.squad,
    positions: numberKeyed<string>(raw.positions),
    teamOf: numberKeyed<number>(raw.team_of),
    now: new Date(raw.now),
    predictions: numberKeyed<number>(raw.predictions),
    rivals: raw.rivals ?? [],
    names: numberKeyed<string>(raw.names),
    entryId: raw.entry_id ?? null,
    baseline: raw.baseline ?? 0,
    hits: raw.hits ?? 0,
    activeChip: raw.active_chip ?? null,
    asOf: raw.as_of ?? null,
  }
}

describe('live scoring matches the Python reference', () => {
  it('has cases to check', () => {
    expect(cases.length).toBeGreaterThan(8)
  })

  for (const c of cases) {
    it(c.name, () => {
      // Round-trip so both sides are compared in the same value space.
      const got = JSON.parse(JSON.stringify(assembleLive(toInput(c.input))))
      expect(got).toEqual(c.expected)
    })
  }
})
