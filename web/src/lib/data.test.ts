import { describe, expect, it } from 'vitest'
import { staleSeasons } from './data'

// T-29: an artifact from a previous season parses cleanly and renders as
// current, because FPL reuses element ids. The only thing that tells them apart
// is the season each one declares.

describe('staleSeasons', () => {
  it('drops an artifact that names a different season', () => {
    const out = staleSeasons('2027-28', {
      decision: { season: '2026-27' },
      review: { season: '2027-28' },
    })
    expect([...out]).toEqual(['decision'])
  })

  it('keeps an artifact that names no season at all', () => {
    // An older build is allowed to be silent. Refusing to render everything on
    // a missing field would be worse than the problem it guards against.
    expect([...staleSeasons('2027-28', { decision: {} })]).toEqual([])
  })

  it('drops nothing when meta itself declares no season', () => {
    expect([...staleSeasons(undefined, { decision: { season: '2026-27' } })]).toEqual([])
    expect([...staleSeasons(null, { decision: { season: '2026-27' } })]).toEqual([])
  })

  it('ignores nulls and non-objects', () => {
    const out = staleSeasons('2027-28', { a: null, b: 'string', c: 42, d: [] })
    expect([...out]).toEqual([])
  })

  it('drops every disagreeing artifact, not just the first', () => {
    const out = staleSeasons('2027-28', {
      decision: { season: '2026-27' },
      review: { season: '2025-26' },
      notifications: { season: '2027-28' },
    })
    expect([...out].sort()).toEqual(['decision', 'review'])
  })
})
