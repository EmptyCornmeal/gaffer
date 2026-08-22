// The season swing ledger: not "who is ahead", but WHAT put them there.
//
// `live/scoring.ts` answers this for one player in one gameweek — the biggest
// single thing deciding your week against your closest rival. This is the same
// arithmetic run over every player and every finished gameweek, and totalled:
//
//     delta(player) = Σ  points(player, gw) × (yourWeight − theirWeight)
//                    gw
//
// so a rival's lead stops being a number and becomes a list of names. It is the
// question a mini-league actually argues about.
//
// Two rules keep it honest, and both are load-bearing:
//
//   1. **Only finished gameweeks count.** FPL's `entry_history.points` lags the
//      live feed while matches are on — measured at 24 against 21 in GW1 — so a
//      week in progress cannot be attributed, only guessed at.
//   2. **Only reconciled gameweeks count.** A manager-gameweek enters the ledger
//      if and only if our arithmetic reproduces FPL's published score for it
//      exactly. Anything else is dropped and counted, never averaged in. A card
//      whose numbers do not add up to the source is the one failure mode this
//      page cannot afford, because it looks exactly like one that does.

import { grossPoints, reconciles, type ManagerSquad } from './squads'

/** Final points for every player in one gameweek. */
export interface GwPoints {
  gw: number
  points: Map<number, number>
}

export interface LedgerLine {
  playerId: number
  /** Points this player has won you against this rival. Negative = he cost you. */
  delta: number
  /** How many counted gameweeks he actually moved anything in. */
  weeks: number
  /** Net copies you had of him across those weeks — the shape of the edge. */
  edge: number
}

export interface RivalLedger {
  entry: number
  /** Your counted points minus theirs. Reproduces the sum of `lines`. */
  gap: number
  /** Sorted by absolute delta: what is deciding this rivalry, first. */
  lines: LedgerLine[]
  /** Gameweeks that entered the ledger. */
  counted: number[]
  /** Gameweeks dropped because one side would not reconcile. */
  dropped: number[]
}

/**
 * One rival's ledger.
 *
 * `mine` and `theirs` are keyed by gameweek; a gameweek needs both sides and a
 * points map before it can be counted at all.
 */
export function rivalLedger(
  entry: number,
  mine: Map<number, ManagerSquad>,
  theirs: Map<number, ManagerSquad>,
  points: Map<number, GwPoints>,
): RivalLedger {
  const lines = new Map<number, LedgerLine>()
  const counted: number[] = []
  const dropped: number[] = []
  let gap = 0

  for (const gw of [...points.keys()].sort((a, b) => a - b)) {
    const pts = points.get(gw)?.points
    const a = mine.get(gw)
    const b = theirs.get(gw)
    if (!pts || !a || !b) continue
    // Both sides, or neither. Attributing a week we can only half-account for
    // would move the gap by a number with nothing behind it.
    if (!reconciles(a, pts) || !reconciles(b, pts)) {
      dropped.push(gw)
      continue
    }
    counted.push(gw)
    // Hits are a real part of the gap and belong to no player, so they are added
    // to the total without ever appearing as a line. `officialPoints` is gross
    // of them (see `live/source.ts`), which is why they subtract here.
    gap += (grossPoints(a, pts) - a.hits) - (grossPoints(b, pts) - b.hits)

    for (const id of new Set([...a.weights.keys(), ...b.weights.keys()])) {
      const edge = (a.weights.get(id) ?? 0) - (b.weights.get(id) ?? 0)
      if (edge === 0) continue
      const delta = (pts.get(id) ?? 0) * edge
      const line = lines.get(id) ?? { playerId: id, delta: 0, weeks: 0, edge: 0 }
      line.edge += edge
      // A week in which he had an edge but scored nothing is still a week the
      // two squads differed; it is only left out of the count when it moved
      // nothing, which is what makes `weeks` readable as "weeks he mattered".
      if (delta !== 0) {
        line.delta += delta
        line.weeks += 1
      }
      lines.set(id, line)
    }
  }

  return {
    entry,
    gap,
    lines: [...lines.values()]
      .filter((l) => l.delta !== 0)
      .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta) || a.playerId - b.playerId),
    counted,
    dropped,
  }
}

/**
 * How much of the gap the top `n` lines explain.
 *
 * Deliberately measured against the sum of the lines rather than against `gap`:
 * the two differ by exactly the hits, which belong to no player, and a "top
 * three explain 140% of it" reads as a bug even when the arithmetic is right.
 */
export function concentration(led: RivalLedger, n: number): number {
  const all = led.lines.reduce((s, l) => s + Math.abs(l.delta), 0)
  if (!all) return 0
  const top = led.lines.slice(0, n).reduce((s, l) => s + Math.abs(l.delta), 0)
  return (top / all) * 100
}

/** Total points lost to transfer hits, over the counted weeks. */
export function hitsPaid(
  squads: Map<number, ManagerSquad>, counted: number[],
): number {
  return counted.reduce((s, gw) => s + (squads.get(gw)?.hits ?? 0), 0)
}
