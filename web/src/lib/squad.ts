// Pure squad logic for the planner. Two jobs, not one: building fifteen from
// scratch, and evolving fifteen you already own. Those are different games with
// different money rules — see the budget section below.
import type { Player, Pos } from './types'

export const QUOTA: Record<Pos, number> = { GKP: 2, DEF: 5, MID: 5, FWD: 3 }
export const BUDGET = 100.0
export const CLUB_LIMIT = 3
// Exported so the live autosub rules read the same formation bounds the lineup
// validator does, rather than keeping a second copy that is free to drift.
export const FORMATION_MIN: Record<Pos, number> = { GKP: 1, DEF: 3, MID: 2, FWD: 1 }
export const FORMATION_MAX: Record<Pos, number> = { GKP: 1, DEF: 5, MID: 5, FWD: 3 }

const CUR_KEY = 'gaffer.plan'
const PLANS_KEY = 'gaffer.plans'

export interface Plan {
  name: string
  ids: number[] // the 15
  starters: number[] // the 11 (subset of ids)
  captainId: number
  viceId: number
  /**
   * Where the fifteen came from, which decides which money rule applies.
   * Optional because plans saved before provenance existed have none, and the
   * safe reading of an unknown squad is the from-scratch cap it was built under.
   */
  origin?: PlanOrigin
  /** Present only on an imported plan: FPL's own money for that squad. */
  holding?: Holding
}

/** The FPL money attached to a squad the manager actually owns. */
export interface Holding {
  /** The gameweek whose picks were imported — the last state FPL publishes. */
  gw: number
  /** In the bank, £m. Exact; FPL publishes it. */
  bank: number
  /** FPL's own team value, £m. Includes the bank. */
  teamValue: number
  /** The fifteen as imported, so an edit can be diffed against them. */
  ids: number[]
}

export function emptyPlan(): Plan {
  return { name: '', ids: [], starters: [], captainId: -1, viceId: -1, origin: 'built' }
}

/**
 * The same plan with its FPL money detached.
 *
 * Anything that REPLACES all fifteen — loading a model optimal, clearing the
 * squad — leaves a squad the manager does not own, so the holding rules must
 * stop applying to it. Dropping the holding is what puts the £100m cap back.
 */
export function asBuilt(plan: Plan): Plan {
  const { holding: _detached, ...rest } = plan
  return { ...rest, origin: 'built' }
}

export function loadCurrent(): Plan {
  try {
    const raw = localStorage.getItem(CUR_KEY)
    if (raw) return { ...emptyPlan(), ...JSON.parse(raw) }
    // migrate old squad-only storage
    const old = localStorage.getItem('gaffer.squad')
    if (old) return { ...emptyPlan(), ids: JSON.parse(old) }
  } catch {
    /* ignore */
  }
  return emptyPlan()
}
export function saveCurrent(plan: Plan) {
  localStorage.setItem(CUR_KEY, JSON.stringify(plan))
}

export function listPlans(): Plan[] {
  try {
    return JSON.parse(localStorage.getItem(PLANS_KEY) || '[]')
  } catch {
    return []
  }
}
export function savePlan(plan: Plan) {
  const plans = listPlans().filter((p) => p.name !== plan.name)
  plans.push(plan)
  localStorage.setItem(PLANS_KEY, JSON.stringify(plans))
}
export function deletePlan(name: string) {
  localStorage.setItem(PLANS_KEY, JSON.stringify(listPlans().filter((p) => p.name !== name)))
}

export interface Totals {
  cost: number
  byPos: Record<Pos, number>
  byClub: Record<number, number>
  count: number
}

export function totals(squad: Player[]): Totals {
  const byPos: Record<Pos, number> = { GKP: 0, DEF: 0, MID: 0, FWD: 0 }
  const byClub: Record<number, number> = {}
  let cost = 0
  for (const p of squad) {
    byPos[p.pos]++
    byClub[p.team_id] = (byClub[p.team_id] || 0) + 1
    cost += p.price
  }
  return { cost: Math.round(cost * 10) / 10, byPos, byClub, count: squad.length }
}

// ---------------------------------------------------------------------------
// A15 — two squads, two money rules.
//
// FPL applies the £100.0m cap exactly once: to a squad you assemble before you
// own anything. After that both the rule and the number change. You hold fifteen
// players, each with a SELLING price FPL fixed at purchase plus half of any
// rise, and the only constraint on a transfer is that the bank does not go
// negative. Squad value above £100m is not an overspend. It is the growth every
// manager is playing for.
//
// This file summed CURRENT prices against the flat cap whatever the fifteen
// were, so a squad imported with "Import my team" and worth £100.2m at today's
// prices was reported as "£0.2m over budget" — while FPL itself put that same
// manager at £100.3m value, £0.0m bank, and entirely legal.
//
// What the browser can see, and what it cannot:
//
//   EXACT — `entry_history` on the public picks endpoint carries `bank` and
//   `value`, and `value` INCLUDES the bank. (Checked against seven entries of
//   mini-league 271619 on 2026-08-31: `value - bank` is the squad's selling
//   value in every one.)
//
//   NOT AVAILABLE — per-player purchase or selling prices. Those live behind
//   FPL's authenticated `my-team` endpoint, which this app has no login for.
//   The pipeline reconstructs them server-side from transfer history
//   (`config.fpl_selling_price`) and publishes only a confidence, never the
//   numbers themselves.
//
// So an imported plan edited away from its fifteen cannot be costed exactly
// here. It can be BOUNDED exactly: FPL's rule guarantees a selling price never
// exceeds the current price, so `bank + Σ current(out) − Σ current(in)` is the
// most money any edit can leave. A negative bound is a certain shortfall and is
// an error; a positive one is a ceiling and says so on screen. Nothing below
// invents a selling price.
// ---------------------------------------------------------------------------

export type PlanOrigin = 'built' | 'imported'
export type BudgetBasis = 'build' | 'holding'

function round1(x: number): number {
  return Math.round(x * 10) / 10
}

/** £1.5m and −£1.5m, rather than `£-1.5m`. */
function money(x: number): string {
  return `${x < 0 ? '−' : ''}£${Math.abs(x).toFixed(1)}m`
}

export interface HoldingFunds {
  /**
   * Bank left after the edit, at the most favourable selling prices FPL's rule
   * permits. A ceiling: the real figure can only be lower.
   */
  bank: number
  /**
   * False when a player on either side of the edit is missing from this build's
   * player list, so a price is unknown and the number above bounds nothing.
   */
  resolved: boolean
  soldCount: number
  boughtCount: number
}

/** How `ids` differs from a holding, priced at today's list prices. */
export function holdingFunds(
  ids: number[], h: Holding, byId: Map<number, Player>,
): HoldingFunds {
  const held = new Set(h.ids)
  const now = new Set(ids)
  let resolved = true
  let proceeds = 0
  let soldCount = 0
  for (const id of h.ids) {
    if (now.has(id)) continue
    soldCount++
    const p = byId.get(id)
    if (p) proceeds += p.price
    else resolved = false
  }
  let outlay = 0
  let boughtCount = 0
  for (const id of ids) {
    if (held.has(id)) continue
    boughtCount++
    const p = byId.get(id)
    if (p) outlay += p.price
    else resolved = false
  }
  return { bank: round1(h.bank + proceeds - outlay), resolved, soldCount, boughtCount }
}

export interface BudgetView {
  basis: BudgetBasis
  /** Current-price sum of the fifteen on screen, £m. */
  cost: number
  /** Certain violations only. A bound that might be fine is never an error. */
  errors: string[]
  /** 0..1, for the meter. */
  fill: number
  /** True only when a rule is actually broken. */
  over: boolean
  /** The caption beside the meter. */
  label: string
  /** The limitation that belongs next to that number, or null. */
  note: string | null
  /** Squad value above the £100m start, £m. Null when there is no holding. */
  growth: number | null
  /** Best-case bank after the edits, £m. Null when it cannot be bounded. */
  bankAtBest: number | null
}

/**
 * The money statement for the squad on screen, under the rule that actually
 * governs it. `byId` only matters for an edited holding, where the players
 * being sold have already left `squad`.
 */
export function budgetView(
  squad: Player[], plan: Plan, byId: Map<number, Player> = new Map(),
): BudgetView {
  const cost = totals(squad).cost
  const h = plan.holding

  if (!h) {
    const over = cost > BUDGET + 1e-9
    return {
      basis: 'build',
      cost,
      errors: over ? [`£${round1(cost - BUDGET).toFixed(1)}m over budget`] : [],
      fill: Math.min(1, cost / BUDGET),
      over,
      label: `£${cost.toFixed(1)} / ${BUDGET}m`,
      note: null,
      growth: null,
      bankAtBest: null,
    }
  }

  const growth = round1(h.teamValue - BUDGET)
  const funds = holdingFunds(plan.ids, h, byId)
  const fill = Math.min(1, h.teamValue / BUDGET)

  if (funds.soldCount === 0 && funds.boughtCount === 0) {
    return {
      basis: 'holding',
      cost,
      errors: [],
      fill,
      over: false,
      label: `${money(h.teamValue)} value · ${money(h.bank)} bank`,
      note:
        (growth > 0 ? `${money(growth)} of that is squad growth since the £100m start. ` : '')
        + 'A squad you already own has no £100m cap — the only money rule is that '
        + `the bank stays at or above zero. Read at the GW${h.gw} deadline, which is `
        + 'the last squad state FPL publishes.',
      growth,
      bankAtBest: h.bank,
    }
  }

  const short = funds.resolved && funds.bank < -1e-9
  return {
    basis: 'holding',
    cost,
    errors: short ? [`${money(funds.bank)} short, even at full selling price`] : [],
    fill,
    over: short,
    label: `${money(h.bank)} bank → ${money(funds.bank)} at best`,
    note: funds.resolved
      ? 'FPL sells a risen player for what you paid plus half the rise, and only '
        + 'tells a logged-in manager what that is. Gaffer has current prices here, '
        + 'so this is a ceiling — what you actually get can only be less.'
      : 'A player in this swap is missing from this build, so its price is unknown '
        + 'and the money here bounds nothing.',
    growth,
    bankAtBest: funds.resolved ? funds.bank : null,
  }
}

export function addBlocker(
  squad: Player[], p: Player, plan?: Plan, byId?: Map<number, Player>,
): string | null {
  if (squad.some((x) => x.id === p.id)) return 'already in squad'
  const t = totals(squad)
  if (t.count >= 15) return 'squad full (15)'
  if (t.byPos[p.pos] >= QUOTA[p.pos]) return `max ${QUOTA[p.pos]} ${p.pos}`
  if ((t.byClub[p.team_id] || 0) >= CLUB_LIMIT) return `max ${CLUB_LIMIT} per club`
  // On a squad you own, affordability is the bank against selling prices — not
  // a £100m cap on current prices. Only a CERTAIN shortfall blocks the button;
  // an unbounded one does not, because refusing on a number we cannot compute
  // is the same mistake in the other direction.
  if (plan?.holding && byId) {
    const funds = holdingFunds([...plan.ids, p.id], plan.holding, byId)
    return funds.resolved && funds.bank < -1e-9
      ? `${money(funds.bank)} short of the bank`
      : null
  }
  if (t.cost + p.price > BUDGET + 1e-9) return 'over budget'
  return null
}

export function squadValidity(
  squad: Player[], plan?: Plan, byId?: Map<number, Player>,
): string[] {
  const t = totals(squad)
  const errs: string[] = []
  for (const pos of ['GKP', 'DEF', 'MID', 'FWD'] as Pos[]) {
    if (t.byPos[pos] < QUOTA[pos]) errs.push(`need ${QUOTA[pos] - t.byPos[pos]} more ${pos}`)
  }
  errs.push(...budgetView(squad, plan ?? emptyPlan(), byId).errors)
  return errs
}

// ---------------------------------------------------------------------------
// Squad watch
//
// Two different worries were being counted under one word. FPL's own vocabulary
// for "availability flag" is `status !== 'a'` — injured, suspended, ineligible,
// with news attached. A `bad` xMins badge is not that: the player is available
// and the minutes model expects him to be rotated or subbed.
//
// The header said "7 availability flags in your squad" over a squad in which
// every one of the fifteen was FPL-available and all seven hits were rotation
// risks. The per-player lines underneath already said "rotation risk", so the
// count contradicted its own detail. Both signals are worth showing; they are
// counted and named separately.
// ---------------------------------------------------------------------------

export type WatchKind = 'unavailable' | 'rotation'

export interface WatchItem {
  player: Player
  kind: WatchKind
  reason: string
  starting: boolean
}

export interface SquadWatch {
  items: WatchItem[]
  unavailable: number
  rotation: number
  /** How many of the flagged players are in the XI. */
  starting: number
  /** One line naming each count for what it is, or '' when there is nothing. */
  headline: string
}

export function squadWatch(squad: Player[], starterIds: number[]): SquadWatch {
  const items: WatchItem[] = []
  for (const p of squad) {
    const unavailable = !!(p.status && p.status !== 'a')
    const rotation = p.xmins_badge?.kind === 'bad'
    if (!unavailable && !rotation) continue
    const hint = p.xmins_badge?.hint?.trim()
    items.push({
      player: p,
      kind: unavailable ? 'unavailable' : 'rotation',
      reason: unavailable
        ? (p.news?.trim() || 'flagged — check status')
        : hint ? `rotation risk — projected ${hint}` : 'rotation risk',
      starting: starterIds.includes(p.id),
    })
  }
  const unavailable = items.filter((i) => i.kind === 'unavailable').length
  const rotation = items.length - unavailable
  const starting = items.filter((i) => i.starting).length
  const parts: string[] = []
  if (unavailable) parts.push(`${unavailable} availability flag${unavailable === 1 ? '' : 's'}`)
  if (rotation) parts.push(`${rotation} rotation risk${rotation === 1 ? '' : 's'}`)
  const headline = parts.length
    ? `${parts.join(' · ')} in your squad${starting ? ` · ${starting} starting` : ''}`
    : ''
  return { items, unavailable, rotation, starting, headline }
}

/** Validate a chosen starting XI (formation + count). */
export function lineupErrors(squad: Player[], starterIds: number[]): string[] {
  const starters = squad.filter((p) => starterIds.includes(p.id))
  const errs: string[] = []
  if (starters.length !== 11) errs.push(`${starters.length}/11 starters`)
  const by: Record<Pos, number> = { GKP: 0, DEF: 0, MID: 0, FWD: 0 }
  for (const p of starters) by[p.pos]++
  if (by.GKP !== 1) errs.push('need exactly 1 GK starting')
  for (const pos of ['DEF', 'MID', 'FWD'] as Pos[]) {
    if (by[pos] < FORMATION_MIN[pos]) errs.push(`min ${FORMATION_MIN[pos]} ${pos}`)
    if (by[pos] > FORMATION_MAX[pos]) errs.push(`max ${FORMATION_MAX[pos]} ${pos}`)
  }
  return errs
}

export function formationOf(squad: Player[], starterIds: number[]): string {
  const by: Record<Pos, number> = { GKP: 0, DEF: 0, MID: 0, FWD: 0 }
  for (const p of squad) if (starterIds.includes(p.id)) by[p.pos]++
  return `${by.DEF}-${by.MID}-${by.FWD}`
}

// Captaincy optimises PURE expected points, and nothing else.
//
// This used to be `next_gw_xp * (1 + 8 * ownership)`. At that weight ownership
// was the dominant term — a player needed 1.27x the expected points of a
// 73.5%-owned rival merely to draw level — so in practice it captained whoever
// the crowd owned. It was added to stop the UI contradicting the verdict, and it
// only moved the contradiction: My Team captained the most-owned player while
// Home, `recommendation.json` and the verdict captained the highest-xP one, from
// the same data. It also quietly reinstated the global-ownership term the solver
// had deleted on measured evidence (it cost 2.11 xP on the armband).
//
// One rule, in the solver and here: the captain is the starter with the highest
// expected points. Ownership is context to show beside that choice, never an
// input to it.
export function captainScore(p: Player): number {
  return p.next_gw_xp
}

/** Auto-pick the best legal XI (by expected points) + EO-aware captain/vice. */
export function autoLineup(squad: Player[]): { starters: number[]; captainId: number; viceId: number } {
  const by: Record<Pos, Player[]> = { GKP: [], DEF: [], MID: [], FWD: [] }
  for (const p of squad) by[p.pos].push(p)
  for (const pos of Object.keys(by) as Pos[]) by[pos].sort((a, b) => b.next_gw_xp - a.next_gw_xp)

  let best: { ids: number[]; xp: number } = { ids: [], xp: -1 }
  for (let d = FORMATION_MIN.DEF; d <= FORMATION_MAX.DEF; d++) {
    for (let m = FORMATION_MIN.MID; m <= FORMATION_MAX.MID; m++) {
      const f = 10 - d - m
      if (f < FORMATION_MIN.FWD || f > FORMATION_MAX.FWD) continue
      if (by.GKP.length < 1 || by.DEF.length < d || by.MID.length < m || by.FWD.length < f) continue
      const xi = [...by.GKP.slice(0, 1), ...by.DEF.slice(0, d), ...by.MID.slice(0, m), ...by.FWD.slice(0, f)]
      const xp = xi.reduce((s, p) => s + p.next_gw_xp, 0)
      if (xp > best.xp) best = { ids: xi.map((p) => p.id), xp }
    }
  }
  const ranked = squad
    .filter((p) => best.ids.includes(p.id))
    .sort((a, b) => captainScore(b) - captainScore(a))
  return {
    starters: best.ids,
    captainId: ranked[0]?.id ?? -1,
    viceId: ranked[1]?.id ?? -1,
  }
}

interface RawPick {
  element: number
  multiplier: number
  is_captain: boolean
  is_vice_captain: boolean
}

/**
 * Build a plan from an FPL picks response (your real squad, XI, captain, vice).
 *
 * With `money`, the plan is marked as one the manager OWNS and carries FPL's
 * bank and team value with it — which is what stops the from-scratch £100m cap
 * being applied to a squad that has since grown past it. Without it the plan is
 * treated as built, because a squad whose money we cannot see is not one we can
 * make bank claims about.
 */
export function planFromPicks(
  picks: RawPick[], name = 'My team', money?: Omit<Holding, 'ids'>,
): Plan {
  const ids = picks.map((p) => p.element)
  return {
    name,
    ids,
    starters: picks.filter((p) => p.multiplier > 0).map((p) => p.element),
    captainId: picks.find((p) => p.is_captain)?.element ?? -1,
    viceId: picks.find((p) => p.is_vice_captain)?.element ?? -1,
    origin: money ? 'imported' : 'built',
    ...(money ? { holding: { ...money, ids } } : {}),
  }
}

/** Projected points for a plan (captain doubled). */
export function planPoints(squad: Player[], plan: Plan): number {
  const starters = squad.filter((p) => plan.starters.includes(p.id))
  const base = starters.reduce((s, p) => s + p.next_gw_xp, 0)
  const cap = squad.find((p) => p.id === plan.captainId)
  return Math.round((base + (cap?.next_gw_xp ?? 0)) * 10) / 10
}
