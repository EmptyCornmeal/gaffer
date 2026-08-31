// Deterministic, team-specific briefing that ALWAYS matches the squad you're
// viewing (the client-side Planner team the pipeline can't see). Same two-section
// shape as the AI verdict: what's strong + what I'd change. Suggested changes
// respect your ACTUAL budget (bank) and the 3-per-club limit.
//
// A15 sibling. That last sentence used to be false. The bank was DERIVED as
// `100 - squadCost`, which is the right rule only for a squad being assembled
// from scratch. For a squad you already own it is meaningless: team value rises
// above 100 as your players appreciate, so the subtraction goes NEGATIVE on a
// perfectly healthy squad. Measured on a real imported team it produced
// -0.2, which then filtered every candidate through `p.price - s.price <= bank`
// and left the briefing able to propose only DOWNGRADES -- while printing
// "£-0.2m in the bank" to the reader. The bank now comes from the holding FPL
// published, exactly as the planner's own money meter does.
import type { Player } from './types'
import { type Plan, holdingFunds } from './squad'

const BUDGET = 100.0
const CLUB_LIMIT = 3
const DEFCON_THRESHOLD: Record<string, number> = { DEF: 10, MID: 12 }

interface Swap {
  out: Player
  in: Player
  gain: number
  injured: boolean
}

/** Best affordable, club-legal single-transfer upgrades, chosen sequentially so
 *  together they stay within the bank. */
function suggestChanges(
  squad: Player[], starters: Player[], pool: Player[], plan?: Plan,
): { swaps: Swap[]; bank: number; bankIsCeiling: boolean } {
  // Held squad -> FPL's own bank, adjusted for anything already swapped in the
  // planner at list prices. Built squad (no holding) -> the 100m rule, which is
  // correct there. Never `100 - cost` on a squad somebody owns.
  const byId = new Map(pool.map((p) => [p.id, p]))
  const funds = plan?.holding
    ? holdingFunds(squad.map((p) => p.id), plan.holding, byId)
    : null
  const squadCost = squad.reduce((s, p) => s + p.price, 0)
  let bank = funds
    ? funds.bank
    : Math.round((BUDGET - squadCost) * 10) / 10
  // A selling price is never above the list price, so a funds figure derived
  // from list prices is an upper bound whenever anything has been swapped.
  const bankIsCeiling = !!funds && funds.soldCount > 0
  const startBank = bank

  const owned = new Set(squad.map((p) => p.id))
  const clubCount = new Map<number, number>()
  for (const p of squad) clubCount.set(p.team_id, (clubCount.get(p.team_id) ?? 0) + 1)

  const swaps: Swap[] = []
  const usedOut = new Set<number>()

  for (let k = 0; k < 2; k++) {
    let best: Swap | null = null
    for (const s of starters) {
      if (usedOut.has(s.id)) continue
      const injured = !!(s.status && s.status !== 'a')
      const alts = pool
        .filter(
          (p) =>
            p.pos === s.pos &&
            !owned.has(p.id) &&
            p.p_start >= 0.6 &&
            p.price - s.price <= bank + 1e-9 && // affordable within the real bank
            p.next_gw_xp > s.next_gw_xp + 0.4 &&
            (clubCount.get(p.team_id) ?? 0) - (p.team_id === s.team_id ? 1 : 0) < CLUB_LIMIT,
        )
        .sort((a, b) => b.next_gw_xp - a.next_gw_xp)
      if (!alts[0]) continue
      const gain = alts[0].next_gw_xp - s.next_gw_xp
      const cand: Swap = { out: s, in: alts[0], gain, injured }
      if (!best || (injured && !best.injured) || (injured === best.injured && gain > best.gain)) best = cand
    }
    if (!best) break
    swaps.push(best)
    usedOut.add(best.out.id)
    owned.delete(best.out.id)
    owned.add(best.in.id)
    clubCount.set(best.out.team_id, (clubCount.get(best.out.team_id) ?? 1) - 1)
    clubCount.set(best.in.team_id, (clubCount.get(best.in.team_id) ?? 0) + 1)
    bank = Math.round((bank - (best.in.price - best.out.price)) * 10) / 10
  }
  return { swaps, bank: startBank, bankIsCeiling }
}

export function generateTeamBrief(
  squad: Player[],
  starterIds: number[],
  captainId: number,
  pool: Player[],
  plan?: Plan,
): string {
  const starters = squad.filter((p) => starterIds.includes(p.id))
  const captain = squad.find((p) => p.id === captainId)
  const suggestedCap = [...starters].sort((a, b) => b.next_gw_xp - a.next_gw_xp)[0]
  const top = [...starters].sort((a, b) => b.next_gw_xp - a.next_gw_xp).slice(0, 3)
  const defcon = starters.filter((p) => p.defcon90 >= (DEFCON_THRESHOLD[p.pos] ?? 99))
  const flagged = squad.filter((p) => p.news || (p.status && p.status !== 'a'))
  const { swaps, bank, bankIsCeiling } = suggestChanges(squad, starters, pool, plan)
  const proj = starters.reduce((s, p) => s + p.next_gw_xp, 0) + (captain?.next_gw_xp ?? 0)

  const L: string[] = []
  const bankText = bankIsCeiling
    ? `up to £${bank.toFixed(1)}m in the bank`
    : `£${bank.toFixed(1)}m in the bank`
  L.push(`**Your XI projects ${proj.toFixed(1)} pts — ${captain ? `captain ${captain.name}` : 'set your captain'}, ${bankText}.**`, '')

  L.push("**✅ What's strong**")
  if (top.length) L.push(`- Best assets: ${top.map((p) => `**${p.name}** (${p.next_gw_xp.toFixed(1)})`).join(', ')}.`)
  if (defcon.length) L.push(`- DEFCON value: ${defcon.slice(0, 3).map((p) => p.name).join(', ')} chipping in +2s.`)
  if (captain && suggestedCap && captain.id !== suggestedCap.id)
    L.push(`- ⚠️ Captaincy: you've got ${captain.name}, but **${suggestedCap.name}** (${suggestedCap.next_gw_xp.toFixed(1)}) is the higher pick this week.`)
  if (flagged.length)
    L.push(`- ⚠️ Fitness: ${flagged.map((p) => `**${p.name}**${p.news ? ` (${p.news})` : ''}`).join('; ')}.`)
  if (!defcon.length && !flagged.length && (!captain || captain.id === suggestedCap?.id))
    L.push('- Solid spine, no fitness flags — a clean base to build on.')
  L.push('')

  L.push(`**🔄 What I'd change** (within your £${bank.toFixed(1)}m bank)`)
  if (swaps.length) {
    for (const c of swaps)
      L.push(`- **${c.out.name}** (${c.out.pos}, ${c.out.next_gw_xp.toFixed(1)}) → **${c.in.name}** (${c.in.team}, £${c.in.price.toFixed(1)}, ${c.in.next_gw_xp.toFixed(1)}) — +${c.gain.toFixed(1)} xP.`)
  } else {
    L.push('- No upgrade you can afford right now — hold and roll your transfer.')
  }
  L.push('')
  L.push(`**Bottom line: captain ${suggestedCap?.name ?? 'your best pick'}${swaps.length ? ` and look at ${swaps[0].in.name}` : ' and roll it'}.**`)
  return L.join('\n')
}
