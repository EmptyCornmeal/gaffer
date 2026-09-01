// Data-age classification, in one place.
//
// The site served 11-day-old projections while the topbar ticked a live deadline
// countdown, because nothing ever rendered `generated_at`. Freshness is a
// first-class UI state now, and the arithmetic lives here so no component can
// quietly disagree about what "stale" means.

export type FreshnessState =
  | 'fresh'
  | 'lagging'
  | 'stale'
  | 'critical'
  | 'expired'
  | 'unknown'

export interface Freshness {
  state: FreshnessState
  /** Age in ms; null when the timestamp is missing or unparseable. */
  ageMs: number | null
  /** Short label for the chip, e.g. "Updated 3h ago". */
  label: string
  /** Full precision for a tooltip / accessible name. */
  title: string
}

/**
 * P0.6 -- the staleness bars, as published by the pipeline.
 *
 * These used to be flat constants in this file: 12 hours "fresh", 36 hours
 * "critical". With a refresh asked for every 15 minutes that made 48 missed
 * cycles read as green, and on 2026-09-01 twenty-six hours of consecutive
 * publish failures rendered as a mild amber chip beside a live recommendation.
 *
 * `gaffer.schedule` already knew better and always had: a per-window bar,
 * tightened as a deadline closes in. One product was holding two disagreeing
 * definitions of "stale" -- a cardinality problem -- and the browser's was the
 * wrong one by two orders of magnitude.
 *
 * So the numbers now arrive on `meta.freshness_policy` and this module only
 * EVALUATES them. `FALLBACK_POLICY` exists for an artifact published before
 * this field did; it mirrors the Python values and must never drift from them.
 */
export interface FreshnessPolicy {
  pre_deadline_open_min: number
  final_approach_min: number
  max_age_min: Record<string, number>
}

export const FALLBACK_POLICY: FreshnessPolicy = {
  pre_deadline_open_min: 360,
  final_approach_min: 120,
  max_age_min: { final_approach: 20, pre_deadline: 90, live: 60, idle: 360 },
}

/** Which window a reader is in, from the deadline alone. */
export function freshnessWindow(
  now: number,
  deadline: number | null,
  policy: FreshnessPolicy,
): 'final_approach' | 'pre_deadline' | 'idle' {
  if (deadline == null || Number.isNaN(deadline)) return 'idle'
  const until = deadline - now
  if (until <= 0) return 'idle'
  if (until <= policy.final_approach_min * 60_000) return 'final_approach'
  if (until <= policy.pre_deadline_open_min * 60_000) return 'pre_deadline'
  return 'idle'
}

/**
 * Tiers, expressed as multiples of the window's own bar rather than as wall
 * clock. One bar late is drift; six bars late is a broken pipeline.
 */
export const LAGGING_BARS = 1
export const STALE_BARS = 2
export const CRITICAL_BARS = 6

/** Below this, data is current. */
export const FRESH_MS = 12 * 60 * 60 * 1000
/** At or above this, data is critically stale. */
export const STALE_MS = 36 * 60 * 60 * 1000
/** Tolerated clock skew before a future timestamp is treated as untrustworthy. */
export const SKEW_MS = 5 * 60 * 1000
/**
 * Inside this much of a deadline, "under twelve hours old" stops being good
 * enough: this is the window in which press conferences, late fitness news and
 * price changes land.
 */
export const DEADLINE_SOON_MS = 12 * 60 * 60 * 1000
/** Near a deadline, data older than this is not safe to decide on. */
export const DEADLINE_MAX_AGE_MS = 3 * 60 * 60 * 1000

function humanAge(ms: number): string {
  if (ms < 60_000) return 'just now'
  const m = Math.floor(ms / 60_000)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(ms / 3_600_000)
  if (h < 48) return `${h}h ago`
  return `${Math.floor(ms / 86_400_000)}d ago`
}

/**
 * Classify an artifact timestamp.
 *
 * `now` is a parameter rather than a `Date.now()` call so this is testable and
 * so every component in a render pass agrees on the current time.
 *
 * `deadline` is the gameweek this advice is *for*. Age alone is the wrong
 * question near one: the scheduled refresh drifts by up to an hour, so the last
 * publish before a 17:30 deadline can easily be the 11:45 one — five and a half
 * hours old, comfortably inside the twelve-hour "fresh" band, and yet predating
 * every team announcement that matters. And once the deadline has passed the
 * advice describes a gameweek nobody can change any more. Neither may render as
 * a reassuring green chip.
 */
export function classifyFreshness(
  generatedAt: string | null | undefined,
  now: number = Date.now(),
  deadline?: string | null,
  policy: FreshnessPolicy = FALLBACK_POLICY,
): Freshness {
  const unknown = (title: string): Freshness => ({
    state: 'unknown',
    ageMs: null,
    label: 'Data age unknown',
    title,
  })

  if (generatedAt == null || typeof generatedAt !== 'string' || !generatedAt.trim()) {
    return unknown('No generation timestamp in meta.json')
  }
  const ts = Date.parse(generatedAt)
  if (Number.isNaN(ts)) {
    return unknown(`Unparseable generation timestamp: ${generatedAt}`)
  }

  const ageMs = now - ts
  const iso = new Date(ts).toISOString().replace('.000', '')

  // A timestamp meaningfully in the future means one of the two clocks is wrong;
  // claiming "fresh" on that basis would be exactly the lie this feature exists
  // to prevent.
  if (ageMs < -SKEW_MS) {
    return {
      state: 'unknown',
      ageMs,
      label: 'Clock mismatch',
      title: `Data is timestamped in the future (${iso}) — check the pipeline clock`,
    }
  }

  const clamped = Math.max(0, ageMs)
  const dlMs = deadline == null ? null : Date.parse(deadline)
  const win = freshnessWindow(now, dlMs, policy)
  const barMs = (policy.max_age_min[win] ?? FALLBACK_POLICY.max_age_min.idle) * 60_000
  let state: FreshnessState =
    clamped <= barMs * LAGGING_BARS
      ? 'fresh'
      : clamped <= barMs * STALE_BARS
        ? 'lagging'
        : clamped <= barMs * CRITICAL_BARS
          ? 'stale'
          : 'critical'
  let label = `Updated ${humanAge(clamped)}`
  let title =
    `Data generated ${iso} (${humanAge(clamped)}). ` +
    `The bar in the ${win.replace('_', ' ')} window is ${humanAge(barMs)
      .replace(' ago', '')}.`

  const dl = deadline == null ? NaN : Date.parse(deadline)
  if (!Number.isNaN(dl)) {
    if (now > dl + SKEW_MS) {
      // The artifact still names a deadline that has already gone, which means
      // no refresh has run since it passed. Whatever is on screen is advice for
      // a gameweek that is now locked.
      return {
        state: 'expired',
        ageMs,
        label: 'Deadline passed',
        title:
          `This advice is for a deadline that passed at ${new Date(dl).toISOString().replace('.000', '')} ` +
          `and has not been refreshed since (data generated ${iso}). It can no longer be acted on.`,
      }
    }
    const untilDeadline = dl - now
    // Near a deadline the window bar above is already tight (20 or 90 minutes),
    // so anything past `fresh` here is not merely old -- it predates the last
    // team-news window and is not safe to decide on. Say that, rather than
    // showing the same amber chip as a quiet Tuesday.
    if (state !== 'fresh' && untilDeadline <= DEADLINE_SOON_MS) {
      label = `${humanAge(clamped)} · do not act`
      title =
        `Deadline in ${humanAge(untilDeadline)} but this data is ${humanAge(clamped)} old ` +
        `(generated ${iso}), over the ${humanAge(barMs).replace(' ago', '')} bar for the ` +
        `${win.replace('_', ' ')} window. It predates the last team-news window. ` +
        `Refresh before deciding.`
    }
  }

  return { state, ageMs, label, title }
}
