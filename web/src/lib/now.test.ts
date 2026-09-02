import { describe, expect, it } from 'vitest'
import {
  blockOrder, DECIDE_HOURS, LOCKED_HOURS, nowState, STATE_COPY, timeToDeadline,
  WAIT_HOURS, type NowContext, type NowState,
} from './now'

const HOUR = 3_600_000
const NOW = Date.parse('2026-09-04T12:00:00Z')

const ctx = (over: Partial<NowContext> = {}): NowContext => ({
  now: NOW,
  deadline: NOW + 100 * HOUR,
  footballOn: false,
  hasFreshReview: false,
  ...over,
})

const at = (hoursUntilDeadline: number, over: Partial<NowContext> = {}) =>
  nowState(ctx({ deadline: NOW + hoursUntilDeadline * HOUR, ...over }))

describe('which week it is', () => {
  it('leads with the decision inside the decide window', () => {
    expect(at(DECIDE_HOURS - 1)).toBe('decide')
    expect(at(1)).toBe('decide')
  })

  it('says "wait" when the projections are ready and the team news is not', () => {
    expect(at(DECIDE_HOURS + 1)).toBe('wait')
    expect(at(WAIT_HOURS - 1)).toBe('wait')
  })

  it('says "watch" when the deadline is far enough that acting buys nothing', () => {
    expect(at(WAIT_HOURS + 1)).toBe('watch')
    expect(at(140)).toBe('watch')
  })

  it('names the locked gap, and stops naming it once it is over', () => {
    // 5.4's window, on the surface this time.
    expect(at(-0.5)).toBe('locked')
    expect(at(-LOCKED_HOURS + 0.1)).toBe('locked')
    expect(at(-LOCKED_HOURS - 1)).toBe('watch')
  })

  it('lets football beat every clock', () => {
    // Whatever the deadline says, if the ball is moving then the only number
    // that changes is the score.
    for (const h of [-1, 1, DECIDE_HOURS + 1, 200]) {
      expect(at(h, { footballOn: true })).toBe('live')
    }
  })

  it('puts an unread result ahead of a distant deadline, but not ahead of a near one', () => {
    // On a Monday the first question is "what happened?". On a Friday it is
    // not, however interesting last week was.
    expect(at(100, { hasFreshReview: true })).toBe('review')
    expect(at(DECIDE_HOURS - 1, { hasFreshReview: true })).toBe('decide')
  })

  it('degrades sensibly with no deadline at all', () => {
    expect(nowState(ctx({ deadline: null }))).toBe('watch')
    expect(nowState(ctx({ deadline: null, hasFreshReview: true }))).toBe('review')
    expect(nowState(ctx({ deadline: NaN }))).toBe('watch')
  })
})

describe('the state is always sayable', () => {
  const states: NowState[] = ['review', 'watch', 'wait', 'decide', 'locked', 'live']

  it('every state has a label and a sentence', () => {
    // 6.2. A surface that changes shape silently is harder to learn, not
    // easier, so there is no state the UI can enter without being able to name
    // it.
    for (const s of states) {
      expect(STATE_COPY[s].label, s).toBeTruthy()
      expect(STATE_COPY[s].says.length, s).toBeGreaterThan(20)
    }
  })

  it('no two states share a label', () => {
    const labels = states.map((s) => STATE_COPY[s].label)
    expect(new Set(labels).size).toBe(labels.length)
  })
})

describe('ordering, not hiding', () => {
  const states: NowState[] = ['review', 'watch', 'wait', 'decide', 'locked', 'live']

  it('every state can still reach every block', () => {
    // The rule that keeps this from being a gimmick. A reader who wants the
    // decision on a Monday scrolls; they are never told to come back later.
    for (const s of states) {
      expect(new Set(blockOrder(s)).size, s).toBe(6)
    }
  })

  it('leads with what the state is for', () => {
    expect(blockOrder('decide')[0]).toBe('decision')
    expect(blockOrder('live')[0]).toBe('live')
    expect(blockOrder('review')[0]).toBe('review')
    expect(blockOrder('wait')[0]).toBe('calendar')
  })

  it('never repeats a block', () => {
    for (const s of states) {
      const order = blockOrder(s)
      expect(order.length).toBe(new Set(order).size)
    }
  })
})

describe('the countdown', () => {
  it('coarsens as the deadline recedes, rather than faking precision', () => {
    expect(timeToDeadline(ctx({ deadline: NOW + 0.25 * HOUR }))).toBe('15 min')
    expect(timeToDeadline(ctx({ deadline: NOW + 6 * HOUR }))).toBe('6h')
    expect(timeToDeadline(ctx({ deadline: NOW + 100 * HOUR }))).toBe('4 days')
  })

  it('says the deadline has gone rather than counting backwards', () => {
    expect(timeToDeadline(ctx({ deadline: NOW - HOUR }))).toBe('deadline passed')
  })

  it('is absent, not zero, with no deadline', () => {
    expect(timeToDeadline(ctx({ deadline: null }))).toBeNull()
  })
})
