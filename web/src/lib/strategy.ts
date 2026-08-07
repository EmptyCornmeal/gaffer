// Strategy artifact schema + validation (T-17/T-18/T-20 on the front-end).
//
// This artifact publishes *probabilities*. A stale or unrecognised version would
// keep rendering a placing chance computed by a simulator that no longer exists,
// so — exactly as with the backtest — anything this build cannot interpret is
// refused with an explanation rather than rendered.

/** Versions of each sub-layer this build can render. */
export const SUPPORTED = {
  strategy: ['strategy-1.0'],
  league: ['league-1.0'],
  multileague: ['multileague-1.0'],
  chips: ['chips-1.0'],
} as const

export interface MiniCard {
  id: number
  name: string
  team?: string | null
  pos?: string | null
  price?: number | null
  code?: number | null
  team_code?: number | null
  next_gw_xp?: number | null
}

export interface OwnershipRow {
  player_id: number
  owners: number
  n_rivals: number
  ownership_pct: number
  effective_ownership_pct: number
  captain_eo_pct: number
  player?: MiniCard
}

export interface Placing {
  p_first: number
  p_target: number
  target_position: number
  expected_position: number
  simulations: number
  ci95_halfwidth: number
  basis: string
  /** False when there was no field to place against — render "—", not a number. */
  available: boolean
  rival_coverage_pct: number
  caveats: string[]
}

export interface Posture {
  stance: 'protect' | 'neutral' | 'chase' | 'desperate'
  reason: string
  variance_preference: number
}

export interface DataQuality {
  rivals: number
  with_picks: number
  coverage_pct: number
  cohort_truncated: boolean
  picks_source_event: number | null
  statuses: string[]
}

export interface LeagueView {
  league_id: number
  name: string
  league_type: string
  classification: string
  size: number | null
  target_position: number
  posture: Posture
  placing: Placing
  shields: OwnershipRow[]
  differentials: OwnershipRow[]
  data_quality: DataQuality
  differs_from_neutral: boolean
  difference_reason: string
}

export interface Option {
  key: string
  label: string
  expected_points: number
  p_target: Record<string, number>
}

export interface Conflict {
  option_a: string
  option_b: string
  per_league: Array<{
    league: string
    p_target_a?: number
    p_target_b?: number
    prefers: string
    delta: number
  }>
}

export interface Resolution {
  default: string | null
  reason: string
  shortlist: Option[]
  conflicts: Conflict[]
  weighted_scores?: Record<string, number>
}

export interface ChipEval {
  chip: string
  gameweek: number
  expected_gain: number
  ci95: [number, number]
  baseline_points: number
  with_chip_points: number
  assumptions: string[]
}

export interface ChipPlan {
  chips_version: string
  recommendation: string
  gameweek: number | null
  expected_gain: number
  use_threshold: number
  alternatives: ChipEval[]
  available: Array<{ name: string; start_event: number; stop_event: number }>
  used: string[]
  reason: string
}

export interface Simulation {
  sim_version: string
  n_sims: number
  seed: number
  model_version: string
  gameweek?: number
  fixtures?: number
  players?: number
  assumptions?: string[]
}

export interface Strategy {
  strategy_version: string
  league_version: string
  multileague_version: string
  chips_version: string
  generated_at: string
  gameweek: number
  gameweeks_remaining: number
  simulation: Simulation
  basis: string
  squad: { starting: MiniCard[]; bench: MiniCard[]; captain: MiniCard | null; source_event: number | null }
  leagues: LeagueView[]
  league_errors: Array<{ league_id: number | null; error: string }>
  options: Option[]
  resolution: Resolution
  chips: ChipPlan
  limitations: string[]
}

export type StrategyState =
  | { kind: 'ok'; data: Strategy }
  | { kind: 'missing' }
  | { kind: 'failed'; detail: string }
  | { kind: 'unsupported'; detail: string }
  | { kind: 'malformed'; detail: string }

const VERSION_FIELDS: Array<[keyof typeof SUPPORTED, string]> = [
  ['strategy', 'strategy_version'],
  ['league', 'league_version'],
  ['multileague', 'multileague_version'],
  ['chips', 'chips_version'],
]

/**
 * Validate a raw strategy artifact.
 *
 * A pipeline run whose strategy step failed writes `{error: ...}`; that is a
 * legitimate, publishable state and is surfaced as `failed` so the UI can say
 * what broke instead of pretending there are no leagues.
 */
export function parseStrategy(raw: unknown): StrategyState {
  if (raw == null) return { kind: 'missing' }
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    return { kind: 'malformed', detail: `expected an object, got ${Array.isArray(raw) ? 'an array' : typeof raw}` }
  }
  const obj = raw as Record<string, unknown>
  if (typeof obj.error === 'string' && obj.error) {
    return { kind: 'failed', detail: obj.error }
  }
  for (const [key, field] of VERSION_FIELDS) {
    const v = obj[field]
    if (typeof v !== 'string' || !(SUPPORTED[key] as readonly string[]).includes(v)) {
      return {
        kind: 'unsupported',
        detail: `${field} ${JSON.stringify(v)} is not renderable by this build ` +
          `(supported: ${SUPPORTED[key].join(', ')}).`,
      }
    }
  }
  const sim = obj.simulation as Record<string, unknown> | undefined
  if (!sim || typeof sim.n_sims !== 'number' || sim.n_sims < 1) {
    return { kind: 'malformed', detail: 'simulation.n_sims must name how many scenarios produced these probabilities' }
  }
  if (!Array.isArray(obj.leagues)) return { kind: 'malformed', detail: "'leagues' must be an array" }
  if (typeof obj.chips !== 'object' || obj.chips == null) {
    return { kind: 'malformed', detail: "'chips' must be an object" }
  }
  const ids = (obj.leagues as LeagueView[]).map((l) => l?.league_id)
  if (new Set(ids).size !== ids.length) {
    return { kind: 'malformed', detail: 'a league appears twice — one league’s ownership would render under another’s name' }
  }
  return { kind: 'ok', data: obj as unknown as Strategy }
}

/** ±1 standard error on a simulated probability, as a percentage. */
export function simError(n: number): number {
  return n > 0 ? (100 * 0.5) / Math.sqrt(n) : 0
}

export function pct(p: number | null | undefined, dp = 0): string {
  if (p == null || !Number.isFinite(p)) return '—'
  return `${(p * 100).toFixed(dp)}%`
}

export const CHIP_LABELS: Record<string, string> = {
  wildcard: 'Wildcard',
  freehit: 'Free Hit',
  bboost: 'Bench Boost',
  '3xc': 'Triple Captain',
  hold: 'Hold',
}

export const STANCE_LABELS: Record<string, string> = {
  protect: 'Protect the lead',
  neutral: 'Play the points',
  chase: 'Chase',
  desperate: 'All-in',
}

/**
 * What kind of league this is. Nothing more.
 *
 * `tiny_private` used to read "every rival readable", which is a claim about
 * *coverage*, not about class. It sat directly above "0/3 rival squads known"
 * in production and contradicted it. Classification is decided from league size
 * before a single rival squad is fetched, so it cannot know what was readable.
 * Coverage wording now comes from `describeCoverage` and the real counts.
 */
export const CLASS_LABELS: Record<string, string> = {
  tiny_private: 'Tiny private league',
  small_private: 'Small private league',
  medium: 'Medium league',
  large: 'Large league — bounded cohort',
  global: 'Global league — bounded cohort',
}

/** How much of the rival field we actually read. Drives wording and colour. */
export type CoverageLevel = 'no_rivals' | 'none' | 'partial' | 'full' | 'inconsistent'

export interface Coverage {
  level: CoverageLevel
  /** The counts, stated plainly: "0 of 3 rival squads known". */
  summary: string
  /** What that does to the numbers. Empty only when there is nothing to add. */
  meaning: string
  /** Status and truncation notes from the artifact, already worded. */
  notes: string[]
}

/** Per-rival fetch outcomes, as `league.py` records them. */
const STATUS_NOTES: Record<string, string> = {
  no_public_picks_yet: 'picks are not public yet',
  revealed: 'picks read from the API',
  stale: 'picks carried from an earlier gameweek',
  fetch_failed: 'some picks could not be fetched',
  unavailable: 'some entries are private',
}

/**
 * Coverage wording built from the counts, never from the classification.
 *
 * The one rule that matters: nothing here claims the field is readable unless
 * `with_picks` actually reaches `rivals`. An inconsistent artifact (more squads
 * read than rivals counted) is reported as inconsistent rather than rounded up
 * into full coverage — a broken producer must not read as a good result.
 */
export function describeCoverage(dq: DataQuality): Coverage {
  const rivals = Math.max(0, Math.trunc(dq.rivals ?? 0))
  const known = Math.max(0, Math.trunc(dq.with_picks ?? 0))

  const notes: string[] = []
  for (const st of dq.statuses ?? []) {
    const note = STATUS_NOTES[st]
    if (note && !notes.includes(note)) notes.push(note)
  }
  if (dq.cohort_truncated) notes.push('rival cohort capped, so this is a sample of the league')
  if (dq.picks_source_event) notes.push(`squads as at GW${dq.picks_source_event}`)

  // `coverage_pct` is the producer's own summary of the same two counts. If it
  // disagrees with them, one of the three numbers is wrong and we do not get to
  // pick the flattering one.
  const impliedPct = rivals > 0 ? (known / rivals) * 100 : 0
  const statedPct = Number.isFinite(dq.coverage_pct) ? dq.coverage_pct : null
  const pctDisagrees = statedPct !== null && Math.abs(statedPct - impliedPct) > 1

  if (known > rivals) {
    return {
      level: 'inconsistent',
      summary: `${known} rival squads known but only ${rivals} rivals counted`,
      meaning: 'the coverage figures disagree, so treat these probabilities as unreliable',
      notes,
    }
  }
  if (pctDisagrees) {
    return {
      level: 'inconsistent',
      summary: `${known} of ${rivals} rival squads known, but the artifact reports ${statedPct!.toFixed(0)}% coverage`,
      meaning: 'the coverage figures disagree, so treat these probabilities as unreliable',
      notes,
    }
  }
  if (rivals === 0) {
    return {
      level: 'no_rivals',
      summary: 'No rivals in this league',
      meaning: 'there is no field to place against',
      notes,
    }
  }
  if (known === 0) {
    return {
      level: 'none',
      summary: `0 of ${rivals} rival squads known`,
      meaning: `all ${rivals} were modelled as a distribution, not as teams`,
      notes,
    }
  }
  if (known < rivals) {
    const unknown = rivals - known
    return {
      level: 'partial',
      summary: `${known} of ${rivals} rival squads known`,
      meaning: `the other ${unknown} ${unknown === 1 ? 'was' : 'were'} modelled as a distribution, not as teams`,
      notes,
    }
  }
  return {
    level: 'full',
    summary: `All ${rivals} rival squads known`,
    meaning: 'every rival is read from their actual picks',
    notes,
  }
}

/** Which league most wants a different decision from the neutral one. */
export function departures(s: Strategy): LeagueView[] {
  return s.leagues.filter((l) => l.differs_from_neutral)
}

/**
 * The expected-points cost of following a league-specific option instead of the
 * highest-EP one. Naming this number is the whole point: a rank-protection move
 * is a trade, and the size of the trade must be visible.
 */
export function epCost(s: Strategy, key: string): number {
  if (!s.options.length) return 0
  const best = Math.max(...s.options.map((o) => o.expected_points))
  const chosen = s.options.find((o) => o.key === key)
  return chosen ? best - chosen.expected_points : 0
}
