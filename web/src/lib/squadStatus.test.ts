import { describe, expect, it } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  SQUAD_FETCH_FAILED, SQUAD_LOADED, SQUAD_MALFORMED, SQUAD_NOT_FOUND,
  SQUAD_NO_ENTRY_ID, SQUAD_NO_PUBLIC_YET, SQUAD_STALE,
  briefingCaveat, squadKnowledge, type SquadMeta,
} from './squadStatus'

// Production shipped an AI briefing ending "Trust Bruno with the armband and roll
// with this £100m XI as is — no hit needed" while meta.squad_status was
// `no_public_squad_yet`. These tests fix the rule: the caveat is owed whenever the
// squad on screen is not the reader's own, and it is decided from the status field
// rather than from the date.

const DEPLOYED_META = join(process.cwd(), 'public', 'data', 'meta.json')

const meta = (over: Partial<SquadMeta> = {}): SquadMeta => ({
  squad_status: SQUAD_NO_PUBLIC_YET,
  squad_status_reason: 'no gameweek deadline has passed yet, so FPL exposes no picks',
  squad_source_event: null,
  entry_name: 'The Ødeyssey',
  ...over,
})

/** Every mention of the reader's team must be negated, never asserted. */
const DISOWNED = /\bnot your (?:FPL )?(?:team|squad)\b/i

describe('squadKnowledge', () => {
  it.each([
    [SQUAD_LOADED, 'known'],
    [SQUAD_STALE, 'stale'],
    [SQUAD_NO_PUBLIC_YET, 'unknown'],
    [SQUAD_NOT_FOUND, 'unknown'],
    [SQUAD_FETCH_FAILED, 'unknown'],
    [SQUAD_MALFORMED, 'unknown'],
    [SQUAD_NO_ENTRY_ID, 'unknown'],
  ])('%s -> %s', (status, expected) => {
    expect(squadKnowledge(status)).toBe(expected)
  })

  it('treats a status this build has never seen as unknown, not as known', () => {
    for (const s of ['some_future_status', '', null, undefined, 'LOADED']) {
      expect(squadKnowledge(s)).toBe('unknown')
    }
  })
})

describe('briefingCaveat — the model briefing', () => {
  it('caveats when no squad is public yet', () => {
    const c = briefingCaveat(meta(), 'model')
    expect(c).not.toBeNull()
    expect(c!.tone).toBe('unknown')
    expect(c!.headline).toContain('model-built reference squad')
    expect(c!.headline).toContain('not your team')
  })

  it('says no personal transfer or hit recommendation is possible', () => {
    const c = briefingCaveat(meta(), 'model')!
    expect(c.body).toMatch(/transfer/i)
    expect(c.body).toMatch(/hit/i)
    expect(c.body).toMatch(/nothing below is[^.]*recommendation for you/i)
  })

  it('repeats the producer reason verbatim rather than paraphrasing it', () => {
    const c = briefingCaveat(meta(), 'model')!
    expect(c.reason).toBe('no gameweek deadline has passed yet, so FPL exposes no picks')
  })

  it('stays silent once the real squad is loaded', () => {
    expect(briefingCaveat(meta({ squad_status: SQUAD_LOADED }), 'model')).toBeNull()
  })

  it('caveats every non-loaded status, including mid-season failures', () => {
    for (const s of [SQUAD_NO_PUBLIC_YET, SQUAD_NOT_FOUND, SQUAD_FETCH_FAILED,
      SQUAD_MALFORMED, SQUAD_NO_ENTRY_ID, 'something_new']) {
      const c = briefingCaveat(meta({ squad_status: s }), 'model')
      expect(c, `status ${s} must be caveated`).not.toBeNull()
      expect(c!.tone).toBe('unknown')
    }
  })

  it('names the gameweek a stale squad came from', () => {
    const c = briefingCaveat(
      meta({ squad_status: SQUAD_STALE, squad_source_event: 7, squad_status_reason: 'fetch failed; showing the squad stored from GW7' }),
      'model',
    )!
    expect(c.tone).toBe('stale')
    expect(c.headline).toContain('GW7')
    expect(c.body).toMatch(/check it against your real team/i)
  })

  it('still caveats a stale squad with no recorded source event', () => {
    const c = briefingCaveat(meta({ squad_status: SQUAD_STALE, squad_source_event: null }), 'model')!
    expect(c.tone).toBe('stale')
    expect(c.headline).not.toContain('GWnull')
  })

  it('copes with a missing reason rather than printing "null"', () => {
    for (const r of [null, undefined, '', '   ']) {
      expect(briefingCaveat(meta({ squad_status_reason: r }), 'model')!.reason).toBeNull()
    }
  })

  it('disowns the squad explicitly rather than merely omitting the claim', () => {
    for (const s of [SQUAD_NO_PUBLIC_YET, SQUAD_NO_ENTRY_ID, SQUAD_FETCH_FAILED]) {
      const c = briefingCaveat(meta({ squad_status: s }), 'model')!
      expect(c.headline).toMatch(DISOWNED)
    }
  })
})

describe('briefingCaveat — a squad built in the Planner', () => {
  it('caveats a local plan while the FPL squad is unreadable', () => {
    const c = briefingCaveat(meta(), 'plan')
    expect(c).not.toBeNull()
    expect(c!.headline).toContain('built in the Planner')
    expect(c!.headline).toMatch(DISOWNED)
    expect(c!.body).toMatch(/free transfers/i)
  })

  it('stays silent when the real squad is loaded and a plan also exists', () => {
    expect(briefingCaveat(meta({ squad_status: SQUAD_LOADED }), 'plan')).toBeNull()
  })

  it('does not call a local plan a model-built reference squad', () => {
    expect(briefingCaveat(meta(), 'plan')!.headline).not.toContain('model-built')
  })
})

describe('the real published meta artifact', () => {
  it.runIf(existsSync(DEPLOYED_META))('drives a caveat from the deployed status', () => {
    const raw = JSON.parse(readFileSync(DEPLOYED_META, 'utf8')) as SquadMeta
    expect(typeof raw.squad_status).toBe('string')
    const c = briefingCaveat(raw, 'model')
    // Whatever the artifact says today, the two must agree: a caveat exactly
    // when the squad is not loaded.
    expect(c === null).toBe(squadKnowledge(raw.squad_status) === 'known')
    if (c) expect(c.headline.length).toBeGreaterThan(20)
  })
})
