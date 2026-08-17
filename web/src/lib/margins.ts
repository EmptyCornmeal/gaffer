// How much each individual pick actually matters.
//
// `recommendation.json` carries an exact near-optimal margin for every squad
// member: the objective cost, over the planning horizon, of the best legal
// squad that does NOT contain him. The pipeline measures it by re-solving the
// MILP once per player with that player forced out (solver/optimize.py,
// `squad_margins`), so it is a measured number, not a heuristic layered on xP.
//
// Why it belongs on screen: projected points and marginal value are different
// quantities and they routinely disagree. On the 2026-27 GW1 build Thiaw
// projects 27.59 xP over six gameweeks and his slot is worth 0.621; Calafiori
// projects 21.36 and his is worth 0.688. A squad rendered as fifteen equal
// tiles, or sorted by xP, says nothing about which slots are actually
// contested — and five of those fifteen were worth under half a point.
import type { Margin, MarginBandName, RecPlayer, Recommendation } from './types'

export interface Band {
  name: MarginBandName
  label: string
  /** One line the reader can act on, not a definition of the metric. */
  hint: string
  /** Tailwind classes for the chip. */
  tone: string
}

// Where the bands split, and why these numbers rather than round ones.
//
// SPINE at 4.0 is the cost of an FPL points hit. A slot worth more than a hit
// is one you would rationally pay -4 to keep, which is what makes it structural
// rather than a preference.
//
// FREE at 0.5 is, over a six-gameweek horizon, under 0.1 points a week — well
// inside the projection model's own per-player error. Below that the solver is
// not expressing a preference, it is breaking a tie, and dressing that up as a
// recommendation would be inventing precision. It is also, on real data, where
// a third of the squad lands.
//
// The band between them is deliberately just "settled": a real but affordable
// difference. Splitting it further would need a measurement nobody has made.
export const SPINE_THRESHOLD = 4.0
export const FREE_THRESHOLD = 0.5

export const BANDS: Record<MarginBandName, Band> = {
  required: {
    name: 'required',
    label: 'Locked',
    hint: 'No legal squad exists without him under the budget, quota and club limits.',
    tone: 'bg-accent/15 text-accent-light',
  },
  spine: {
    name: 'spine',
    label: 'Spine',
    hint: 'Worth more than a -4 hit. Replacing him is a real loss, not a swap.',
    tone: 'bg-brand/15 text-brand-light',
  },
  settled: {
    name: 'settled',
    label: 'Settled',
    hint: 'A real edge over the next best option, but one you can afford to trade.',
    tone: 'bg-white/10 text-white/80',
  },
  free: {
    name: 'free',
    label: 'Free swap',
    hint: 'Inside the projection error. The solver is breaking a tie here, not choosing.',
    tone: 'bg-white/5 text-muted',
  },
}

/**
 * The band a margin falls in, or null when there is no honest answer.
 *
 * `not_computed` and `anomaly` return null on purpose: an unmeasured slot must
 * render as absent, never as "free swap". Treating a missing measurement as a
 * zero is exactly the dishonesty this feature exists to remove.
 */
export function marginBand(m: Margin | null | undefined): Band | null {
  if (!m) return null
  if (m.status === 'required') return BANDS.required
  if (m.status !== 'optimal' || m.points === null) return null
  if (m.points >= SPINE_THRESHOLD) return BANDS.spine
  if (m.points >= FREE_THRESHOLD) return BANDS.settled
  return BANDS.free
}

/** Two decimals throughout: `0.10` and `0.24` are different answers here. */
export function formatMargin(m: Margin | null | undefined): string {
  if (!m) return '—'
  if (m.status === 'required') return 'locked'
  if (m.status !== 'optimal' || m.points === null) return '—'
  return m.points.toFixed(2)
}

export interface RankedPick {
  player: RecPlayer
  margin: Margin
  band: Band | null
}

/**
 * The fifteen, ordered by how much they matter. Structurally required picks sort
 * to the top — they are the strongest possible answer to "does this one matter",
 * and they have no number to sort by.
 */
export function rankedPicks(rec: Recommendation): RankedPick[] {
  const rank = (m: Margin) =>
    m.status === 'required' ? Infinity : m.status === 'optimal' && m.points !== null ? m.points : -1
  return [...rec.starting, ...rec.bench]
    .filter((p): p is RecPlayer & { margin: Margin } => !!p.margin)
    .map((p) => ({ player: p, margin: p.margin, band: marginBand(p.margin) }))
    .sort((a, b) => rank(b.margin) - rank(a.margin))
}

/**
 * The one line the card leads with. Says what the spread IS rather than what a
 * margin means — the definition is a tooltip, the spread is the finding.
 */
export function marginHeadline(rec: Recommendation): string {
  const picks = rankedPicks(rec)
  if (!picks.length) return ''
  const gws = rec.margins?.horizon ?? 0
  const window = gws ? `${gws} GW${gws === 1 ? '' : 's'}` : 'the horizon'
  const free = picks.filter((p) => p.band?.name === 'free').length
  const top = picks[0]
  const lead =
    top.margin.status === 'required'
      ? `${top.player.name} cannot be replaced at all`
      : `${top.player.name} is worth ${formatMargin(top.margin)}`
  return free
    ? `${lead}; ${free} of ${picks.length} are worth under ${FREE_THRESHOLD} over ${window}.`
    : `${lead} over ${window}.`
}

/** True when the published block is worth rendering at all. */
export function marginsUsable(rec: Recommendation): boolean {
  const b = rec.margins
  return !!b && b.status !== 'unavailable' && rankedPicks(rec).length > 0
}
