// Deterministic, team-specific briefing that ALWAYS matches the squad you're
// viewing (the client-side Planner team the pipeline can't see). Same two-section
// shape as the AI verdict: what's strong + what I'd change.
import type { Player } from './types'

const DEFCON_THRESHOLD: Record<string, number> = { DEF: 10, MID: 12 }

interface Swap {
  out: Player
  in: Player
  gain: number
}

function suggestChanges(starters: Player[], pool: Player[], ownedIds: Set<number>): Swap[] {
  const swaps: Swap[] = []
  for (const s of starters) {
    const alts = pool
      .filter(
        (p) =>
          p.pos === s.pos &&
          !ownedIds.has(p.id) &&
          p.p_start >= 0.6 &&
          p.price <= s.price + 1.5 && // assume a little bank flexibility
          p.next_gw_xp > s.next_gw_xp + 0.4,
      )
      .sort((a, b) => b.next_gw_xp - a.next_gw_xp)
    if (alts[0]) swaps.push({ out: s, in: alts[0], gain: alts[0].next_gw_xp - s.next_gw_xp })
  }
  // prioritise upgrades for injured players, then biggest point gain
  swaps.sort((a, b) => {
    const ai = a.out.status && a.out.status !== 'a' ? 1 : 0
    const bi = b.out.status && b.out.status !== 'a' ? 1 : 0
    return bi - ai || b.gain - a.gain
  })
  // one suggestion per outgoing player, top 2
  const seen = new Set<number>()
  return swaps.filter((s) => !seen.has(s.out.id) && seen.add(s.out.id)).slice(0, 2)
}

export function generateTeamBrief(
  squad: Player[],
  starterIds: number[],
  captainId: number,
  pool: Player[],
): string {
  const ownedIds = new Set(squad.map((p) => p.id))
  const starters = squad.filter((p) => starterIds.includes(p.id))
  const captain = squad.find((p) => p.id === captainId)
  const suggestedCap = [...starters].sort((a, b) => b.next_gw_xp - a.next_gw_xp)[0]
  const top = [...starters].sort((a, b) => b.next_gw_xp - a.next_gw_xp).slice(0, 3)
  const defcon = starters.filter((p) => p.defcon90 >= (DEFCON_THRESHOLD[p.pos] ?? 99))
  const flagged = squad.filter((p) => p.news || (p.status && p.status !== 'a'))
  const changes = suggestChanges(starters, pool, ownedIds)
  const proj = starters.reduce((s, p) => s + p.next_gw_xp, 0) + (captain?.next_gw_xp ?? 0)

  const L: string[] = []
  L.push(`**Your XI projects ${proj.toFixed(1)} pts — ${captain ? `captain ${captain.name}` : 'set your captain'} and it's a tidy base.**`, '')

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

  L.push("**🔄 What I'd change**")
  if (changes.length) {
    for (const c of changes)
      L.push(`- **${c.out.name}** (${c.out.pos}, ${c.out.next_gw_xp.toFixed(1)}) → **${c.in.name}** (${c.in.team}, £${c.in.price.toFixed(1)}, ${c.in.next_gw_xp.toFixed(1)}) — +${c.gain.toFixed(1)} xP.`)
  } else {
    L.push("- No clear upgrade within budget — hold and roll your transfer.")
  }
  L.push('')
  L.push(`**Bottom line: captain ${suggestedCap?.name ?? 'your best pick'}${changes.length ? ` and look at ${changes[0].in.name}` : ' and roll it'}.**`)
  return L.join('\n')
}
