import { describe, expect, it } from 'vitest'
import { captainScore, autoLineup } from './squad'
import type { Player, Pos } from './types'

// The client used to score the armband as `xP * (1 + 8 * ownership)`, so it
// captained whoever the crowd owned while Home, recommendation.json and the
// verdict captained the highest-xP player from the same data.
function p(id: number, pos: Pos, xp: number, owned: number): Player {
  return {
    id, code: null, name: `P${id}`, full_name: `P${id}`, team: 'ARS', team_id: 1,
    team_code: 1, pos, price: 5, owned_by: owned, net_transfers: 0,
    cost_change_event: 0,
    price_pred: { dir: 'stable', momentum: 0, progress: 0, threshold: 0 },
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
