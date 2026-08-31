import { describe, expect, it } from 'vitest'
import { gameweekHeading } from './Live.svelte'

// ---------------------------------------------------------------------------
// A14 — the Live page names its gameweek and its state
//
// Read live on 2026-08-31: `live.json` reported gameweek 2 with
// `fixture_summary: { total: 10, by_state: { awaiting_bonus: 9, scheduled: 1 } }`
// while `meta.json` reported `current_gw: "3"` and `squad_source_event: 2`. The
// header said "Live", My Team said "Gameweek 3", and nothing on either screen
// explained how both could be true.
// ---------------------------------------------------------------------------

const LIVE_GW2 = {
  gameweek: 2,
  fixture_summary: { total: 10, by_state: { awaiting_bonus: 9, scheduled: 1 } },
}
const META = { current_gw: '3', squad_source_event: 2 }

describe('gameweekHeading', () => {
  it('names the gameweek being scored, which is not the one being picked', () => {
    const h = gameweekHeading(LIVE_GW2, META, '4 September')
    expect(h.title).toBe('Live · GW2')
    expect(h.state).toBe(
      "GW2's deadline has passed and 1 of its 10 fixtures is still to kick off. "
      + "GW3 is the gameweek you're picking now — deadline 4 September.",
    )
  })

  it('drops the deadline when there is none to quote', () => {
    expect(gameweekHeading(LIVE_GW2, META).state).toContain(
      "GW3 is the gameweek you're picking now.",
    )
  })

  it('says nothing about a second gameweek when there is only one', () => {
    const h = gameweekHeading(LIVE_GW2, { current_gw: '2', squad_source_event: 2 })
    expect(h.state).toBe("GW2's deadline has passed and 1 of its 10 fixtures is still to kick off.")
  })

  it('reports matches in play', () => {
    const h = gameweekHeading(
      { gameweek: 2, fixture_summary: { total: 10, by_state: { live: 3, scheduled: 2, finished: 5 } } },
      META,
    )
    expect(h.state).toContain('3 of its 10 fixtures are in play and 2 are still to kick off')
  })

  it('counts half time as in play, the way the chip row does', () => {
    const h = gameweekHeading(
      { gameweek: 2, fixture_summary: { total: 10, by_state: { live: 1, half_time: 1, finished: 8 } } },
      META,
    )
    expect(h.state).toContain('2 of its 10 fixtures are in play')
  })

  it('says so when the football is done', () => {
    const h = gameweekHeading(
      { gameweek: 2, fixture_summary: { total: 10, by_state: { finished: 10 } } },
      META,
    )
    expect(h.state).toContain('all 10 of its fixtures have been played')
  })

  // The claim "the deadline has passed" is the producer's, never a clock
  // comparison here — see the note in Live.svelte.
  it('never claims a deadline has passed for a gameweek that is not locked', () => {
    const h = gameweekHeading(
      { gameweek: 1, fixture_summary: { total: 10, by_state: { scheduled: 10 } } },
      { current_gw: '1', squad_source_event: null },
    )
    expect(h.title).toBe('Live · GW1')
    expect(h.state).toBe("10 of GW1's 10 fixtures are still to kick off.")
  })

  it('falls back to the locked gameweek before the first fetch lands', () => {
    const h = gameweekHeading(null, META, '4 September')
    expect(h.title).toBe('Live · GW2')
    expect(h.state).toBe(
      "GW2's deadline has passed. GW3 is the gameweek you're picking now — deadline 4 September.",
    )
  })

  it('stays plain "Live" when no gameweek is known at all', () => {
    expect(gameweekHeading(null, null)).toEqual({ title: 'Live', state: null })
    expect(gameweekHeading(null, { current_gw: '', squad_source_event: null }).title).toBe('Live')
  })

  it('handles a single-fixture gameweek without a plural', () => {
    const h = gameweekHeading(
      { gameweek: 2, fixture_summary: { total: 1, by_state: { scheduled: 1 } } },
      { current_gw: '2', squad_source_event: 2 },
    )
    expect(h.state).toBe("GW2's deadline has passed and 1 of its 1 fixture is still to kick off.")
  })
})
