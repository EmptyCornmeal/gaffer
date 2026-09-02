import { describe, expect, it } from 'vitest'
import {
  captainScore, autoLineup, budgetView, squadValidity, addBlocker, holdingFunds,
  planFromPicks, asBuilt, squadWatch, emptyPlan, BUDGET, type Holding, type Plan,
} from './squad'
import type { Player, Pos } from './types'

// The client used to score the armband as `xP * (1 + 8 * ownership)`, so it
// captained whoever the crowd owned while Home, recommendation.json and the
// verdict captained the highest-xP player from the same data.
function p(id: number, pos: Pos, xp: number, owned: number, price = 5): Player {
  return {
    id, code: null, name: `P${id}`, full_name: `P${id}`, team: 'ARS', team_id: 1,
    team_code: 1, pos, price, owned_by: owned, net_transfers: 0,
    cost_change_event: 0,
    price_pred: { dir: 'stable', momentum: 0, percent: 0, due: false },
    status: 'a', news: '', set_pieces: '', form: 0, ict: 0, last_season: null,
    dist: null, defcon: null, xgi90: 0, defcon90: 0, next_gw_xp: xp,
    horizon_xp: xp * 6, xp_window: xp * 6, gw_xp: [], p_start: 1, confidence: 1,
    xmins_badge: { label: 'NAILED', kind: 'good', hint: '' }, rationale: '',
    tags: [], fixtures: [],
    breakdown: { appearance: 0, goals: 0, assists: 0, clean_sheet: 0, defcon: 0, bonus: 0 },
  } as Player
}

describe('captaincy is pure expected points', () => {
  it('scores a player on xP alone', () => {
    expect(captainScore(p(1, 'MID', 5.02, 48.1))).toBe(5.02)
    expect(captainScore(p(2, 'FWD', 4.47, 73.5))).toBe(4.47)
  })

  it('does not let ownership overturn an xP edge', () => {
    const differential = p(1, 'MID', 5.02, 1)
    const crowd = p(2, 'FWD', 4.47, 95)
    expect(captainScore(differential)).toBeGreaterThan(captainScore(crowd))
  })

  it('is unaffected by ownership entirely', () => {
    expect(captainScore(p(1, 'MID', 6, 0))).toBe(captainScore(p(2, 'MID', 6, 99)))
  })
})

describe('autoLineup', () => {
  const squad: Player[] = [
    p(1, 'GKP', 3.0, 5), p(2, 'GKP', 1.0, 1),
    p(3, 'DEF', 4.5, 10), p(4, 'DEF', 4.0, 10), p(5, 'DEF', 3.5, 10),
    p(6, 'DEF', 2.0, 10), p(7, 'DEF', 1.5, 10),
    p(8, 'MID', 5.0, 2), p(9, 'MID', 4.8, 90), p(10, 'MID', 3.0, 10),
    p(11, 'MID', 2.5, 10), p(12, 'MID', 2.0, 10),
    p(13, 'FWD', 4.9, 88), p(14, 'FWD', 3.2, 10), p(15, 'FWD', 1.0, 10),
  ]

  it('captains the highest-xP starter, not the most-owned one', () => {
    const { starters, captainId, viceId } = autoLineup(squad)
    expect(captainId).toBe(8) // 5.0 xP, 2% owned
    expect(starters).toContain(captainId)
    expect(viceId).not.toBe(captainId)
  })

  it('picks the vice as the next-highest xP starter', () => {
    const { viceId } = autoLineup(squad)
    expect(viceId).toBe(13) // 4.9 xP beats the 4.8 xP 90%-owned midfielder
  })
})


// ---------------------------------------------------------------------------
// A15 — the budget rule branches on where the fifteen came from
//
// Measured against the real thing on 2026-08-31: entry 1066421's GW2 picks sum
// to £100.2m at current prices, while FPL reports that same squad as
// `value: 1003, bank: 0`. The old rule called it "£0.2m over budget". It is not
// over anything — £100.3m of team value on £0.0m of bank is a legal squad that
// has grown £0.3m since the £100m start.
// ---------------------------------------------------------------------------

/** Fifteen legal players — right quota, three per club — summing to `total` £m. */
function fifteen(total = 100.2): Player[] {
  const squad: Player[] = []
  let id = 1
  const shape: [Pos, number][] = [['GKP', 2], ['DEF', 5], ['MID', 5], ['FWD', 3]]
  for (const [pos, n] of shape) {
    for (let i = 0; i < n; i++) {
      const pl = p(id, pos, 4, 10, 6)
      pl.team_id = Math.floor((id - 1) / 3) + 1
      squad.push(pl)
      id++
    }
  }
  // Nudge the last player so the fifteen sum to exactly `total`.
  const sum = squad.reduce((s, x) => s + x.price, 0)
  squad[14].price = Math.round((squad[14].price + (total - sum)) * 10) / 10
  return squad
}

const IDS = fifteen().map((x) => x.id)

function builtPlan(ids = IDS): Plan {
  return { ...emptyPlan(), name: 'Built', ids, starters: ids.slice(0, 11) }
}

function heldPlan(holding: Partial<Holding> = {}, ids = IDS): Plan {
  return {
    ...builtPlan(ids),
    name: 'My team',
    origin: 'imported',
    holding: { gw: 2, bank: 0, teamValue: 100.3, ids: IDS, ...holding },
  }
}

describe('budgetView — a squad built from scratch', () => {
  it('keeps the £100m cap and reports the overspend', () => {
    const b = budgetView(fifteen(100.2), builtPlan())
    expect(b.basis).toBe('build')
    expect(b.over).toBe(true)
    expect(b.errors).toEqual(['£0.2m over budget'])
    expect(b.label).toBe('£100.2 / 100m')
    expect(b.growth).toBeNull()
  })

  it('is happy inside the cap', () => {
    const b = budgetView(fifteen(99.5), builtPlan())
    expect(b.over).toBe(false)
    expect(b.errors).toEqual([])
  })

  it('is what a plan saved before provenance existed falls back to', () => {
    // No `origin`, no `holding` — the safe reading of an unknown squad.
    const legacy = { name: 'old', ids: IDS, starters: [], captainId: -1, viceId: -1 }
    expect(budgetView(fifteen(100.2), legacy).basis).toBe('build')
    expect(budgetView(fifteen(100.2), legacy).errors).toHaveLength(1)
  })
})

describe('budgetView — a squad you already own', () => {
  it('does not call a £100.2m imported squad over budget', () => {
    const b = budgetView(fifteen(100.2), heldPlan())
    expect(b.basis).toBe('holding')
    expect(b.over).toBe(false)
    expect(b.errors).toEqual([])
  })

  it('presents value above £100m as growth, and says the rule is the bank', () => {
    const b = budgetView(fifteen(100.2), heldPlan())
    expect(b.growth).toBe(0.3)
    expect(b.label).toBe('£100.3m value · £0.0m bank')
    expect(b.note).toContain('growth')
    expect(b.note).toContain('bank stays at or above zero')
    expect(b.note).toContain('GW2')
  })

  it('reports the bank FPL published, not £100m minus the squad', () => {
    const b = budgetView(fifteen(100.2), heldPlan({ bank: 1.5, teamValue: 101.8 }))
    expect(b.bankAtBest).toBe(1.5)
    expect(b.label).toBe('£101.8m value · £1.5m bank')
  })

  it('squadValidity raises no budget error for it', () => {
    const squad = fifteen(100.2)
    const byId = new Map(squad.map((x) => [x.id, x]))
    expect(squadValidity(squad, heldPlan(), byId)).toEqual([])
    // …while the same fifteen built from scratch still is over.
    expect(squadValidity(squad, builtPlan(), byId)).toEqual(['£0.2m over budget'])
  })
})

describe('budgetView — an imported squad you have edited', () => {
  const held = fifteen(100.2)
  const sold = held[13] // £6.0m
  const incoming = p(99, 'FWD', 9, 30, 9.0)
  incoming.team_id = 9
  const byId = new Map<number, Player>([...held.map((x) => [x.id, x] as const), [99, incoming]])

  function afterSwap(bank: number) {
    const ids = [...IDS.filter((i) => i !== sold.id), 99]
    return budgetView(
      [...held.filter((x) => x.id !== sold.id), incoming],
      heldPlan({ bank }, ids),
      byId,
    )
  }

  it('costs the swap against the bank plus what the outgoing player fetches', () => {
    expect(sold.price).toBe(6)
    const b = afterSwap(3.0)
    expect(b.bankAtBest).toBe(0) // 3.0 + 6.0 − 9.0
    expect(b.errors).toEqual([])
    expect(b.label).toBe('£3.0m bank → £0.0m at best')
  })

  it('errors only on a shortfall that is certain', () => {
    const b = afterSwap(0)
    expect(b.bankAtBest).toBe(-3) // 0 + 6.0 − 9.0
    expect(b.over).toBe(true)
    expect(b.errors).toEqual(['−£3.0m short, even at full selling price'])
    expect(b.label).toBe('£0.0m bank → −£3.0m at best')
  })

  it('says the figure is a ceiling, because selling prices are not public', () => {
    const b = afterSwap(3.0)
    expect(b.note).toContain('ceiling')
    expect(b.note).toContain('half the rise')
  })

  it('bounds nothing when a player in the swap has no price here', () => {
    const ids = [...IDS.filter((i) => i !== sold.id), 99]
    const b = budgetView(
      [...held.filter((x) => x.id !== sold.id), incoming],
      heldPlan({ bank: 0 }, ids),
      new Map(), // neither side resolvable
    )
    expect(b.bankAtBest).toBeNull()
    expect(b.errors).toEqual([])
    expect(b.note).toContain('bounds nothing')
  })
})

describe('holdingFunds', () => {
  const held = fifteen(100.2)
  const byId = new Map(held.map((x) => [x.id, x]))
  const h: Holding = { gw: 2, bank: 0.4, teamValue: 100.6, ids: IDS }

  it('is the bank untouched when nothing has moved', () => {
    expect(holdingFunds(IDS, h, byId)).toEqual({
      bank: 0.4, resolved: true, soldCount: 0, boughtCount: 0,
    })
  })

  it('counts both sides of an edit', () => {
    // The fifteenth is the £16.2m slot that makes the squad sum to £100.2m.
    const f = holdingFunds(IDS.slice(0, 14), h, byId)
    expect(f.soldCount).toBe(1)
    expect(f.boughtCount).toBe(0)
    expect(f.bank).toBe(16.6) // 0.4 + 16.2
  })
})

describe('addBlocker', () => {
  // Fourteen at £6.5m = £91.0m, so a £9.5m fifteenth breaks the £100m cap by
  // exactly £0.5m. The imported squad they came from also held a £6.0m
  // fifteenth, now sold.
  const fourteen = Array.from({ length: 14 }, (_, i) => {
    const pl = p(i + 1, 'DEF', 3, 10, 6.5)
    pl.team_id = Math.floor(i / 3) + 1
    return pl
  })
  const ids14 = fourteen.map((x) => x.id)
  const sold = p(15, 'FWD', 3, 10, 6.0)
  sold.team_id = 8
  const pricey = p(99, 'FWD', 9, 30, 9.5)
  pricey.team_id = 9
  const byId = new Map<number, Player>([
    ...fourteen.map((x) => [x.id, x] as const), [15, sold], [99, pricey],
  ])
  const holding = (bank: number): Plan => ({
    ...builtPlan(ids14),
    name: 'My team',
    origin: 'imported',
    holding: { gw: 2, bank, teamValue: 97.0, ids: [...ids14, 15] },
  })

  it('still blocks on the £100m cap for a built squad', () => {
    expect(addBlocker(fourteen, pricey, builtPlan(ids14), byId)).toBe('over budget')
  })

  it('blocks a holding only on money it can prove is missing', () => {
    // Sold a £6.0m forward, buying a £9.5m one, on a £0.0m bank.
    expect(addBlocker(fourteen, pricey, holding(0), byId)).toBe('−£3.5m short of the bank')
  })

  it('lets an affordable swap through on a holding the £100m cap would refuse', () => {
    expect(addBlocker(fourteen, pricey, holding(3.5), byId)).toBeNull()
  })

  it('still enforces the quota and club rules on a holding', () => {
    const plan = heldPlan({ bank: 99 })
    expect(addBlocker(fifteen(90), p(99, 'FWD', 9, 30, 4), plan, byId)).toBe('squad full (15)')
  })
})

describe('plan provenance', () => {
  const picks = [
    { element: 1, multiplier: 1, is_captain: true, is_vice_captain: false },
    { element: 2, multiplier: 0, is_captain: false, is_vice_captain: true },
  ]

  it('marks an imported plan as owned and carries FPL money onto it', () => {
    const plan = planFromPicks(picks, 'My team', { gw: 2, bank: 0, teamValue: 100.3 })
    expect(plan.origin).toBe('imported')
    expect(plan.holding).toEqual({ gw: 2, bank: 0, teamValue: 100.3, ids: [1, 2] })
  })

  it('treats picks with no money as built — no bank claims from nothing', () => {
    const plan = planFromPicks(picks)
    expect(plan.origin).toBe('built')
    expect(plan.holding).toBeUndefined()
  })

  it('detaches the holding when the fifteen are replaced wholesale', () => {
    const built = asBuilt(heldPlan())
    expect(built.holding).toBeUndefined()
    expect(built.origin).toBe('built')
    expect(budgetView(fifteen(100.2), built).errors).toEqual(['£0.2m over budget'])
  })

  it('BUDGET is still the from-scratch rule it always was', () => {
    expect(BUDGET).toBe(100)
  })
})

// ---------------------------------------------------------------------------
// The squad watch counts two different worries separately
// ---------------------------------------------------------------------------

describe('squadWatch', () => {
  function rotation(id: number, hint = "~50'"): Player {
    const pl = p(id, 'DEF', 3, 10)
    pl.xmins_badge = { label: 'CAMEO?', kind: 'bad', hint }
    return pl
  }
  function injured(id: number, news = 'Knee injury - 75% chance of playing'): Player {
    const pl = p(id, 'MID', 3, 10)
    pl.status = 'd'
    pl.news = news
    return pl
  }

  it('does not call a rotation risk an availability flag', () => {
    // The real GW3 squad: seven `bad` xMins badges, zero FPL status flags.
    const squad = [1, 2, 3, 4, 5, 6, 7].map((i) => rotation(i))
    const w = squadWatch(squad, [1, 2, 3, 4])
    expect(w.unavailable).toBe(0)
    expect(w.rotation).toBe(7)
    expect(w.headline).toBe('7 rotation risks in your squad · 4 starting')
  })

  it('names both when both are present', () => {
    const w = squadWatch([injured(1), rotation(2), rotation(3)], [1])
    expect(w.headline).toBe('1 availability flag · 2 rotation risks in your squad · 1 starting')
  })

  it('carries FPL news for a flagged player and the minutes hint for a risk', () => {
    const w = squadWatch([injured(1), rotation(2, "~39'")], [])
    expect(w.items[0].reason).toBe('Knee injury - 75% chance of playing')
    expect(w.items[0].kind).toBe('unavailable')
    expect(w.items[1].reason).toBe("rotation risk — projected ~39'")
    expect(w.items[1].kind).toBe('rotation')
  })

  it('says nothing about a clean squad', () => {
    const w = squadWatch([p(1, 'MID', 5, 10)], [1])
    expect(w.items).toEqual([])
    expect(w.headline).toBe('')
  })
})
