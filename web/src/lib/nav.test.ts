import { describe, expect, it } from 'vitest'
import { DEFAULT_ROUTE, KNOWN_ROUTES, NAV_TABS, normaliseRoute } from './nav'

describe('normaliseRoute', () => {
  it('resolves every declared tab', () => {
    for (const t of NAV_TABS) {
      expect(normaliseRoute(`#/${t.key}`)).toBe(t.key)
    }
  })

  it('falls back to Overview for an empty hash', () => {
    expect(normaliseRoute('')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute('#')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute('#/')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute(null)).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute(undefined)).toBe(DEFAULT_ROUTE)
  })

  it('falls back to Overview for unknown routes', () => {
    // Previously these rendered chrome around a completely empty <main>.
    expect(normaliseRoute('#/nope')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute('#/old-bookmark')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute('#/league/271619')).toBe('league')
  })

  it('normalises case and stray whitespace', () => {
    expect(normaliseRoute('#/Players')).toBe('players')
    expect(normaliseRoute('#/MY-TEAM')).toBe('my-team')
    expect(normaliseRoute('#/  planner  ')).toBe('planner')
  })

  it('strips query strings and trailing fragments', () => {
    expect(normaliseRoute('#/meta?sort=xp')).toBe('meta')
    expect(normaliseRoute('#/chips&x=1')).toBe('chips')
  })

  it('is idempotent — normalising twice cannot loop', () => {
    for (const raw of ['#/nope', '#/Players', '', '#/meta?sort=xp']) {
      const once = normaliseRoute(raw)
      expect(normaliseRoute(`#/${once}`)).toBe(once)
    }
  })

  it('keeps KNOWN_ROUTES in sync with the rendered nav', () => {
    expect(KNOWN_ROUTES.size).toBe(NAV_TABS.length)
  })
})
