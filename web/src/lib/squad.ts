// Pure squad-building logic for the manual planner (pre-season, no login needed).
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
}

export function emptyPlan(): Plan {
  return { name: '', ids: [], starters: [], captainId: -1, viceId: -1 }
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

export function addBlocker(squad: Player[], p: Player): string | null {
  if (squad.some((x) => x.id === p.id)) return 'already in squad'
  const t = totals(squad)
  if (t.count >= 15) return 'squad full (15)'
  if (t.byPos[p.pos] >= QUOTA[p.pos]) return `max ${QUOTA[p.pos]} ${p.pos}`
  if ((t.byClub[p.team_id] || 0) >= CLUB_LIMIT) return `max ${CLUB_LIMIT} per club`
  if (t.cost + p.price > BUDGET + 1e-9) return 'over budget'
  return null
}

export function squadValidity(squad: Player[]): string[] {
  const t = totals(squad)
  const errs: string[] = []
  for (const pos of ['GKP', 'DEF', 'MID', 'FWD'] as Pos[]) {
    if (t.byPos[pos] < QUOTA[pos]) errs.push(`need ${QUOTA[pos] - t.byPos[pos]} more ${pos}`)
  }
  if (t.cost > BUDGET) errs.push(`£${(t.cost - BUDGET).toFixed(1)}m over budget`)
  return errs
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

/** Build a plan from an FPL picks response (your real squad, XI, captain, vice). */
export function planFromPicks(picks: RawPick[], name = 'My team'): Plan {
  return {
    name,
    ids: picks.map((p) => p.element),
    starters: picks.filter((p) => p.multiplier > 0).map((p) => p.element),
    captainId: picks.find((p) => p.is_captain)?.element ?? -1,
    viceId: picks.find((p) => p.is_vice_captain)?.element ?? -1,
  }
}

/** Projected points for a plan (captain doubled). */
export function planPoints(squad: Player[], plan: Plan): number {
  const starters = squad.filter((p) => plan.starters.includes(p.id))
  const base = starters.reduce((s, p) => s + p.next_gw_xp, 0)
  const cap = squad.find((p) => p.id === plan.captainId)
  return Math.round((base + (cap?.next_gw_xp ?? 0)) * 10) / 10
}
