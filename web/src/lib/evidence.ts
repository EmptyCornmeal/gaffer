/**
 * 4.7 / 4.8 -- attach the measurement to the claim, and make the measurement
 * reachable.
 *
 * Gaffer grades itself thoroughly and then publishes the grades on a page no
 * number links to. A badge reading NAILED is a one-word confidence statement
 * in capital letters; the archive says that badge over-claims by five points,
 * and until now nothing on the screen showing the badge said so.
 *
 * Two jobs, both joins:
 *
 *   - a badge or a projection, joined to what the backtest measured about it;
 *   - a graded quantity, joined to the Model-page section that grades it.
 *
 * Nothing here computes a grade. Every number comes from an artifact the
 * pipeline generated and the contract validated.
 */
import type { Meta, Player } from './types'

// ---------------------------------------------------------------------------
// 4.8 -- where each graded number is graded
// ---------------------------------------------------------------------------

/**
 * Model-page section ids, as the page itself defines them.
 *
 * Keyed by the quantity a reader is looking at when they want the evidence,
 * not by the section's own name: the reader has a number in front of them and
 * wants to know whether to believe it.
 */
export const MODEL_SECTION = {
  minutes: 'acc-minutes',
  badge: 'acc-minutes',
  xp: 'acc-horizon',
  season: 'acc-inseason',
  cleanSheet: 'acc-withdrawn',
  decisions: 'acc-decisions',
} as const

export type GradedQuantity = keyof typeof MODEL_SECTION

/**
 * A deep link to the section of the Model page that grades this quantity.
 *
 * `#/model/<section>`: `normaliseRoute` already splits on `/`, so the route
 * resolves and the trailing segment is free to carry the anchor. A plain
 * `#acc-minutes` cannot work here -- the hash is the router.
 */
export function modelLink(what: GradedQuantity): string {
  return `#/model/${MODEL_SECTION[what]}`
}

// ---------------------------------------------------------------------------
// 4.7 -- the badge carries its own calibration
// ---------------------------------------------------------------------------

export interface BadgeBand {
  claimed?: number
  start_rate?: number
  appear_rate?: number
  n?: number
  over_claims_by?: number
}

export interface BadgeCalibration {
  available?: boolean
  population?: string
  source?: string
  means?: string
  bands?: Record<string, BadgeBand>
}

const pct = (v: number | undefined | null): string =>
  v === undefined || v === null || !Number.isFinite(v) ? '—' : `${Math.round(v * 100)}%`

/**
 * What this badge claimed in the archive, and what the badged players did.
 *
 * Returns null rather than a placeholder when the measurement is absent: an
 * ungraded badge should look ungraded, not confidently zero.
 */
export function badgeCaption(meta: Meta | null | undefined, label: string | undefined): string | null {
  const cal = meta?.badge_calibration
  if (!cal?.available || !label) return null
  const band = cal.bands?.[label]
  if (!band || band.claimed === undefined || band.start_rate === undefined) return null
  const n = typeof band.n === 'number' ? band.n.toLocaleString() : '—'
  return `${label} claims ${pct(band.claimed)}, started ${pct(band.start_rate)} (n=${n}, ${cal.population ?? 'measured'})`
}

/** The signed error, for a caller that wants to colour the gap rather than print it. */
export function badgeError(meta: Meta | null | undefined, label: string | undefined): number | null {
  const band = label ? meta?.badge_calibration?.bands?.[label] : undefined
  const v = band?.over_claims_by
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

// ---------------------------------------------------------------------------
// 4.2 -- evidence quality, in words
// ---------------------------------------------------------------------------

export interface EvidenceQuality {
  weak_evidence_share?: number
  largest_weak_component?: string | null
  largest_weak_status?: string | null
}

/** Human names for the components; the artifact keys are snake_case. */
const COMPONENT_LABEL: Record<string, string> = {
  appearance: 'appearance points',
  goals: 'goals',
  assists: 'assists',
  clean_sheet: 'clean sheets',
  defcon: 'defensive contribution',
  bonus: 'bonus',
  saves: 'saves',
  other: 'cards and other',
}

/**
 * "62% of this rests on weakly evidenced components — mostly clean sheets".
 *
 * Deliberately not called confidence anywhere in this file. It says how well
 * evidenced the number is, not how likely it is to be right.
 */
export function evidenceCaption(eq: EvidenceQuality | null | undefined): string | null {
  const share = eq?.weak_evidence_share
  if (typeof share !== 'number' || !Number.isFinite(share)) return null
  const biggest = eq?.largest_weak_component
  const tail = biggest ? ` — mostly ${COMPONENT_LABEL[biggest] ?? biggest}` : ''
  return `${pct(share)} of this projection rests on components Gaffer has measured and found wanting${tail}`
}

/**
 * A three-step severity for styling. Thresholds are a DECLARED PRESENTATION
 * CHOICE -- nothing fitted them, and they change no number. They exist so a
 * reader can see at a glance that a defender's total and a striker's total are
 * not the same kind of quantity.
 */
export function evidenceTone(eq: EvidenceQuality | null | undefined): 'good' | 'warn' | 'bad' | null {
  const share = eq?.weak_evidence_share
  if (typeof share !== 'number' || !Number.isFinite(share)) return null
  if (share >= 0.5) return 'bad'
  if (share >= 0.25) return 'warn'
  return 'good'
}

/** Convenience for a player row. */
export function playerEvidence(p: Pick<Player, 'evidence_quality'> | null | undefined): EvidenceQuality | null {
  return p?.evidence_quality ?? null
}
