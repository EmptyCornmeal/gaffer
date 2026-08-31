import { describe, expect, it } from 'vitest'
import { generateTeamBrief } from './teamBrief'
import type { Plan } from './squad'
import type { Player } from './types'

// A15 sibling. `teamBrief` derived its bank as `100 - squadCost`. That is the
// right rule for a squad being assembled and meaningless for one already owned:
// team value rises above 100 as players appreciate, so the subtraction goes
// NEGATIVE on a healthy squad. Measured at -0.2 on a real imported team, which
// filtered every candidate through `p.price - s.price <= bank` and left the
// briefing able to suggest only downgrades, while printing "£-0.2m in the bank".

function player(id: number, price: number, xp: number, pos = 'MID'): Player {
  return {
    id, name: `P${id}`, pos, price, team_id: (id % 15) + 1,
    next_gw_xp: xp, defcon90: 0, status: 'a', news: '',
    // the candidate filter gates on p_start >= 0.6; without it every synthetic
    // player compares `undefined >= 0.6` and silently fails to be a candidate
    p_start: 0.9,
  } as unknown as Player
}

// Fifteen players costing 100.2 in total: an appreciated, perfectly legal squad.
const SQUAD: Player[] = [
  ...Array.from({ length: 14 }, (_, i) => player(i + 1, 6.8, 3.0)),
  player(15, 5.0, 3.0),
]
const STARTERS = SQUAD.slice(0, 11).map((p) => p.id)
// An upgrade that is affordable out of a real £1.0m bank but not out of a
// derived negative one.
const POOL: Player[] = [...SQUAD, player(99, 7.5, 9.9)]

const HELD: Plan = {
  name: 'mine', ids: SQUAD.map((p) => p.id), starters: STARTERS,
  captainId: SQUAD[0].id, viceId: SQUAD[1].id,
  origin: 'imported',
  holding: { gw: 2, bank: 1.0, teamValue: 101.2, ids: SQUAD.map((p) => p.id) },
}

describe('A15 sibling — the briefing uses the real bank', () => {
  it('does not derive a negative bank from an appreciated squad', () => {
    const cost = SQUAD.reduce((s, p) => s + p.price, 0)
    expect(cost).toBeGreaterThan(100) // the condition that broke it
    const brief = generateTeamBrief(SQUAD, STARTERS, SQUAD[0].id, POOL, HELD)
    expect(brief).not.toMatch(/£-/)
    expect(brief).toContain('£1.0m in the bank')
  })

  it('can suggest an upgrade the real bank affords', () => {
    const brief = generateTeamBrief(SQUAD, STARTERS, SQUAD[0].id, POOL, HELD)
    expect(brief).toContain('P99')
  })

  it('could not suggest that upgrade without the holding', () => {
    // No plan -> the from-scratch rule -> a negative derived bank -> downgrades
    // only. Kept as the explicit record of the old behaviour.
    const brief = generateTeamBrief(SQUAD, STARTERS, SQUAD[0].id, POOL)
    expect(brief).not.toContain('P99')
  })

  it('still applies the from-scratch cap to a squad with no holding', () => {
    const built: Plan = { ...HELD, origin: 'built', holding: undefined }
    const brief = generateTeamBrief(SQUAD, STARTERS, SQUAD[0].id, POOL, built)
    expect(brief).not.toContain('P99')
  })
})
