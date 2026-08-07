import { describe, it, expect } from 'vitest'
import {
  parseDecision, parseLive, parseReview, parseNotifications,
  ALL_ACTIONS, signed, pctOf, money,
  SUPPORTED_WEEKLY, SUPPORTED_LIVE, SUPPORTED_REVIEW,
} from './weekly'

// --------------------------------------------------------------------------
// Fixtures shaped exactly like the artifacts the pipeline writes
// --------------------------------------------------------------------------

function decision(overrides: Record<string, unknown> = {}) {
  return {
    weekly_version: 'weekly-1.0',
    decision_version: 'decision-1.0',
    generated_at: '2026-08-20T10:00:00+00:00',
    gameweek: 1,
    horizon: 6,
    squad_state: { known: true, status: 'loaded', source_event: 1, players: [], captain: 1, vice: 2 },
    decision: {
      action: 'roll',
      headline: 'Roll your transfer',
      reason: 'nothing beats holding',
      transfers_in: [], transfers_out: [],
      captain: { id: 1, name: 'Haaland' }, vice: { id: 2, name: 'Salah' },
      starting: [], bench: [],
      comparison: {
        move_expected: 60, hold_expected: 60, delta: 0,
        delta_ci95: [-1, 1], p_move_beats_hold: 0.5, simulations: 2000,
        short_term_delta: 0, horizon_delta: null, hit_cost: 0,
      },
      executability: null, chip: null, league_note: '',
      confidence: 'medium', biggest_risk: 'minutes', assumptions: [],
    },
    versions: {
      model_version: 'm', objective_version: 'o', sim_version: 's',
      n_sims: 2000, seed: 1,
    },
    chip: null, leagues: [], freshness: {}, overrides: {},
    ...overrides,
  }
}

function live(overrides: Record<string, unknown> = {}) {
  return {
    live_version: 'live-1.0',
    gameweek: 1,
    as_of: '2026-08-22T16:00:00+00:00',
    available: true,
    unavailable_reason: null,
    fixtures: [{ id: 1, event: 1, team_h: 1, team_a: 2, state: 'live', minutes: 30, kickoff: null, bonus_final: false }],
    fixture_summary: { total: 1, by_state: { live: 1 }, all_finished: false, bonus_final: false },
    separation: { confirmed: 40, provisional_bonus: 3, predicted_remaining: 12, note: 'x' },
    ...overrides,
  }
}

function review(overrides: Record<string, unknown> = {}) {
  return {
    review_version: 'review-1.0',
    schema_version: 1,
    season: '2026-27',
    entry_id: 1066421,
    event: 1,
    generated_at: '2026-08-25T10:00:00+00:00',
    snapshot_as_of: '2026-08-21T15:00:00+00:00',
    has_snapshot: true,
    comparison: {
      recommended_points: 60, actual_points: 58, hold_points: 55,
      hindsight_points: 110, hindsight_is_unknowable: true,
      followed_advice: true, note: 'x',
    },
    attribution: { captaincy: 12, bench: 4 },
    quality: {
      expected_at_decision: 60, realised: 58, outcome_percentile: 0.44,
      positive_ev: true, verdict: 'good_decision', explanation: 'x',
    },
    lesson: null, league: [], limitations: [],
    ...overrides,
  }
}

// --------------------------------------------------------------------------
// Decision
// --------------------------------------------------------------------------

describe('parseDecision', () => {
  it('accepts a well-formed artifact', () => {
    expect(parseDecision(decision()).kind).toBe('ok')
  })

  it('reports a missing artifact rather than throwing', () => {
    expect(parseDecision(null).kind).toBe('missing')
    expect(parseDecision(undefined).kind).toBe('missing')
  })

  it('rejects a non-object', () => {
    expect(parseDecision([]).kind).toBe('malformed')
    expect(parseDecision('no').kind).toBe('malformed')
  })

  it.each(['weekly_version', 'decision_version'])(
    'refuses an unrecognised %s', (field) => {
      const s = parseDecision(decision({ [field]: 'v99' }))
      expect(s.kind).toBe('unsupported')
      if (s.kind === 'unsupported') expect(s.detail).toContain(field)
    })

  it('refuses a missing version outright', () => {
    const d = decision()
    delete (d as Record<string, unknown>).weekly_version
    expect(parseDecision(d).kind).toBe('unsupported')
  })

  it('rejects an action outside the vocabulary', () => {
    const d = decision()
    ;(d.decision as Record<string, unknown>).action = 'panic'
    const s = parseDecision(d)
    expect(s.kind).toBe('malformed')
    if (s.kind === 'malformed') expect(s.detail).toContain('panic')
  })

  it('accepts every declared action', () => {
    for (const action of ALL_ACTIONS) {
      const d = decision()
      ;(d.decision as Record<string, unknown>).action = action
      expect(parseDecision(d).kind).toBe('ok')
    }
  })

  it('rejects a decision with no headline', () => {
    const d = decision()
    ;(d.decision as Record<string, unknown>).headline = ''
    expect(parseDecision(d).kind).toBe('malformed')
  })

  it('names exactly the versions the python side emits', () => {
    expect(SUPPORTED_WEEKLY).toContain('weekly-1.0')
    expect(SUPPORTED_LIVE).toContain('live-1.0')
    expect(SUPPORTED_REVIEW).toContain('review-1.0')
  })
})

// --------------------------------------------------------------------------
// Live
// --------------------------------------------------------------------------

describe('parseLive', () => {
  it('accepts a well-formed live state', () => {
    expect(parseLive(live()).kind).toBe('ok')
  })

  it('refuses an unrecognised version', () => {
    expect(parseLive(live({ live_version: 'live-9' })).kind).toBe('unsupported')
  })

  it('requires an explicit availability flag', () => {
    const l = live()
    delete (l as Record<string, unknown>).available
    const s = parseLive(l)
    expect(s.kind).toBe('malformed')
    if (s.kind === 'malformed') expect(s.detail).toContain('available')
  })

  it('requires unavailable data to say why', () => {
    const s = parseLive(live({ available: false, unavailable_reason: null }))
    expect(s.kind).toBe('malformed')
  })

  it('accepts an unavailable state that gives a reason', () => {
    const s = parseLive(live({ available: false, unavailable_reason: 'not_started' }))
    expect(s.kind).toBe('ok')
  })

  it('is missing rather than malformed when there is no artifact', () => {
    expect(parseLive(null).kind).toBe('missing')
  })
})

// --------------------------------------------------------------------------
// Review
// --------------------------------------------------------------------------

describe('parseReview', () => {
  it('accepts a well-formed review', () => {
    expect(parseReview(review()).kind).toBe('ok')
  })

  it('refuses an unrecognised version', () => {
    expect(parseReview(review({ review_version: 'review-2' })).kind).toBe('unsupported')
  })

  it('requires an event number', () => {
    expect(parseReview(review({ event: 'one' })).kind).toBe('malformed')
  })

  it('requires a verdict', () => {
    expect(parseReview(review({ quality: {} })).kind).toBe('malformed')
  })

  it('keeps the hindsight column labelled unknowable', () => {
    const s = parseReview(review())
    expect(s.kind).toBe('ok')
    if (s.kind === 'ok') {
      expect(s.data.comparison.hindsight_is_unknowable).toBe(true)
    }
  })
})

// --------------------------------------------------------------------------
// Notifications
// --------------------------------------------------------------------------

describe('parseNotifications', () => {
  it('requires an explicit dry_run flag', () => {
    expect(parseNotifications({ result: {} }).kind).toBe('malformed')
    expect(parseNotifications({ result: { dry_run: true } }).kind).toBe('ok')
  })

  it('is missing rather than malformed when absent', () => {
    expect(parseNotifications(null).kind).toBe('missing')
  })
})

// --------------------------------------------------------------------------
// Formatting
// --------------------------------------------------------------------------

describe('formatting', () => {
  it('signs numbers and never invents one', () => {
    expect(signed(2.34)).toBe('+2.3')
    expect(signed(-2.34)).toBe('-2.3')
    expect(signed(0)).toBe('+0.0')
    expect(signed(null)).toBe('—')
    expect(signed(NaN)).toBe('—')
  })

  it('formats probabilities', () => {
    expect(pctOf(0.5)).toBe('50%')
    expect(pctOf(0.512, 1)).toBe('51.2%')
    expect(pctOf(null)).toBe('—')
  })

  it('formats money in FPL tenths, and says when the bank is unknown', () => {
    expect(money(15)).toBe('£1.5m')
    expect(money(0)).toBe('£0.0m')
    expect(money(null)).toBe('unknown')
  })
})
