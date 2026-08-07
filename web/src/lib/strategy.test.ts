import { describe, it, expect } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  parseStrategy, simError, pct, epCost, departures, SUPPORTED,
  CLASS_LABELS, describeCoverage,
  type DataQuality, type Strategy,
} from './strategy'

function valid(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    strategy_version: 'strategy-1.0',
    league_version: 'league-1.0',
    multileague_version: 'multileague-1.0',
    chips_version: 'chips-1.0',
    generated_at: '2026-08-06T12:00:00+00:00',
    gameweek: 8,
    gameweeks_remaining: 31,
    simulation: { sim_version: 'scenarios-1.0', n_sims: 2000, seed: 1, model_version: 'm' },
    basis: 'your stored squad',
    squad: { starting: [], bench: [], captain: null, source_event: 7 },
    leagues: [],
    league_errors: [],
    options: [],
    resolution: { default: null, reason: 'nothing to resolve', shortlist: [], conflicts: [] },
    chips: {
      chips_version: 'chips-1.0', recommendation: 'hold', gameweek: null,
      expected_gain: 0, use_threshold: 4, alternatives: [], available: [],
      used: [], reason: 'no chip is available',
    },
    limitations: ['x'],
    ...overrides,
  }
}

function league(id: number, extra: Record<string, unknown> = {}) {
  return {
    league_id: id, name: `L${id}`, league_type: 'x', classification: 'tiny_private',
    size: 4, target_position: 1,
    posture: { stance: 'neutral', reason: 'level', variance_preference: 0 },
    placing: {
      p_first: 0.4, p_target: 0.4, target_position: 1, expected_position: 1.8,
      simulations: 2000, ci95_halfwidth: 0.021, basis: 'shared fixture scenarios',
      rival_coverage_pct: 100, caveats: [],
    },
    shields: [], differentials: [],
    data_quality: {
      rivals: 3, with_picks: 3, coverage_pct: 100, cohort_truncated: false,
      picks_source_event: 7, statuses: ['revealed'],
    },
    differs_from_neutral: false, difference_reason: '',
    ...extra,
  }
}

describe('parseStrategy', () => {
  it('accepts a well-formed artifact', () => {
    const s = parseStrategy(valid())
    expect(s.kind).toBe('ok')
  })

  it('reports a missing artifact rather than throwing', () => {
    expect(parseStrategy(null).kind).toBe('missing')
    expect(parseStrategy(undefined).kind).toBe('missing')
  })

  it('rejects a non-object', () => {
    expect(parseStrategy([]).kind).toBe('malformed')
    expect(parseStrategy('nope').kind).toBe('malformed')
  })

  it('surfaces a contained pipeline failure as failed, not missing', () => {
    const s = parseStrategy({ error: 'HTTPStatusError: 503' })
    expect(s.kind).toBe('failed')
    if (s.kind === 'failed') expect(s.detail).toContain('503')
  })

  it.each(['strategy_version', 'league_version', 'multileague_version', 'chips_version'])(
    'refuses an unrecognised %s',
    (field) => {
      const s = parseStrategy(valid({ [field]: 'something-99' }))
      expect(s.kind).toBe('unsupported')
      if (s.kind === 'unsupported') expect(s.detail).toContain(field)
    },
  )

  it('refuses a missing version outright', () => {
    const v = valid()
    delete v.league_version
    expect(parseStrategy(v).kind).toBe('unsupported')
  })

  it('requires the simulation to name how many scenarios it ran', () => {
    expect(parseStrategy(valid({ simulation: { n_sims: 0 } })).kind).toBe('malformed')
    expect(parseStrategy(valid({ simulation: undefined })).kind).toBe('malformed')
  })

  it('refuses a duplicated league', () => {
    const s = parseStrategy(valid({ leagues: [league(10), league(10)] }))
    expect(s.kind).toBe('malformed')
    if (s.kind === 'malformed') expect(s.detail).toContain('twice')
  })

  it('accepts several distinct leagues', () => {
    const s = parseStrategy(valid({ leagues: [league(10), league(20), league(30)] }))
    expect(s.kind).toBe('ok')
    if (s.kind === 'ok') expect(s.data.leagues).toHaveLength(3)
  })

  it('requires leagues and chips to have the right shape', () => {
    expect(parseStrategy(valid({ leagues: {} })).kind).toBe('malformed')
    expect(parseStrategy(valid({ chips: null })).kind).toBe('malformed')
  })

  it('names exactly the versions the python side emits', () => {
    expect(SUPPORTED.strategy).toContain('strategy-1.0')
    expect(SUPPORTED.league).toContain('league-1.0')
    expect(SUPPORTED.multileague).toContain('multileague-1.0')
    expect(SUPPORTED.chips).toContain('chips-1.0')
  })
})

describe('presentation helpers', () => {
  it('formats probabilities, and never invents one', () => {
    expect(pct(0.4)).toBe('40%')
    expect(pct(0.4123, 1)).toBe('41.2%')
    expect(pct(null)).toBe('—')
    expect(pct(NaN)).toBe('—')
  })

  it('shrinks simulation error with the square root of the sample', () => {
    expect(simError(2000)).toBeCloseTo(1.118, 2)
    expect(simError(8000)).toBeCloseTo(simError(2000) / 2, 3)
    expect(simError(0)).toBe(0)
  })
})

describe('the expected-points cost of a league-driven choice', () => {
  const s = {
    options: [
      { key: 'captain:1', label: 'A', expected_points: 62.0, p_target: {} },
      { key: 'captain:2', label: 'B', expected_points: 60.5, p_target: {} },
    ],
    leagues: [],
  } as unknown as Strategy

  it('is zero for the highest-expected-points option', () => {
    expect(epCost(s, 'captain:1')).toBeCloseTo(0)
  })

  it('names what a rank-protection move actually costs', () => {
    expect(epCost(s, 'captain:2')).toBeCloseTo(1.5)
  })

  it('is zero when there is nothing to compare', () => {
    expect(epCost({ options: [] } as unknown as Strategy, 'x')).toBe(0)
    expect(epCost(s, 'captain:missing')).toBe(0)
  })
})

describe('departures from the neutral recommendation', () => {
  it('lists only the leagues that actually want something different', () => {
    const s = parseStrategy(valid({
      leagues: [
        league(10),
        league(20, { differs_from_neutral: true, difference_reason: 'trailing' }),
      ],
    }))
    expect(s.kind).toBe('ok')
    if (s.kind === 'ok') {
      expect(departures(s.data).map((l) => l.league_id)).toEqual([20])
    }
  })
})

// The Strategy page shipped "Tiny private league - every rival readable" directly
// above "0/3 rival squads known". The label is chosen from league size before a
// single rival squad is fetched, so it never knew what was readable. These tests
// hold the two apart: classification says what kind of league it is, coverage
// says what we actually read.

const DEPLOYED = join(process.cwd(), 'public', 'data', 'strategy.json')

const dq = (over: Partial<DataQuality> = {}): DataQuality => ({
  rivals: 3, with_picks: 0, coverage_pct: 0, cohort_truncated: false,
  picks_source_event: null, statuses: ['no_public_picks_yet'], ...over,
})

/** Anything that would be a claim about how much of the field we can see. */
const READABILITY_CLAIM = /readable|every rival|all rivals|fully known|exact EO|complete coverage/i

describe('classification labels claim nothing about coverage', () => {
  it.each(Object.entries(CLASS_LABELS))('%s: %s', (_key, label) => {
    expect(label).not.toMatch(READABILITY_CLAIM)
  })

  it('still names the classification', () => {
    expect(CLASS_LABELS.tiny_private).toBe('Tiny private league')
    expect(Object.keys(CLASS_LABELS).sort()).toEqual(
      ['global', 'large', 'medium', 'small_private', 'tiny_private'],
    )
  })
})

describe('describeCoverage', () => {
  it('3 rivals, 0 known: says none are known and that they were modelled', () => {
    const c = describeCoverage(dq({ rivals: 3, with_picks: 0, coverage_pct: 0 }))
    expect(c.level).toBe('none')
    expect(c.summary).toBe('0 of 3 rival squads known')
    expect(c.meaning).toContain('modelled as a distribution')
    expect(c.summary + ' ' + c.meaning).not.toMatch(READABILITY_CLAIM)
  })

  it('3 rivals, 1 known: names the two unread', () => {
    const c = describeCoverage(dq({
      rivals: 3, with_picks: 1, coverage_pct: 33.3,
      statuses: ['revealed', 'no_public_picks_yet'],
    }))
    expect(c.level).toBe('partial')
    expect(c.summary).toBe('1 of 3 rival squads known')
    expect(c.meaning).toContain('other 2')
    expect(c.summary + ' ' + c.meaning).not.toMatch(READABILITY_CLAIM)
  })

  it('3 rivals, 2 known: uses the singular for one unread rival', () => {
    const c = describeCoverage(dq({ rivals: 3, with_picks: 2, coverage_pct: 66.7, statuses: ['revealed'] }))
    expect(c.level).toBe('partial')
    expect(c.meaning).toContain('other 1 was')
  })

  it('3 rivals, 3 known: only here may it say every rival is read', () => {
    const c = describeCoverage(dq({ rivals: 3, with_picks: 3, coverage_pct: 100, statuses: ['revealed'] }))
    expect(c.level).toBe('full')
    expect(c.summary).toBe('All 3 rival squads known')
    expect(c.meaning).toContain('every rival')
  })

  it('0 rivals, 0 known: no field, not zero coverage of a field', () => {
    const c = describeCoverage(dq({ rivals: 0, with_picks: 0, coverage_pct: 0, statuses: [] }))
    expect(c.level).toBe('no_rivals')
    expect(c.summary).toBe('No rivals in this league')
    expect(c.meaning).toContain('no field')
  })

  it('more squads than rivals: reported as inconsistent, never rounded up to full', () => {
    const c = describeCoverage(dq({ rivals: 3, with_picks: 5, coverage_pct: 100, statuses: ['revealed'] }))
    expect(c.level).toBe('inconsistent')
    expect(c.summary).toContain('5 rival squads known but only 3 rivals counted')
    expect(c.meaning).toContain('unreliable')
    expect(c.summary + c.meaning).not.toMatch(READABILITY_CLAIM)
  })

  it('a coverage_pct that contradicts the counts is inconsistent too', () => {
    const c = describeCoverage(dq({ rivals: 3, with_picks: 0, coverage_pct: 100 }))
    expect(c.level).toBe('inconsistent')
    expect(c.summary).toContain('reports 100% coverage')
  })

  it('tolerates rounding in coverage_pct', () => {
    expect(describeCoverage(dq({ rivals: 3, with_picks: 1, coverage_pct: 33 })).level).toBe('partial')
    expect(describeCoverage(dq({ rivals: 3, with_picks: 2, coverage_pct: 66.7 })).level).toBe('partial')
  })

  it('ignores a non-numeric coverage_pct rather than crying inconsistent', () => {
    const c = describeCoverage({ ...dq({ rivals: 3, with_picks: 3 }), coverage_pct: NaN })
    expect(c.level).toBe('full')
  })

  it('never claims readability for any with_picks below rivals', () => {
    for (let known = 0; known < 12; known++) {
      const c = describeCoverage(dq({ rivals: 12, with_picks: known, coverage_pct: (known / 12) * 100 }))
      expect(c.level).not.toBe('full')
      expect(c.summary + ' ' + c.meaning).not.toMatch(READABILITY_CLAIM)
    }
  })

  it('words the per-rival statuses instead of leaking raw enum values', () => {
    const c = describeCoverage(dq({ statuses: ['no_public_picks_yet'] }))
    expect(c.notes).toContain('picks are not public yet')
    expect(c.notes.join(' ')).not.toContain('no_public_picks_yet')
  })

  it('flags a truncated cohort and the gameweek the squads came from', () => {
    const c = describeCoverage(dq({
      rivals: 50, with_picks: 50, coverage_pct: 100,
      cohort_truncated: true, picks_source_event: 7, statuses: ['revealed'],
    }))
    expect(c.notes.join(' - ')).toContain('cohort capped')
    expect(c.notes.join(' - ')).toContain('GW7')
  })

  it('drops an unrecognised status rather than printing it', () => {
    const c = describeCoverage(dq({ statuses: ['something_new'] }))
    expect(c.notes.join(' ')).not.toContain('something_new')
  })
})

describe('the real published strategy artifact', () => {
  it.runIf(existsSync(DEPLOYED))('never pairs a readability claim with unread rivals', () => {
    const raw = JSON.parse(readFileSync(DEPLOYED, 'utf8'))
    const s = parseStrategy(raw)
    expect(s.kind).toBe('ok')
    if (s.kind !== 'ok') return
    expect(s.data.leagues.length).toBeGreaterThan(0)
    for (const l of s.data.leagues) {
      const label = CLASS_LABELS[l.classification] ?? l.classification
      const c = describeCoverage(l.data_quality)
      expect(label).not.toMatch(READABILITY_CLAIM)
      if (l.data_quality.with_picks < l.data_quality.rivals) {
        expect(c.level).not.toBe('full')
        expect(label + ' ' + c.summary + ' ' + c.meaning).not.toMatch(READABILITY_CLAIM)
      }
    }
  })
})
