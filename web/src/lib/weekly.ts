// Weekly decision, live gameweek and review artifacts (T-21/T-22/T-23).
//
// Each parser refuses a version it cannot interpret rather than rendering it.
// These artifacts carry advice, live scores and judgements about past decisions;
// a stale schema rendered optimistically is worse than an explanatory error,
// because the user cannot tell the difference by looking.

// ---------------------------------------------------------------------------
// Shared
// ---------------------------------------------------------------------------

export interface Card {
  id: number
  name?: string
  team?: string | null
  pos?: string | null
  price?: number | null
  code?: number | null
  team_code?: number | null
  next_gw_xp?: number | null
}

export type ParseState<T> =
  | { kind: 'ok'; data: T }
  | { kind: 'missing' }
  | { kind: 'unsupported'; detail: string }
  | { kind: 'malformed'; detail: string }

function object(raw: unknown): Record<string, unknown> | string {
  if (raw == null) return 'missing'
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    return `expected an object, got ${Array.isArray(raw) ? 'an array' : typeof raw}`
  }
  return raw as Record<string, unknown>
}

// ---------------------------------------------------------------------------
// Decision (T-21)
// ---------------------------------------------------------------------------

export const SUPPORTED_WEEKLY = ['weekly-1.0'] as const
export const SUPPORTED_DECISION = ['decision-1.0'] as const

export type Action = 'transfer' | 'roll' | 'too_close' | 'unavailable'
export const ALL_ACTIONS: Action[] = ['transfer', 'roll', 'too_close', 'unavailable']

export interface DecisionComparison {
  move_expected: number
  hold_expected: number
  delta: number
  delta_ci95: [number, number]
  p_move_beats_hold: number
  simulations: number
  short_term_delta: number
  horizon_delta: number | null
  hit_cost: number
}

export interface Executability {
  affordable: boolean
  bank_before: number | null
  bank_after: number | null
  bank_before_m: number | null
  bank_after_m: number | null
  cost_m: number
  recouped_m: number
  free_transfers_before: number
  free_transfers_after: number
  paid_transfers: number
  reason: string
}

export interface CandidateMove {
  status: 'evidence_only'
  basis: 'future_horizon'
  label: string
  reason: string
  transfers_in: Card[]
  transfers_out: Card[]
  captain: Card | null
  vice: Card | null
  executability: Executability
}

export interface DecisionBody {
  action: Action
  headline: string
  reason: string
  transfers_in: Card[]
  transfers_out: Card[]
  captain: Card | null
  vice: Card | null
  starting: Card[]
  bench: Card[]
  comparison: DecisionComparison | null
  executability: Executability | null
  chip: Record<string, unknown> | null
  league_note: string
  confidence: 'high' | 'medium' | 'low' | 'unknown'
  biggest_risk: string
  assumptions: string[]
  candidate_move?: CandidateMove | null
}

export interface WeeklyDecision {
  weekly_version: string
  decision_version: string
  generated_at: string
  gameweek: number
  horizon: number
  squad_state: {
    known: boolean
    status: string | null
    source_event: number | null
    players: Card[]
    captain: number | null
    vice: number | null
  }
  decision: DecisionBody
  versions: Record<string, unknown>
  chip: Record<string, unknown> | null
  leagues: Array<Record<string, unknown>>
  freshness: Record<string, unknown>
  overrides: Record<string, unknown>
}

export function parseDecision(raw: unknown): ParseState<WeeklyDecision> {
  const obj = object(raw)
  if (obj === 'missing') return { kind: 'missing' }
  if (typeof obj === 'string') return { kind: 'malformed', detail: obj }

  for (const [field, supported] of [
    ['weekly_version', SUPPORTED_WEEKLY],
    ['decision_version', SUPPORTED_DECISION],
  ] as const) {
    const v = obj[field]
    if (typeof v !== 'string' || !(supported as readonly string[]).includes(v)) {
      return {
        kind: 'unsupported',
        detail: `${field} ${JSON.stringify(v)} is not renderable by this build ` +
          `(supported: ${supported.join(', ')}).`,
      }
    }
  }
  const body = obj.decision as DecisionBody | undefined
  if (!body || typeof body !== 'object') {
    return { kind: 'malformed', detail: "'decision' must be an object" }
  }
  if (!ALL_ACTIONS.includes(body.action)) {
    return { kind: 'malformed', detail: `unknown action ${JSON.stringify(body.action)}` }
  }
  if (!body.headline) {
    return { kind: 'malformed', detail: 'the decision has no headline' }
  }
  return { kind: 'ok', data: obj as unknown as WeeklyDecision }
}

export const ACTION_LABELS: Record<Action, string> = {
  transfer: 'Make this transfer',
  roll: 'Roll your transfer',
  too_close: 'Too close to call',
  unavailable: 'No recommendation available',
}

/**
 * Chip tone per action.
 *
 * `bad` is this app's *fault* colour — expired data, a failed leakage check —
 * so it is reserved for something actually being wrong. `unavailable` is not
 * wrong: before the first deadline FPL publishes nobody's picks, and the app
 * saying so is it working correctly. It gets `neutral`, the uncoloured chip, so
 * an honest limit cannot be mistaken for a broken pipeline at a glance.
 * `too_close` stays `warn` — that one really is a caution about the advice.
 */
export const ACTION_TONE: Record<Action, string> = {
  transfer: 'good',
  roll: 'info',
  too_close: 'warn',
  unavailable: 'neutral',
}

/** Does the comparison clear the bar the backend used? Purely for wording. */
export function isActionable(d: WeeklyDecision): boolean {
  return d.decision.action === 'transfer'
}

/** Say what the confidence applies to. A narrow CI around a negative move is
 * high confidence IN HOLDING; the bare phrase "high confidence" beside
 * "too close" was the live A4 contradiction. */
export function confidenceLabel(body: DecisionBody): string {
  if (body.confidence === 'unknown') return 'confidence unknown'
  if (body.action === 'roll') return `${body.confidence} confidence in holding`
  if (body.action === 'transfer') return `${body.confidence} confidence in the move`
  if (body.action === 'too_close') return `${body.confidence} confidence in the comparison`
  return `${body.confidence} confidence`
}

// ---------------------------------------------------------------------------
// Live (T-22)
// ---------------------------------------------------------------------------

export const SUPPORTED_LIVE = ['live-1.0'] as const

export type FixtureStateName =
  | 'scheduled' | 'live' | 'half_time' | 'awaiting_bonus'
  | 'finished' | 'postponed' | 'abandoned'

export interface LiveFixture {
  id: number
  event: number | null
  team_h: number
  team_a: number
  state: FixtureStateName
  minutes: number
  kickoff: string | null
  bonus_final: boolean
}

export interface LivePlayer {
  id: number
  name: string
  pos: string | null
  minutes: number
  confirmed: number
  provisional: number
  predicted: number
  total: number
  played: boolean
  finished: boolean
  yet_to_play: boolean
  in_xi: boolean
  is_captain: boolean
  fixture_states: string[]
}

export interface LiveRival {
  entry_id: number | null
  name: string
  you: boolean
  current: number
  projected: number
  gw_points: number
  yet_to_play: number
  provisional_position: number
}

export interface LiveState {
  live_version: string
  gameweek: number
  as_of: string | null
  generated_at?: string
  available: boolean
  unavailable_reason: string | null
  note?: string
  fixtures: LiveFixture[]
  fixture_summary: {
    total: number
    by_state: Record<string, number>
    all_finished: boolean
    bonus_final: boolean
  }
  active_chip?: string | null
  squad?: {
    confirmed: number
    provisional_bonus: number
    predicted_remaining: number
    current: number
    projected: number
    bench_points: number
    players_played: number
    players_yet_to_play: number
    hits: number
    // Both are null until the entry history is readable — `pipeline.py:449-450`
    // and `live/source.ts:320-321` set them so, and `source.test.ts:346` asserts
    // it. Typing them as `number` told every caller a null could not arrive.
    season_total_before: number | null
    season_total_projected: number | null
    autosubs: {
      xi: number[]
      bench: number[]
      subs_in: number[]
      subs_out: number[]
      captain: number | null
      captain_source: 'captain' | 'vice' | 'none'
      multiplier: number
      provisional: boolean
      notes: string[]
    }
  }
  players?: LivePlayer[]
  rivals?: LiveRival[]
  largest_swing?: {
    player_id: number
    name: string
    swing: number
    in_your_xi: boolean
    against: number | null
    note: string
  } | null
  separation?: {
    confirmed: number
    provisional_bonus: number
    predicted_remaining: number
    note: string
  }
}

export function parseLive(raw: unknown): ParseState<LiveState> {
  const obj = object(raw)
  if (obj === 'missing') return { kind: 'missing' }
  if (typeof obj === 'string') return { kind: 'malformed', detail: obj }
  const v = obj.live_version
  if (typeof v !== 'string' || !(SUPPORTED_LIVE as readonly string[]).includes(v)) {
    return {
      kind: 'unsupported',
      detail: `live_version ${JSON.stringify(v)} is not renderable by this build ` +
        `(supported: ${SUPPORTED_LIVE.join(', ')}).`,
    }
  }
  if (typeof obj.available !== 'boolean') {
    return { kind: 'malformed', detail: "'available' must be an explicit boolean" }
  }
  if (!obj.available && !obj.unavailable_reason) {
    return { kind: 'malformed', detail: 'unavailable live data must say why' }
  }
  return { kind: 'ok', data: obj as unknown as LiveState }
}

export const FIXTURE_STATE_LABELS: Record<FixtureStateName, string> = {
  scheduled: 'Not started',
  live: 'Live',
  half_time: 'Half time',
  awaiting_bonus: 'FT — bonus pending',
  finished: 'Final',
  postponed: 'Postponed',
  abandoned: 'Abandoned',
}

export const LIVE_UNAVAILABLE_LABELS: Record<string, string> = {
  no_gameweek: 'No fixtures are scheduled for this gameweek.',
  not_started: 'No match has kicked off yet.',
  no_squad: 'Your squad is not readable, so there is nothing to score.',
  no_live_data: 'Matches have started but FPL is not serving player data yet.',
}

// ---------------------------------------------------------------------------
// Review (T-23)
// ---------------------------------------------------------------------------

export const SUPPORTED_REVIEW = ['review-1.0'] as const

export type Verdict =
  | 'good_decision' | 'good_decision_lucky' | 'good_decision_unlucky'
  | 'bad_decision' | 'bad_decision_lucky' | 'bad_decision_unlucky'
  | 'not_assessable'

export interface ReviewState {
  review_version: string
  schema_version: number
  season: string
  entry_id: number
  event: number
  generated_at: string
  snapshot_as_of: string | null
  has_snapshot: boolean
  comparison: {
    recommended_points: number | null
    actual_points: number | null
    hold_points: number | null
    hindsight_points: number | null
    hindsight_is_unknowable: boolean
    followed_advice: boolean | null
    note: string
  }
  attribution: Record<string, number>
  quality: {
    expected_at_decision: number | null
    realised: number | null
    outcome_percentile: number | null
    positive_ev: boolean | null
    verdict: Verdict
    explanation: string
  }
  lesson: { key: string; text: string; evidence: Array<Record<string, unknown>>; weeks: number } | null
  league: Array<Record<string, unknown>>
  limitations: string[]
  facts?: Record<string, unknown>
}

export function parseReview(raw: unknown): ParseState<ReviewState> {
  const obj = object(raw)
  if (obj === 'missing') return { kind: 'missing' }
  if (typeof obj === 'string') return { kind: 'malformed', detail: obj }
  const v = obj.review_version
  if (typeof v !== 'string' || !(SUPPORTED_REVIEW as readonly string[]).includes(v)) {
    return {
      kind: 'unsupported',
      detail: `review_version ${JSON.stringify(v)} is not renderable by this build ` +
        `(supported: ${SUPPORTED_REVIEW.join(', ')}).`,
    }
  }
  if (typeof obj.event !== 'number') {
    return { kind: 'malformed', detail: "'event' must be the gameweek number" }
  }
  const q = obj.quality as Record<string, unknown> | undefined
  if (!q || typeof q.verdict !== 'string') {
    return { kind: 'malformed', detail: "'quality.verdict' is required" }
  }
  return { kind: 'ok', data: obj as unknown as ReviewState }
}

export const VERDICT_LABELS: Record<Verdict, string> = {
  good_decision: 'Good decision',
  good_decision_lucky: 'Good decision, lucky result',
  good_decision_unlucky: 'Good decision, unlucky result',
  bad_decision: 'Poor decision',
  bad_decision_lucky: 'Poor decision, lucky result',
  bad_decision_unlucky: 'Poor decision, unlucky result',
  not_assessable: 'Not assessable',
}

export const VERDICT_TONE: Record<Verdict, string> = {
  good_decision: 'good',
  good_decision_lucky: 'good',
  good_decision_unlucky: 'good',
  bad_decision: 'bad',
  bad_decision_lucky: 'warn',
  bad_decision_unlucky: 'bad',
  not_assessable: 'info',
}

export const ATTRIBUTION_LABELS: Record<string, string> = {
  starting_xi: 'Starting XI',
  captaincy: 'Captaincy',
  bench: 'Left on the bench',
  transfers: 'Transfers',
  hit_cost: 'Hits',
  chip: 'Chip',
  autosubs: 'Autosubs',
}

// ---------------------------------------------------------------------------
// Notifications (T-24)
// ---------------------------------------------------------------------------

export interface NotificationState {
  notify_version: string
  generated_at: string
  result: {
    considered: number
    new: number
    duplicates: number
    suppressed: number
    delivered: number
    failed: number
    dry_run: boolean
    alerts: Array<{
      kind: string
      title: string
      body: string
      severity: string
      deep_link: string
      state: string
      reason?: string
    }>
    errors: string[]
  }
  config: { sink: string; configured: boolean; missing_env: string[] }
  summary: Record<string, unknown>
}

export function parseNotifications(raw: unknown): ParseState<NotificationState> {
  const obj = object(raw)
  if (obj === 'missing') return { kind: 'missing' }
  if (typeof obj === 'string') return { kind: 'malformed', detail: obj }
  const r = obj.result as Record<string, unknown> | undefined
  if (!r || typeof r.dry_run !== 'boolean') {
    return { kind: 'malformed', detail: "'result.dry_run' must be an explicit boolean" }
  }
  return { kind: 'ok', data: obj as unknown as NotificationState }
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

export function signed(n: number | null | undefined, dp = 1): string {
  if (n == null || !Number.isFinite(n)) return '—'
  return `${n >= 0 ? '+' : ''}${n.toFixed(dp)}`
}

export function pctOf(p: number | null | undefined, dp = 0): string {
  if (p == null || !Number.isFinite(p)) return '—'
  return `${(p * 100).toFixed(dp)}%`
}

export function money(tenths: number | null | undefined): string {
  if (tenths == null || !Number.isFinite(tenths)) return 'unknown'
  return `£${(tenths / 10).toFixed(1)}m`
}
