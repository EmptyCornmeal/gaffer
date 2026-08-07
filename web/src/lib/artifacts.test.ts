import { describe, it, expect } from 'vitest'
import { readFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { parseDecision, parseLive, parseReview, parseNotifications } from './weekly'
import { parseStrategy } from './strategy'
import { parseBacktest } from './backtest'

// The artifacts this build actually serves, parsed by the parsers that will do
// it in the browser.
//
// Unit tests use hand-written fixtures, which is exactly why they cannot catch
// the failure that matters: the pipeline writing a shape the front-end refuses.
// This closes the loop against the real published files, so a version bump on
// one side without the other fails here rather than on a phone.

const D = join(process.cwd(), 'public', 'data')
const present = existsSync(join(D, 'meta.json'))
const read = (n: string): unknown =>
  existsSync(join(D, n)) ? JSON.parse(readFileSync(join(D, n), 'utf8')) : null

describe.runIf(present)('the published artifacts parse in this build', () => {
  it('decision.json', () => {
    const s = parseDecision(read('decision.json'))
    expect(s.kind, JSON.stringify(s).slice(0, 300)).toBe('ok')
  })

  it('live.json', () => {
    const s = parseLive(read('live.json'))
    expect(s.kind, JSON.stringify(s).slice(0, 300)).toBe('ok')
  })

  it('strategy.json', () => {
    const s = parseStrategy(read('strategy.json'))
    expect(s.kind, JSON.stringify(s).slice(0, 300)).toBe('ok')
  })

  it('notifications.json', () => {
    const s = parseNotifications(read('notifications.json'))
    expect(s.kind, JSON.stringify(s).slice(0, 300)).toBe('ok')
  })

  it('backtest.json', () => {
    const s = parseBacktest(read('backtest.json'))
    expect(s.kind, JSON.stringify(s).slice(0, 300)).toBe('ok')
  })

  it('published notifications are dry-run', () => {
    const s = parseNotifications(read('notifications.json'))
    if (s.kind === 'ok') expect(s.data.result.dry_run).toBe(true)
  })

  it('a missing optional artifact is `missing`, never malformed', () => {
    // review.json legitimately does not exist until a gameweek has finished.
    const s = parseReview(read('review.json'))
    expect(['ok', 'missing']).toContain(s.kind)
  })
})
