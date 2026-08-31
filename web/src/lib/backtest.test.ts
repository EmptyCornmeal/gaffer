import { describe, expect, it } from 'vitest'
import {
  horizonKeys, leakageClean, methodsIn, parseBacktest, SUPPORTED_SCHEMA_VERSIONS,
  withdrawalConsequence, withdrawn, modelCandidates, DECISION_LABELS,
  minutesModel, minutesUnmeasured, minutesBaselineSweeps, minutesHorizonKeys,
  MINUTES_METHOD_LABELS,
} from './backtest'

const valid = {
  schema_version: 7,
  model_version: 'heuristic-0.1',
  dataset: 'vaastav/Fantasy-Premier-League merged_gw',
  season: '2024-25',
  decision_gameweeks: 'GW1-GW38',
  horizons: [1, 2],
  coverage: {
    rows_evaluated: 26615,
    zero_minute_rows_retained: 15491,
    zero_minute_share_pct: 58.2,
  },
  pre_season: {
    decision_gw: 1,
    n: 616,
    regime: 'prior-season rates and the price prior only',
    mae: { gaffer: 1.544 },
    rank_corr: { gaffer: 0.439 },
    naive_baseline: 'The naive baseline here is cumulative season-to-date ' +
      'points-per-game: it predicts 0 for every player.',
  },
  leakage_check: { enforced: true, post_match_fields_in_features: [], policy: 'shift(1)' },
  per_horizon: {
    '1': { n: 26615, mae: { gaffer: 1.566, naive: 1.11 }, rank_corr: { gaffer: 0.44, naive: 0.666 } },
    '2': { n: 25443, mae: { gaffer: 1.576 }, rank_corr: { gaffer: 0.423 } },
  },
  calibration: { overall: [] },
  limitations: ['pre-deadline team ratings are rebuilt, not the live construction'],
  withdrawn_baselines: {
    fpl_xp: {
      withdrawn_in_schema: 4,
      previously_reported: { rank_corr_h1: 0.76, xi_points_per_gw: 84.2 },
      reason: "computed from the archive's xP column, which carries same-gameweek information",
    },
    consequence: 'EP_NEXT_BLEND_WEIGHT is now a labelled policy choice.',
  },
  model_candidates: {
    heuristic_reference: { xi_points_per_gw: { '1': 50.86 }, captain_accuracy_pct_h1: 29.7 },
    candidates: [
      {
        candidate: 'gbm', label: 'Gradient-boosted trees', decision: 'rejected',
        reason: 'worse at every horizon', worse_at_every_horizon: true,
        per_horizon: { '1': { candidate_xi: 46.22, diff: -4.65, ci95: [-10.03, 0.84] } },
        captain_accuracy_pct_h1: 13.5,
      },
      {
        candidate: 'ridge', label: 'Regularised linear model', decision: 'inconclusive',
        reason: 'beat the heuristic at h=1 but the interval spans zero',
        worse_at_every_horizon: false,
        per_horizon: { '1': { candidate_xi: 53.57, diff: 2.7, ci95: [-1.38, 6.89] } },
        captain_accuracy_pct_h1: 32.4,
      },
    ],
    not_ruled_out: 'a minutes-only classifier',
  },
  generated_at: '2026-08-06T18:00:00+00:00',
}

// A11 — the minutes block, as schema 8 publishes it.
const minutes = {
  measured: true,
  season: '2025-26',
  model_version: 'heuristic-0.5',
  verdict: 'Measured for the first time, and it loses.',
  baselines: {
    start_rate_td: 'season-to-date starts over the team fixtures played',
    start_rate_r3: 'the share of his last three fixtures he started',
  },
  per_horizon: {
    '1': {
      n: 28913, start_rate: 0.281,
      brier: { gaffer: 0.1502, start_rate_td: 0.11, start_rate_r3: 0.0986 },
      brier_skill: { gaffer: 0.2554, start_rate_td: 0.4553, start_rate_r3: 0.5123 },
      auc: { gaffer: 0.8284, start_rate_td: 0.9074, start_rate_r3: 0.9038 },
      exp_minutes_mae: { gaffer: 27.17, mins_avg_td: 14.87, started_lag_x90: 11.54 },
    },
    '2': {
      n: 27662, start_rate: 0.281,
      brier: { gaffer: 0.1566, start_rate_td: 0.1193, start_rate_r3: 0.1216 },
      brier_skill: { gaffer: 0.22, start_rate_td: 0.41, start_rate_r3: 0.4 },
      auc: { gaffer: 0.8141, start_rate_td: 0.8924, start_rate_r3: 0.8712 },
      exp_minutes_mae: { gaffer: 27.9, mins_avg_td: 15.4, started_lag_x90: 14.2 },
    },
  },
  bands: {
    overall: [
      { band: 'NAILED', n: 3398, claimed: 0.944, start_rate: 0.835,
        appear_rate: 0.876, exp_minutes: 77.7, actual_minutes: 73.3 },
      { band: 'CAMEO?', n: 22535, claimed: 0.259, start_rate: 0.147,
        appear_rate: 0.259, exp_minutes: 25.1, actual_minutes: 13.6 },
    ],
    considered: [
      { band: 'CAMEO?', n: 4248, claimed: 0.32, start_rate: 0.331,
        appear_rate: 0.508, exp_minutes: 29.9, actual_minutes: 30.2 },
    ],
  },
  branches: [
    { branch: 'price_prior', n: 10791, share_pct: 36.3, mean_p_start: 0.288,
      start_rate: 0.023, brier: 0.0916, brier_skill: -3.07 },
  ],
  calibration: { overall: [], considered_rank_cut: 250 },
  limitations: ['availability is never varied'],
  live_audit: { status: 'measured', target_gw: 1, n: 600, brier: 0.1837,
                nailed_n: 52, nailed_that_did_not_start: 10 },
}

describe('parseBacktest — acceptance', () => {
  it('accepts a well-formed v7 artifact', () => {
    const s = parseBacktest(valid)
    expect(s.kind).toBe('ok')
    if (s.kind === 'ok') expect(s.data.model_version).toBe('heuristic-0.1')
  })
  it('accepts a v8 artifact, which differs from v7 only by addition', () => {
    const s = parseBacktest({ ...valid, schema_version: 8, minutes_model: minutes })
    expect(s.kind).toBe('ok')
  })
  it('only claims support for versions it can render', () => {
    expect(SUPPORTED_SCHEMA_VERSIONS).toEqual([7, 8])
  })
  // v6 was honest, so it was supported through the 6/7 window. It is refused
  // now: it reports a different split AND predates every minutes number, and a
  // transition window two splits wide is not a transition.
  it('refuses v6 now that the window has moved on', () => {
    const s = parseBacktest({ ...valid, schema_version: 6 })
    expect(s.kind).toBe('unsupported')
  })
})

describe('the minutes model', () => {
  const v8 = { ...valid, schema_version: 8, minutes_model: minutes }
  const parsed = parseBacktest(v8)
  const bt = parsed.kind === 'ok' ? parsed.data : null

  it('is absent from a v7 artifact rather than empty', () => {
    const p = parseBacktest(valid)
    expect(p.kind).toBe('ok')
    if (p.kind === 'ok') {
      expect(minutesModel(p.data)).toBeNull()
      expect(minutesUnmeasured(p.data)).toBeNull()
    }
  })

  it('surfaces the block when it was measured', () => {
    expect(bt && minutesModel(bt)?.season).toBe('2025-26')
  })

  // "we could not measure this" and "there is nothing to show" are different
  // sentences, and the page must be able to say the first one.
  it('reports an unmeasured season as unmeasured, with its reason', () => {
    const p = parseBacktest({
      ...valid, schema_version: 8,
      minutes_model: { measured: false, reason: 'no `starts` column' },
    })
    expect(p.kind).toBe('ok')
    if (p.kind === 'ok') {
      expect(minutesModel(p.data)).toBeNull()
      expect(minutesUnmeasured(p.data)).toContain('starts')
    }
  })

  it('derives the "a baseline beats us everywhere" claim from the numbers', () => {
    expect(bt && minutesBaselineSweeps(minutesModel(bt)!)).toBe(true)
  })

  // The one that stops the page congratulating itself: if Gaffer ever wins a
  // horizon, the sweeping claim must stop rendering.
  it('stops claiming a sweep the moment Gaffer wins one horizon', () => {
    const won = structuredClone(minutes)
    won.per_horizon['2'].brier = { gaffer: 0.05, start_rate_td: 0.11, start_rate_r3: 0.12 }
    expect(minutesBaselineSweeps(won as never)).toBe(false)
  })

  it('orders minutes horizons numerically', () => {
    expect(bt && minutesHorizonKeys(minutesModel(bt)!)).toEqual(['1', '2'])
  })

  it('names every baseline it renders as a column', () => {
    for (const k of Object.keys(minutes.per_horizon['1'].brier)) {
      expect(MINUTES_METHOD_LABELS[k], `no label for ${k}`).toBeTruthy()
    }
  })
})

describe('parseBacktest — rejection', () => {
  it('reports a missing artifact distinctly from a broken one', () => {
    expect(parseBacktest(null).kind).toBe('missing')
    expect(parseBacktest(undefined).kind).toBe('missing')
  })

  it('rejects the legacy artifact that has no schema_version', () => {
    // The real shipped file: ml-vs-heuristic, minutes>0 filtered.
    const legacy = {
      season: '2024-25', n_predictions: 10011, trained_on: '2022-23 + 2023-24',
      mae: { gaffer: 1.889, ml: 1.858, fpl_xp: 1.804, naive: 2.064 },
      rank_corr: { gaffer: 0.3, ml: 0.379, fpl_xp: 0.572, naive: 0.308 },
      generated_at: '2026-07-26T15:32:54+00:00',
    }
    const s = parseBacktest(legacy)
    expect(s.kind).toBe('unsupported')
    if (s.kind === 'unsupported') {
      expect(s.detail).toContain('substitute model')
      expect(s.detail).toContain('post-match')
    }
  })

  it('rejects a superseded numeric schema version', () => {
    const s = parseBacktest({ ...valid, schema_version: 2 })
    expect(s.kind).toBe('unsupported')
    if (s.kind === 'unsupported') expect(s.detail).toContain('supported: 7')
  })

  it('rejects v3 — it reported baselines that were later withdrawn', () => {
    // A v3 artifact renders `fpl_xp` and `ensemble` as measured. Those numbers
    // came from the archive's `xP` column and are inadmissible; showing them
    // again would undo the retraction.
    expect(parseBacktest({ ...valid, schema_version: 3 }).kind).toBe('unsupported')
  })

  it('rejects v4 — it collapsed every trained model into one verdict', () => {
    // v4 said trained models lose every decision metric. Ridge did not. A v4
    // artifact cannot carry the per-candidate evidence, so rendering it would
    // repeat the claim it got wrong.
    expect(parseBacktest({ ...valid, schema_version: 4 }).kind).toBe('unsupported')
  })

  it('rejects v5 — it never evaluated the pre-season decision', () => {
    // v5 ran from GW2 while its own constant comment claimed GW1 was included,
    // so its headline numbers are in-season numbers under an all-season label,
    // and it carries no `pre_season` block. Rendering one would present the
    // regime that picks the opening squad as measured when it was not.
    expect(parseBacktest({ ...valid, schema_version: 5 }).kind).toBe('unsupported')
  })

  it('rejects a future schema version rather than guessing', () => {
    expect(parseBacktest({ ...valid, schema_version: 99 }).kind).toBe('unsupported')
  })

  it('carries the pre-season block through, with its missing baseline named', () => {
    const s = parseBacktest(valid)
    expect(s.kind).toBe('ok')
    if (s.kind !== 'ok') return
    expect(s.data.pre_season?.decision_gw).toBe(1)
    expect(s.data.pre_season?.rank_corr.gaffer).toBe(0.439)
    // The absent naive baseline must be explained, never rendered as a number:
    // "no baseline" and "beat the baseline" must not look alike on the page.
    expect(s.data.pre_season?.rank_corr.naive).toBeUndefined()
    expect(s.data.pre_season?.naive_baseline).toContain('predicts 0 for every player')
  })

  it('rejects non-object payloads', () => {
    for (const bad of ['a string', 42, [1, 2, 3], true]) {
      expect(parseBacktest(bad).kind).toBe('malformed')
    }
  })

  it.each([
    'model_version', 'season', 'per_horizon', 'coverage',
    'leakage_check', 'limitations', 'generated_at',
  ])('rejects an artifact missing %s', (key) => {
    const broken: Record<string, unknown> = { ...valid }
    delete broken[key]
    const s = parseBacktest(broken)
    expect(s.kind).toBe('malformed')
    if (s.kind === 'malformed') expect(s.detail).toContain(key)
  })

  it('rejects an empty per_horizon', () => {
    const s = parseBacktest({ ...valid, per_horizon: {} })
    expect(s.kind).toBe('malformed')
    if (s.kind === 'malformed') expect(s.detail).toContain('nothing was evaluated')
  })

  it('rejects a malformed leakage_check', () => {
    const s = parseBacktest({ ...valid, leakage_check: { enforced: 'yes' } })
    expect(s.kind).toBe('malformed')
  })

  it('rejects non-array limitations', () => {
    expect(parseBacktest({ ...valid, limitations: 'none' }).kind).toBe('malformed')
  })
})

describe('helpers', () => {
  it('orders horizons numerically, not lexically', () => {
    const many = { ...valid, per_horizon: { '10': valid.per_horizon['1'], '2': valid.per_horizon['2'], '1': valid.per_horizon['1'] } }
    const s = parseBacktest(many)
    if (s.kind !== 'ok') throw new Error('expected ok')
    expect(horizonKeys(s.data)).toEqual(['1', '2', '10'])
  })

  it('collects every method named across horizons', () => {
    const s = parseBacktest(valid)
    if (s.kind !== 'ok') throw new Error('expected ok')
    expect(methodsIn(s.data).sort()).toEqual(['gaffer', 'naive'])
  })

  it('surfaces withdrawn baselines with the numbers they used to show', () => {
    const s = parseBacktest(valid)
    if (s.kind !== 'ok') throw new Error('expected ok')
    const w = withdrawn(s.data)
    expect(w).toHaveLength(1)
    expect(w[0].key).toBe('fpl_xp')
    expect(w[0].label).toBe("FPL's own xP")
    expect(w[0].entry.previously_reported.xi_points_per_gw).toBe(84.2)
    expect(withdrawalConsequence(s.data)).toContain('policy choice')
  })

  it('treats the consequence string as prose, never as a withdrawn baseline', () => {
    const s = parseBacktest(valid)
    if (s.kind !== 'ok') throw new Error('expected ok')
    expect(withdrawn(s.data).map((w) => w.key)).not.toContain('consequence')
  })

  it('has nothing to surface when no baseline was withdrawn', () => {
    const s = parseBacktest({ ...valid, withdrawn_baselines: undefined })
    if (s.kind !== 'ok') throw new Error('expected ok')
    expect(withdrawn(s.data)).toEqual([])
    expect(withdrawalConsequence(s.data)).toBeNull()
  })

  it('surfaces every model candidate, not just the rejected one', () => {
    // Rendering GBM alone is how the summary came to say trained models lose
    // every decision metric when ridge beat the heuristic at h=1.
    const s = parseBacktest(valid)
    if (s.kind !== 'ok') throw new Error('expected ok')
    const cs = modelCandidates(s.data)
    expect(cs.map((c) => c.candidate)).toEqual(['gbm', 'ridge'])
    expect(cs.map((c) => c.decision)).toEqual(['rejected', 'inconclusive'])
  })

  it('labels rejected and inconclusive differently', () => {
    expect(DECISION_LABELS.rejected).not.toBe(DECISION_LABELS.inconclusive)
    expect(DECISION_LABELS.inconclusive).toContain('not selected')
  })

  it('records ridge as beating the heuristic at h=1', () => {
    const s = parseBacktest(valid)
    if (s.kind !== 'ok') throw new Error('expected ok')
    const ridge = modelCandidates(s.data).find((c) => c.candidate === 'ridge')!
    expect(ridge.per_horizon!['1'].diff).toBeGreaterThan(0)
    expect(ridge.decision).not.toBe('rejected')
  })

  it('has no candidates when the artifact carries none', () => {
    const s = parseBacktest({ ...valid, model_candidates: undefined })
    if (s.kind !== 'ok') throw new Error('expected ok')
    expect(modelCandidates(s.data)).toEqual([])
  })

  it('reports leakage state honestly', () => {
    const ok = parseBacktest(valid)
    if (ok.kind !== 'ok') throw new Error('expected ok')
    expect(leakageClean(ok.data)).toBe(true)

    const dirty = parseBacktest({
      ...valid,
      leakage_check: { enforced: true, post_match_fields_in_features: ['minutes'], policy: 'x' },
    })
    if (dirty.kind !== 'ok') throw new Error('expected ok')
    expect(leakageClean(dirty.data)).toBe(false)

    const unenforced = parseBacktest({
      ...valid, leakage_check: { enforced: false, post_match_fields_in_features: [], policy: 'x' },
    })
    if (unenforced.kind !== 'ok') throw new Error('expected ok')
    expect(leakageClean(unenforced.data)).toBe(false)
  })
})
