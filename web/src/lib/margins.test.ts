import { describe, expect, it } from 'vitest'
import {
  BANDS,
  FREE_THRESHOLD,
  SPINE_THRESHOLD,
  formatMargin,
  marginBand,
  marginHeadline,
  marginsUsable,
  rankedPicks,
} from './margins'
import type { Margin, RecPlayer, Recommendation } from './types'

function pick(id: number, name: string, margin?: Margin): RecPlayer {
  return {
    id, code: null, name, team: 'ARS', team_code: 1, pos: 'MID', price: 6,
    next_gw_xp: 5, confidence: 0.9, margin,
  }
}

// The real GW1 build, trimmed: the spread this feature exists to show.
function rec(starting: RecPlayer[], bench: RecPlayer[] = [], horizon = 6): Recommendation {
  return {
    mode: 'build', status: 'Optimal', formation: '3-4-3', squad_value: 100,
    xi_expected: 64.46, captain: starting[0], vice: starting[0], starting, bench,
    transfers_in: [], transfers_out: [], hits: 0, summary: '',
    margins: {
      status: 'ok', method: 'exact-forced-resolve', objective_version: 'objective-1.0',
      horizon, baseline_objective: 308.638787, baseline_matches_solution: true,
      elapsed_s: 2.88, note: '', by_player: {},
    },
  }
}

const optimal = (points: number): Margin => ({ points, status: 'optimal' })
const required: Margin = { points: null, status: 'required', note: 'no legal squad' }

describe('banding a margin', () => {
  it('calls anything past a -4 hit the spine', () => {
    expect(marginBand(optimal(8.288))?.name).toBe('spine')
    expect(marginBand(optimal(SPINE_THRESHOLD))?.name).toBe('spine')
  })

  it('calls anything inside the projection error a free swap', () => {
    expect(marginBand(optimal(0.097))?.name).toBe('free')
    expect(marginBand(optimal(0.0))?.name).toBe('free')
    expect(marginBand(optimal(FREE_THRESHOLD - 0.001))?.name).toBe('free')
  })

  it('puts the middle in settled', () => {
    expect(marginBand(optimal(FREE_THRESHOLD))?.name).toBe('settled')
    expect(marginBand(optimal(2.181))?.name).toBe('settled')
    expect(marginBand(optimal(SPINE_THRESHOLD - 0.001))?.name).toBe('settled')
  })

  it('gives a structurally required pick its own band, not a number', () => {
    expect(marginBand(required)).toBe(BANDS.required)
    expect(formatMargin(required)).toBe('locked')
  })

  it('refuses to render an unmeasured slot as a free swap', () => {
    // The whole point of the feature is not overstating confidence. A margin
    // that was never measured is absent, and absent is not zero.
    expect(marginBand({ points: null, status: 'not_computed' })).toBeNull()
    expect(marginBand({ points: null, status: 'anomaly' })).toBeNull()
    expect(marginBand(undefined)).toBeNull()
    expect(formatMargin({ points: null, status: 'not_computed' })).toBe('—')
    expect(formatMargin(undefined)).toBe('—')
  })
})

describe('formatting keeps the resolution that matters', () => {
  it('does not round the free-swap band into a single value', () => {
    // 0.097, 0.101 and 0.243 are three different answers; one decimal makes
    // the first two identical and the whole band unreadable.
    expect(formatMargin(optimal(0.097))).toBe('0.10')
    expect(formatMargin(optimal(0.243))).toBe('0.24')
    expect(formatMargin(optimal(8.288))).toBe('8.29')
  })
})

describe('ranking the fifteen', () => {
  const squad = rec(
    [pick(1, 'B.Fernandes', optimal(8.288)), pick(2, 'Mbeumo', optimal(5.672)),
     pick(3, 'Virgil', optimal(2.181))],
    [pick(4, 'Kelleher', optimal(0.097)), pick(5, 'Dubravka', optimal(0.253))],
  )

  it('orders by how much the pick matters', () => {
    expect(rankedPicks(squad).map((p) => p.player.name)).toEqual(
      ['B.Fernandes', 'Mbeumo', 'Virgil', 'Dubravka', 'Kelleher'],
    )
  })

  it('sorts a structurally required pick above every number', () => {
    const locked = rec([pick(9, 'Enabler', required), pick(1, 'B.Fernandes', optimal(8.288))])
    expect(rankedPicks(locked)[0].player.name).toBe('Enabler')
  })

  it('drops players with no margin rather than inventing one', () => {
    const partial = rec([pick(1, 'Measured', optimal(3)), pick(2, 'Unmeasured')])
    expect(rankedPicks(partial).map((p) => p.player.name)).toEqual(['Measured'])
  })

  it('leads with the spread, not with a definition', () => {
    const line = marginHeadline(squad)
    expect(line).toContain('B.Fernandes is worth 8.29')
    expect(line).toContain('2 of 5')   // Kelleher 0.097 and Dubravka 0.253
    expect(line).toContain('6 GWs')
  })

  it('says so when the top pick cannot be replaced at all', () => {
    const locked = rec([pick(9, 'Enabler', required), pick(1, 'Other', optimal(1))])
    expect(marginHeadline(locked)).toContain('Enabler cannot be replaced at all')
  })
})

describe('deciding whether to render at all', () => {
  it('renders when the sweep succeeded', () => {
    expect(marginsUsable(rec([pick(1, 'A', optimal(1))]))).toBe(true)
  })

  it('stays silent when the sweep was unavailable', () => {
    const r = rec([pick(1, 'A', optimal(1))])
    r.margins!.status = 'unavailable'
    expect(marginsUsable(r)).toBe(false)
  })

  it('stays silent on an artifact published before margins existed', () => {
    const r = rec([pick(1, 'A')])
    delete r.margins
    expect(marginsUsable(r)).toBe(false)
  })

  it('still renders a truncated sweep for the players it did measure', () => {
    const r = rec([pick(1, 'A', optimal(1)), pick(2, 'B', { points: null, status: 'not_computed' })])
    r.margins!.status = 'truncated'
    expect(marginsUsable(r)).toBe(true)
    expect(rankedPicks(r)).toHaveLength(2)
    expect(rankedPicks(r)[1].band).toBeNull()
  })
})
