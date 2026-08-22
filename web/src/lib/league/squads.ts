// Rival squads for the League page — who owned what, and how many copies of his
// points each manager actually banked.
//
// The weight comes from FPL's own `multiplier`, never from a rule this file
// re-derives. Verified against real payloads rather than assumed:
//
//   bench 0 · XI 1 · captain 2 · Triple Captain 3 · **Bench Boost 1 on all
//   fifteen**
//
// That last one is the whole reason this reads `multiplier` instead of counting
// squad positions. `live/scoring.ts` has to model the rulebook because it scores
// a gameweek that is still being played, before FPL has ruled on anything; here
// the gameweek is over and FPL has already published its answer. Copying the
// rulebook a second time to reach a number FPL is handing us is exactly the
// duplication that made the solver's two objectives drift apart.

import { displayName } from '../fpl'

/** How many copies of a player's points land in one manager's total. */
export type Weight = number

export interface ManagerSquad {
  entry: number
  gw: number
  /** element id → weight. Only players who are actually his; absent = not owned. */
  weights: Map<number, Weight>
  /** Every element he held, bench included — ownership, as opposed to scoring. */
  held: Set<number>
  captain: number | null
  vice: number | null
  /** FPL's chip name (`bboost`, `3xc`, `freehit`, `wildcard`) or null. */
  chip: string | null
  /** Points paid for transfers this gameweek. */
  hits: number
  /**
   * FPL's own GROSS score for the gameweek — `entry_history.points`, before the
   * transfer cost is taken off (established in `live/source.ts`). Null when the
   * payload did not carry one, which is a different state from zero.
   */
  officialPoints: number | null
}

export interface PicksPayload {
  picks?: {
    element?: unknown
    multiplier?: unknown
    is_captain?: unknown
    is_vice_captain?: unknown
  }[]
  automatic_subs?: { element_in?: unknown; element_out?: unknown }[] | null
  active_chip?: string | null
  entry_history?: {
    points?: unknown
    event_transfers_cost?: unknown
  } | null
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/**
 * One manager's gameweek, from the picks payload FPL publishes for it.
 *
 * Autosubs are applied here rather than trusted to `multiplier`: FPL keeps the
 * submitted multipliers on the picks and publishes what it changed separately,
 * in `automatic_subs`. A manager whose striker blanked therefore reads as though
 * that striker still counted unless this is done — and it is done in the only
 * direction FPL ever moves it, bench in at 1, starter out at 0.
 *
 * Returns null for a payload with no picks at all: before a deadline FPL has
 * nothing to publish, and an empty squad is not a squad of zeroes.
 */
export function squadFromPicks(
  entry: number, gw: number, payload: PicksPayload | null | undefined,
): ManagerSquad | null {
  const picks = payload?.picks
  if (!picks?.length) return null

  const weights = new Map<number, Weight>()
  const held = new Set<number>()
  let captain: number | null = null
  let vice: number | null = null
  for (const p of picks) {
    const id = num(p?.element)
    if (id == null) continue
    held.add(id)
    weights.set(id, num(p?.multiplier) ?? 0)
    if (p?.is_captain === true) captain = id
    if (p?.is_vice_captain === true) vice = id
  }

  for (const sub of payload?.automatic_subs ?? []) {
    const out = num(sub?.element_out)
    const inn = num(sub?.element_in)
    // A substitution only ever moves a starter out for a bench player. Reading
    // the captain's multiplier across would be wrong in both directions: FPL
    // never subs a captain in, and a captain who blanks loses the armband to
    // the vice rather than handing it to whoever replaced him.
    if (out != null && weights.has(out)) weights.set(out, 0)
    if (inn != null && weights.has(inn)) weights.set(inn, 1)
  }

  return {
    entry,
    gw,
    weights,
    held,
    captain,
    vice,
    chip: typeof payload?.active_chip === 'string' ? payload.active_chip : null,
    hits: num(payload?.entry_history?.event_transfers_cost) ?? 0,
    officialPoints: num(payload?.entry_history?.points),
  }
}

/** What this squad scored, under a given set of player points. */
export function grossPoints(sq: ManagerSquad, points: Map<number, number>): number {
  let total = 0
  for (const [id, w] of sq.weights) {
    if (w === 0) continue
    total += (points.get(id) ?? 0) * w
  }
  return total
}

/**
 * Does our arithmetic reproduce FPL's published score for this gameweek, exactly?
 *
 * This is the gate on everything downstream. Attribution that does not add up to
 * the number on the FPL website is not a smaller truth, it is a different and
 * wrong one — and it fails silently, which is worse, because a ledger of
 * plausible per-player deltas looks identical whether or not it is right.
 *
 * It is also the only honest way to use a feed that lags: mid-gameweek
 * `entry_history.points` trails the live endpoint by whatever bonus has not been
 * confirmed yet, so a week in progress simply will not reconcile and is dropped
 * rather than half-counted.
 */
export function reconciles(sq: ManagerSquad, points: Map<number, number>): boolean {
  return sq.officialPoints != null && grossPoints(sq, points) === sq.officialPoints
}

// ---------------------------------------------------------------------------
// Fetching
// ---------------------------------------------------------------------------

/** How many managers to read. Beyond this a league view is a rate-limit problem
 *  rather than a scoreboard — the same ceiling `live/source.ts` applies. */
export const MAX_MANAGERS = 12

const BUDGET_MS = 20_000
const CONCURRENCY = 4

export interface GatherResult {
  squads: ManagerSquad[]
  /** How many managers we set out to read. */
  wanted: number
  /** How many of them could not be read at all. Named on screen, never hidden. */
  unread: number
}

/**
 * Read many managers' squads for one gameweek, concurrently and under one
 * deadline.
 *
 * Bounded as a phase rather than per request, for the reason spelled out in
 * `live/source.ts`: twelve twelve-second deadlines in series is two and a half
 * minutes of spinner, and a page doing nothing is the failure. Order is
 * preserved so the caller's league ordering survives a partial read.
 */
export async function gatherSquads(
  entries: number[],
  gw: number,
  picksFor: (entry: number, gw: number) => Promise<PicksPayload>,
  opts: { budgetMs?: number; concurrency?: number } = {},
): Promise<GatherResult> {
  const targets = entries.slice(0, MAX_MANAGERS)
  const got = new Array<ManagerSquad | null>(targets.length).fill(null)
  const deadline = Date.now() + (opts.budgetMs ?? BUDGET_MS)
  let cursor = 0
  // Counts answers, not squads. A manager who genuinely has no picks for this
  // gameweek has answered the question.
  let read = 0

  const worker = async (): Promise<void> => {
    for (;;) {
      const i = cursor++
      if (i >= targets.length) return
      if (Date.now() >= deadline) return
      try {
        const payload = await picksFor(targets[i], gw)
        read += 1
        got[i] = squadFromPicks(targets[i], gw, payload)
      } catch {
        // One unreadable manager must not cost the whole grid.
      }
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(opts.concurrency ?? CONCURRENCY, targets.length) },
               worker))
  return {
    squads: got.filter((s): s is ManagerSquad => s != null),
    wanted: targets.length,
    unread: targets.length - read,
  }
}

/** A manager's display name, as the standings gave it. */
export function managerLabel(
  row: { player_name?: string; entry_name?: string; entry: number },
): string {
  return displayName(row.player_name) || displayName(row.entry_name) || String(row.entry)
}
