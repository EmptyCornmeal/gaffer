// Backtest artifact schema + validation.
//
// The Accuracy page previously rendered a legacy artifact produced by a harness
// that scored a *substitute* model (it injected a different fixture multiplier
// and clean-sheet formula, and filtered the evaluation set on post-match
// minutes). Those numbers were published as if they described the shipped
// model. This module makes the schema explicit and refuses anything it does not
// recognise, so that cannot silently recur.

/**
 * Schema versions this build can render.
 *
 * 7 is the current one: it moves the test season to 2025-26, adds `season_split`
 * and relabels the frozen `model_candidates` block. Every change is additive —
 * nothing in `web/src` reads `season_split` — so a v6 and a v7 artifact render
 * identically today.
 *
 * 6 is kept alongside it deliberately, and this is a transition state rather
 * than a permanent pair. The pipeline writes `data/` and `web/public/data/` on
 * the same run, so between a deploy carrying this code and the next scheduled
 * refresh the published artifact is still v6; accepting only 7 would blank the
 * Accuracy page for that window. Drop 6 once the deployed artifact is v7.
 *
 * Supported rather than rejected because v6 is not *wrong*. Versions 3, 4 and 5
 * are refused below because rendering them would put a withdrawn or mislabelled
 * number back on the page. A v6 artifact is an honest measurement of an earlier
 * split — superseded, not retracted — and that distinction is the entire reason
 * the two lists exist separately.
 */
export const SUPPORTED_SCHEMA_VERSIONS = [6, 7] as const
/**
 * Versions we know about and deliberately refuse.
 *
 * 3 is refused: it reported `fpl_xp` and `ensemble` as measured baselines, and
 * those were computed from the archive's `xP` column, which is inadmissible.
 * Rendering a v3 artifact would put a withdrawn number back on the page.
 *
 * 4 is refused: it reported one collapsed model verdict, which said trained
 * models lost every decision metric. Ridge did not. A v4 artifact cannot show
 * the per-candidate evidence, so it would repeat the claim it got wrong.
 *
 * 5 is refused: it evaluated GW2 onwards while describing itself as covering
 * GW1, so its headline numbers are in-season numbers wearing an all-season
 * label — and it carries no `pre_season` block, so a build rendering it would
 * present the pre-season regime as measured when it never was.
 */
export const REJECTED_SCHEMA_VERSIONS = [1, 2, 3, 4, 5] as const

export interface CalBin {
  pred: number
  actual: number
  haul_rate: number
  n: number
}

export interface HorizonDecisions {
  gameweeks_scored: number
  xi_points_per_gw: number
  xi_regret_per_gw: number | null
  captain_points_per_gw: number
  captain_regret_per_gw: number
  captain_accuracy_pct: number
}

export interface HorizonBlock {
  n: number
  mae: Record<string, number>
  rank_corr: Record<string, number>
  decisions?: Record<string, HorizonDecisions | Record<string, never>>
  transfers?: Record<string, { with_transfers: number; hold_squad: number; gain: number; gameweeks: number }>
}

/**
 * GW1 measured on its own.
 *
 * A separate block rather than a row in `per_horizon` because it is a separate
 * regime: no season-to-date history exists, so the model runs on prior-season
 * rates and the price prior alone, and the naive baseline — cumulative
 * season-to-date points-per-game — does not exist at all. `naive_baseline`
 * carries that explanation rather than a number, so the page cannot imply a
 * comparison that was never made.
 */
export interface PreSeasonBlock {
  decision_gw: number
  n: number
  regime: string
  mae: Record<string, number>
  rank_corr: Record<string, number>
  decisions?: Record<string, HorizonDecisions | Record<string, never>>
  /** Why the `decisions` numbers above are one evening, not a rate. */
  decisions_caveat?: string
  zero_minute_share_pct?: number
  naive_baseline: string
}

/** A baseline that was published and then retracted, with its old numbers. */
export interface WithdrawnBaseline {
  withdrawn_in_schema: number
  previously_reported: Record<string, number>
  reason: string
}

export interface BacktestV6 {
  schema_version: number
  model_version: string
  dataset: string
  season: string
  decision_gameweeks: string
  horizons: number[]
  coverage: {
    rows_evaluated: number
    zero_minute_rows_retained: number
    zero_minute_share_pct: number
    decision_gws?: number
    excluded?: Record<string, unknown>
  }
  leakage_check: {
    enforced: boolean
    post_match_fields_in_features: string[]
    policy: string
  }
  per_horizon: Record<string, HorizonBlock>
  /** Absent on an artifact whose season had no evaluable GW1. */
  pre_season?: PreSeasonBlock | Record<string, never>
  calibration: {
    overall: CalBin[]
    /** G20 — restricted to player-gameweeks where the player featured. */
    appeared?: CalBin[]
    by_position?: Record<string, CalBin[]>
    note?: string
  }
  limitations: string[]
  generated_at: string
  baselines?: Record<string, Record<string, number | string>>
  ablations?: Array<Record<string, unknown>>
  shipped_projection?: Record<string, string>
  withdrawn_baselines?: Record<string, WithdrawnBaseline | string>
  model_candidates?: ModelCandidates
}

/** One horizon's paired result for a candidate. */
export interface CandidateHorizon {
  candidate_xi: number
  diff: number
  ci95: [number, number]
  p_better?: number
  wins?: number
  losses?: number
}

export interface ModelCandidate {
  candidate: string
  label?: string
  detail?: string
  decision: 'rejected' | 'inconclusive' | 'invalid_experiment' | 'shipped'
  reason: string
  worse_at_every_horizon?: boolean | null
  per_horizon?: Record<string, CandidateHorizon>
  statistical?: Record<string, Record<string, number>>
  captain_accuracy_pct_h1?: number
  captain_regret_per_gw_h1?: number
  limitations?: string[]
}

export interface ModelCandidates {
  evaluation_version?: string
  outcome?: string
  protocol?: string
  heuristic_reference?: {
    xi_points_per_gw?: Record<string, number>
    captain_accuracy_pct_h1?: number
    captain_regret_per_gw_h1?: number
  }
  candidates: ModelCandidate[]
  not_ruled_out?: string
}

export type BacktestState =
  | { kind: 'ok'; data: BacktestV6 }
  | { kind: 'missing' }
  | { kind: 'unsupported'; version: unknown; detail: string }
  | { kind: 'malformed'; detail: string }

const REQUIRED_KEYS = [
  'model_version', 'season', 'per_horizon', 'coverage',
  'leakage_check', 'limitations', 'generated_at',
] as const

/**
 * Validate a raw backtest artifact.
 *
 * Deliberately strict: an unrecognised or malformed artifact renders an
 * explanatory error, never a number. Publishing a stale accuracy claim is worse
 * than publishing none.
 */
export function parseBacktest(raw: unknown): BacktestState {
  if (raw == null) return { kind: 'missing' }
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    return { kind: 'malformed', detail: `expected an object, got ${Array.isArray(raw) ? 'an array' : typeof raw}` }
  }

  const obj = raw as Record<string, unknown>
  const version = obj.schema_version

  if (version === undefined) {
    // The legacy artifact had no schema_version at all.
    return {
      kind: 'unsupported',
      version: undefined,
      detail:
        'This artifact predates the corrected backtest. It was produced by a harness ' +
        'that scored a substitute model and filtered the evaluation set on post-match ' +
        'minutes, so its numbers do not describe the shipped model.',
    }
  }
  // Widened rather than cast to the current literal, matching `parseLive`. The
  // old `version as 5` had to be edited on every schema bump and failed the type
  // check when it was not — a compile error each time, but only by luck.
  if (typeof version !== 'number'
      || !(SUPPORTED_SCHEMA_VERSIONS as readonly number[]).includes(version)) {
    return {
      kind: 'unsupported',
      version,
      detail: `schema_version ${JSON.stringify(version)} is not renderable by this build ` +
        `(supported: ${SUPPORTED_SCHEMA_VERSIONS.join(', ')}).`,
    }
  }

  for (const k of REQUIRED_KEYS) {
    if (!(k in obj)) return { kind: 'malformed', detail: `missing required field '${k}'` }
  }
  if (typeof obj.per_horizon !== 'object' || obj.per_horizon == null) {
    return { kind: 'malformed', detail: "'per_horizon' must be an object" }
  }
  if (Object.keys(obj.per_horizon as object).length === 0) {
    return { kind: 'malformed', detail: "'per_horizon' is empty — nothing was evaluated" }
  }
  if (!Array.isArray(obj.limitations)) {
    return { kind: 'malformed', detail: "'limitations' must be an array" }
  }
  const leak = obj.leakage_check as Record<string, unknown> | undefined
  if (!leak || typeof leak.enforced !== 'boolean') {
    return { kind: 'malformed', detail: "'leakage_check.enforced' must be a boolean" }
  }

  return { kind: 'ok', data: obj as unknown as BacktestV6 }
}

/** Horizons present, in numeric order. */
export function horizonKeys(bt: BacktestV6): string[] {
  return Object.keys(bt.per_horizon).sort((a, b) => Number(a) - Number(b))
}

/** Every method named anywhere in the per-horizon blocks. */
export function methodsIn(bt: BacktestV6): string[] {
  const seen = new Set<string>()
  for (const k of horizonKeys(bt)) {
    for (const m of Object.keys(bt.per_horizon[k].rank_corr ?? {})) seen.add(m)
  }
  return [...seen]
}

/** True when the leakage guard ran and found nothing. */
export function leakageClean(bt: BacktestV6): boolean {
  return bt.leakage_check.enforced &&
    (bt.leakage_check.post_match_fields_in_features?.length ?? 0) === 0
}

export const METHOD_LABELS: Record<string, string> = {
  gaffer: 'Gaffer (component model)',
  naive: 'Recent-form average',
  // Retained so a withdrawn baseline still has a name if it ever appears in a
  // `withdrawn_baselines` block. It must never appear in `per_horizon`.
  fpl_xp: "FPL's own xP",
  ensemble: 'Gaffer + FPL xP blend',
}

/** Baselines that were retracted, newest schema first. Empty when there are none. */
export function withdrawn(bt: BacktestV6): Array<{ key: string; label: string; entry: WithdrawnBaseline }> {
  const out: Array<{ key: string; label: string; entry: WithdrawnBaseline }> = []
  for (const [key, entry] of Object.entries(bt.withdrawn_baselines ?? {})) {
    if (entry && typeof entry === 'object' && 'reason' in entry) {
      out.push({ key, label: METHOD_LABELS[key] ?? key, entry: entry as WithdrawnBaseline })
    }
  }
  return out
}

/**
 * Every evaluated candidate, in artifact order.
 *
 * The page must render all of them. Showing only the rejected one is how the
 * summary came to say "trained models lose every decision metric" when ridge
 * beat the heuristic at h=1.
 */
export function modelCandidates(bt: BacktestV6): ModelCandidate[] {
  const c = bt.model_candidates?.candidates
  return Array.isArray(c) ? c.filter((x) => x && typeof x.candidate === 'string') : []
}

/** Human label for a decision. `rejected` and `inconclusive` are not the same. */
export const DECISION_LABELS: Record<string, string> = {
  rejected: 'Rejected',
  inconclusive: 'Inconclusive — not selected',
  invalid_experiment: 'Invalid experiment',
  shipped: 'Shipped',
}

/** Free-text consequence note on the withdrawal, when the artifact carries one. */
export function withdrawalConsequence(bt: BacktestV6): string | null {
  const c = bt.withdrawn_baselines?.consequence
  return typeof c === 'string' ? c : null
}
