import { describe, expect, it } from 'vitest'
import {
  BOTTOM_TABS, DEFAULT_ROUTE, KNOWN_ROUTES, MORE_TABS, NAV_TABS, PRIMARY_TABS,
  normaliseRoute,
} from './nav'

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

// The desktop header rendered all fifteen NAV_TABS at once. At 1024-1440px the
// document measured 1667px, Help sat off-screen entirely, and the player search
// collapsed to 26px. The header now shows a bounded primary set with the rest
// behind a More menu, and both are derived from NAV_TABS so nothing can go
// missing. Behaviour (menu, keyboard, focus, widths) is verified in a real
// browser during acceptance; these tests hold the partition itself.

describe('the header partition', () => {
  it('covers every route exactly once', () => {
    const covered = [...PRIMARY_TABS, ...MORE_TABS].map((t) => t.key)
    expect(covered.slice().sort()).toEqual(NAV_TABS.map((t) => t.key).sort())
    expect(new Set(covered).size).toBe(covered.length)
  })

  it('overlaps nowhere', () => {
    const primary = new Set(PRIMARY_TABS.map((t) => t.key))
    for (const t of MORE_TABS) expect(primary.has(t.key)).toBe(false)
  })

  it('accounts for all fifteen routes', () => {
    expect(PRIMARY_TABS.length + MORE_TABS.length).toBe(NAV_TABS.length)
    expect(KNOWN_ROUTES.size).toBe(PRIMARY_TABS.length + MORE_TABS.length)
  })

  it('keeps the primary set small enough to fit beside the search box', () => {
    // Seven labelled buttons is where 1024px started to squeeze the search.
    expect(PRIMARY_TABS.length).toBeGreaterThanOrEqual(4)
    expect(PRIMARY_TABS.length).toBeLessThanOrEqual(6)
  })

  it('leads with the weekly questions, in NAV_TABS order', () => {
    expect(PRIMARY_TABS.map((t) => t.key)).toEqual(
      ['home', 'live', 'my-team', 'players', 'fixtures', 'strategy'],
    )
  })

  it('preserves NAV_TABS order within each list', () => {
    const order = NAV_TABS.map((t) => t.key)
    for (const list of [PRIMARY_TABS, MORE_TABS]) {
      const idx = list.map((t) => order.indexOf(t.key))
      expect(idx).toEqual(idx.slice().sort((a, b) => a - b))
    }
  })

  it('carries the same object identity as NAV_TABS, so labels cannot drift', () => {
    for (const t of [...PRIMARY_TABS, ...MORE_TABS]) {
      expect(NAV_TABS).toContain(t)
    }
  })

  it('still routes every partitioned key', () => {
    for (const t of [...PRIMARY_TABS, ...MORE_TABS]) {
      expect(normaliseRoute('#/' + t.key)).toBe(t.key)
    }
  })

  it('puts Help behind More rather than off the right-hand edge', () => {
    expect(MORE_TABS.map((t) => t.key)).toContain('help')
  })
})

describe('the phone bottom bar is untouched', () => {
  it('still shows exactly This Week, Live, My Team and Players', () => {
    expect(BOTTOM_TABS.map((t) => t.label)).toEqual(
      ['This Week', 'Live', 'My Team', 'Players'],
    )
  })

  it('leaves the fifth slot for More', () => {
    expect(BOTTOM_TABS).toHaveLength(4)
  })

  it('does not depend on the desktop partition', () => {
    // A route may sit on the bottom bar and behind desktop More, or vice versa;
    // the two are independent by design. Assert only that nothing vanished.
    for (const t of BOTTOM_TABS) expect(KNOWN_ROUTES.has(t.key)).toBe(true)
  })
})

describe('unknown hashes after the partition', () => {
  it.each(['#/more', '#/menu', '#/nope', '#/'])('%s -> home', (raw) => {
    expect(normaliseRoute(raw)).toBe(DEFAULT_ROUTE)
  })

  it('still resolves a More-menu route from a deep link', () => {
    expect(normaliseRoute('#/Help/')).toBe('help')
    expect(normaliseRoute('#/overview')).toBe('overview')
  })

  it('does not accidentally route the More button label', () => {
    expect(KNOWN_ROUTES.has('more')).toBe(false)
  })
})
