// Who owns what, across one mini-league.
//
// The number that decides a mini-league is not ownership but EFFECTIVE
// ownership: how many copies of a player's points are landing in other people's
// totals. Six managers owning him is a very different week from six owning him
// and four captaining him, and only the second number tells you what a haul
// costs you. `strategy.json` already computes this for the pipeline's own
// purposes; this is the same idea made visible, manager by manager.

import type { ManagerSquad } from './squads'

export interface GridRow {
  playerId: number
  /** Managers who held him at all, bench included. */
  held: number
  /** Managers for whom he actually scored (weight > 0). */
  scoring: number
  /** Managers who captained him. */
  captains: number
  /** Sum of weights across the league, as a percentage of managers. */
  effectiveOwnership: number
  /** Plain ownership, as a percentage of managers. */
  ownership: number
  /** entry → weight, for every manager who held him. */
  byEntry: Map<number, number>
  /** True when the viewer holds him and nobody else does. */
  yourDifferential: boolean
  /** True when everybody except the viewer holds him. */
  againstYou: boolean
  /** Whether the viewer holds him at all. */
  yours: boolean
}

/**
 * Build the ownership grid.
 *
 * `you` may be null — a visitor who has not set an entry id still gets a useful
 * league-wide picture, just without the two columns that are about them.
 */
export function ownershipGrid(
  squads: ManagerSquad[], you: number | null,
): GridRow[] {
  const n = squads.length
  if (!n) return []

  const rows = new Map<number, GridRow>()
  const row = (id: number): GridRow => {
    let r = rows.get(id)
    if (!r) {
      r = {
        playerId: id, held: 0, scoring: 0, captains: 0,
        effectiveOwnership: 0, ownership: 0, byEntry: new Map(),
        yourDifferential: false, againstYou: false, yours: false,
      }
      rows.set(id, r)
    }
    return r
  }

  for (const sq of squads) {
    for (const id of sq.held) {
      const r = row(id)
      const w = sq.weights.get(id) ?? 0
      r.held += 1
      r.byEntry.set(sq.entry, w)
      if (w > 0) r.scoring += 1
      // Weights are summed, not counted: a captain contributes two, which is
      // the entire point of the measure.
      r.effectiveOwnership += w
      if (id === sq.captain) r.captains += 1
      if (sq.entry === you) r.yours = true
    }
  }

  for (const r of rows.values()) {
    r.ownership = (r.held / n) * 100
    r.effectiveOwnership = (r.effectiveOwnership / n) * 100
    r.yourDifferential = r.yours && r.held === 1
    r.againstYou = !r.yours && r.held === n - (you != null ? 1 : 0) && r.held > 0
  }

  // Effective ownership descending, then plain ownership, then id — so the order
  // is total and two runs of the same league cannot disagree.
  return [...rows.values()].sort(
    (a, b) =>
      b.effectiveOwnership - a.effectiveOwnership ||
      b.held - a.held ||
      a.playerId - b.playerId,
  )
}

/**
 * Short, DISTINCT column headings for a set of managers.
 *
 * Truncating a name to its first three letters put "Nat Uttley" and "Nat Stubbs"
 * under the same heading — in a table whose entire job is telling managers
 * apart, which is not a cosmetic problem. Initials collide far less often, and
 * where they still do the surname is extended a letter at a time until they
 * stop. Only if two managers share a name outright does this fall back to a
 * numeric suffix, because at that point nothing shorter can separate them.
 */
export function columnLabels(names: Map<number, string>): Map<number, string> {
  const initials = (raw: string): string => {
    const parts = raw.trim().split(/\s+/).filter(Boolean)
    if (!parts.length) return '?'
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
  }
  const surname = (raw: string): string => {
    const parts = raw.trim().split(/\s+/).filter(Boolean)
    return parts.length > 1 ? parts[parts.length - 1] : (parts[0] ?? '')
  }

  const out = new Map<number, string>()
  for (const [entry, name] of names) out.set(entry, initials(name))

  // Widen every colliding group together, so the headings in one column stay
  // the same length as each other and the table does not look ragged.
  for (let extra = 1; extra <= 6; extra++) {
    const byLabel = new Map<string, number[]>()
    for (const [entry, label] of out) {
      const list = byLabel.get(label)
      if (list) list.push(entry)
      else byLabel.set(label, [entry])
    }
    const clashing = [...byLabel.values()].filter((g) => g.length > 1).flat()
    if (!clashing.length) break
    for (const entry of clashing) {
      const name = names.get(entry) ?? ''
      const head = initials(name)
      out.set(entry, (head + surname(name).slice(1, 1 + extra)).toUpperCase())
    }
  }

  // Two managers with the identical name cannot be separated by their name.
  const seen = new Map<string, number>()
  for (const [entry, label] of out) {
    const n = seen.get(label) ?? 0
    seen.set(label, n + 1)
    if (n > 0) out.set(entry, `${label}${n + 1}`)
  }
  return out
}

/**
 * How alike two squads are: the share of one manager's holdings the other also
 * holds. Symmetric, because it divides by the union.
 */
export function overlap(a: ManagerSquad, b: ManagerSquad): number {
  if (!a.held.size || !b.held.size) return 0
  let shared = 0
  for (const id of a.held) if (b.held.has(id)) shared += 1
  const union = a.held.size + b.held.size - shared
  return union ? (shared / union) * 100 : 0
}

/**
 * The players that separate you from one rival this gameweek, by how many copies
 * of their points you have that they do not — signed, so a positive number is an
 * edge to you.
 *
 * This is `largestSwing` widened from "the one biggest" to "all of them", and it
 * is the shape the season ledger accumulates.
 */
export function edges(mine: ManagerSquad, theirs: ManagerSquad): Map<number, number> {
  const out = new Map<number, number>()
  for (const id of new Set([...mine.weights.keys(), ...theirs.weights.keys()])) {
    const edge = (mine.weights.get(id) ?? 0) - (theirs.weights.get(id) ?? 0)
    if (edge !== 0) out.set(id, edge)
  }
  return out
}
