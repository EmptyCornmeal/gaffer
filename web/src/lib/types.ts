export type Pos = 'GKP' | 'DEF' | 'MID' | 'FWD'

export interface Breakdown {
  appearance: number
  goals: number
  assists: number
  clean_sheet: number
  defcon: number
  bonus: number
  /** Goalkeeper save points. Optional so an older artifact still parses. */
  saves?: number
  /** Goals conceded + cards + own goals/penalties: usually negative. */
  other?: number
}

export interface Tag {
  label: string
  kind: 'good' | 'info' | 'warn' | 'bad'
}

export interface XminsBadge {
  label: string
  kind: 'good' | 'warn' | 'bad'
  hint: string
}

export interface TeamFixture {
  gw: number
  opp: string
  home: boolean
  difficulty: number
  att?: number
  def?: number
}

export interface PricePred {
  dir: 'up' | 'down' | 'stable'
  momentum: number
  progress?: number // 0..1 estimated share of the price-change threshold
  threshold?: number
}

export interface LastSeason {
  /** The season the sample came from, or null when that was never recorded. */
  season: string | null
  /**
   * Is this the season just gone?
   *
   * FPL's `history_past` holds the most recent season it has for a player, which
   * for someone who has been abroad can be years old. false means the numbers
   * are real but stale; null means the provenance is unknown, which is not the
   * same as "current".
   */
  is_prior_season?: boolean | null
  minutes: number
  starts: number
  xg90: number
  xa90: number
}

export interface Dist {
  mean: number
  floor: number
  ceiling: number
  boom: number
  std: number
}

export interface DefconView {
  p_hit: number
  per90: number
  threshold: number
  near_hit: boolean
}

export interface Player {
  id: number
  code: number | null
  name: string
  full_name: string
  team: string
  team_id: number
  team_code: number | null
  pos: Pos
  price: number
  owned_by: number
  net_transfers: number
  cost_change_event: number
  price_pred: PricePred
  status: string | null
  news: string
  set_pieces: string
  form: number
  ict: number
  last_season: LastSeason | null
  dist: Dist | null
  defcon: DefconView | null
  xgi90: number
  defcon90: number
  next_gw_xp: number
  /** Gaffer's own component sum. Equals `next_gw_xp` unless the h=1 blend ran. */
  model_xp?: number | null
  /** FPL's published one-week number, for reference. Never shown as ours. */
  ep_next_xp?: number | null
  horizon_xp: number
  xp_window: number
  gw_xp: { gw: number; xp: number }[]
  p_start: number
  confidence: number
  xmins_badge: XminsBadge
  rationale: string
  tags: Tag[]
  fixtures: TeamFixture[]
  breakdown: Breakdown
}

export interface Meta {
  current_gw: string
  gw_name: string
  deadline: string
  last_finished_gw: string
  /** Machine-readable squad state — see `lib/squadStatus.ts`. Never infer it from a date. */
  squad_status: string
  squad_status_reason?: string | null
  squad_source_event?: number | string | null
  entry_name: string | null
  manager_name: string | null
  overall_rank: string | null
  overall_points?: string | null
  /** 'component_only' | 'blended' — which h=1 number was published, and why. */
  projection_regime?: string | null
  projection_regime_reason?: string | null
  ep_next_blend_weight?: string | null
  /** Whether the live FPL scoring table matched the rules Gaffer models. */
  rule_scoring_source?: string | null
  rule_scoring_status?: string | null
  rule_scoring_drift?: string | null
  bank: string | null
  team_value: string | null
  active_chip: string | null
  model_version: string
  /** Pipeline run timestamp (ISO 8601, UTC). Shared with recommendation/plan. */
  generated_at: string | null
  season: string
  /** 'personalised' when an entry id was configured, otherwise 'generic'. */
  build_mode?: 'personalised' | 'generic'
  entry_id?: number | null
  league_ids?: number[]
}

export interface RecPlayer {
  id: number
  code: number | null
  name: string
  team: string
  team_code?: number | null
  pos: Pos
  price: number
  next_gw_xp: number
  confidence: number
  rationale?: string
  tags?: Tag[]
  xmins_badge?: XminsBadge
}

export interface OptimalExplanation {
  headline: string
  bullets: string[]
}

export type RiskStance = 'differential' | 'balanced' | 'template'

export interface OptimalHorizon {
  horizon: number
  label: string
  risk: RiskStance
  status: string
  formation: string
  squad_value: number
  xi_expected: number
  captain: RecPlayer
  vice: RecPlayer
  starting: RecPlayer[]
  bench: RecPlayer[]
  explanation: OptimalExplanation
}

export interface HorizonBlock {
  horizon: number
  label: string
  default_risk: RiskStance
  /**
   * Why the three risk stances currently return the same answer. The pipeline
   * has always written this field and the Planner has never rendered it, so the
   * toggle presented three choices the solver was not actually making. Optional
   * because an artifact published before this was read will not carry the key.
   */
  risk_note?: string
  by_risk: Record<RiskStance, OptimalHorizon>
}

export interface Recommendation {
  /** Same pipeline run timestamp as meta.generated_at. */
  generated_at?: string
  mode: string
  status: string
  formation: string
  squad_value: number
  xi_expected: number
  captain: RecPlayer
  vice: RecPlayer
  starting: RecPlayer[]
  bench: RecPlayer[]
  transfers_in: RecPlayer[]
  transfers_out: RecPlayer[]
  hits: number
  summary: string
  by_horizon?: Record<string, HorizonBlock>
}

export interface PlanStep {
  gw: number
  xi_expected: number
  free_transfers: number
  hits: number
  captain: RecPlayer
  vice: RecPlayer
  transfers_in: RecPlayer[]
  transfers_out: RecPlayer[]
  starting: RecPlayer[]
  bench: RecPlayer[]
}

export interface TransferPlan {
  /** Same pipeline run timestamp as meta.generated_at. */
  generated_at?: string
  status: string
  mode: string
  horizon: number
  total_expected: number
  steps: PlanStep[]
}

// The fixtures artifact is NOT a bare map of team records: it also carries
// `season`. Its type and its one type-guard live in `lib/fixtures.ts`, so this
// alias is deliberately gone — a `Record<string, {team, fixtures}>` was a lie
// that let the page dereference the season string.

export interface MyTeam {
  gw: number
  players: Player[]
}

export interface Verdict {
  briefing_md: string
  model: string | null
  source: string
  generated_at: string
}

export interface NewsItem {
  id: string
  source: string
  title: string
  summary: string
  link: string
  published: string
}

/**
 * One generated statement, with the items that support it.
 *
 * `source_item_ids` is the whole point: a claim the model produced is only
 * rendered beside the headlines it came from, and it can never carry a URL of
 * its own — links are resolved from `News.items`.
 */
export interface NewsClaim {
  text: string
  source_item_ids: string[]
  claim_type: 'transfer' | 'injury' | 'availability' | 'selection' | 'other'
  certainty: 'confirmed' | 'reported' | 'rumoured'
  players?: string[]
  teams?: string[]
}

export interface News {
  news_version?: string
  items: NewsItem[]
  claims?: NewsClaim[]
  digest_md: string
  /** 'ai' or 'template'. The reason lives in `fallback_reason`. */
  source: 'ai' | 'template'
  fallback_reason: string | null
  model: string | null
  count: number
  quarantined?: Array<{ id: string; source?: string; reason: string }>
  generated_at: string
}

// Backtest types live in lib/backtest.ts, which owns the versioned schema
// and its validation. The legacy interface here described the superseded
// ml-vs-heuristic artifact and has been removed.
