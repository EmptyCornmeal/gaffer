import { describe, expect, it } from 'vitest'
import {
  BOTTOM_TABS, DEFAULT_ROUTE, KNOWN_ROUTES, MORE_TABS, NAV_TABS, PRIMARY_TABS,
  REDIRECTS, SECONDARY_TABS,
  normaliseRoute, routeSection,
} from './nav'

describe('normaliseRoute', () => {
  it('resolves every declared tab', () => {
    for (const t of NAV_TABS) {
      expect(normaliseRoute(`#/${t.key}`)).toBe(t.key)
    }
  })

  it('falls back to This Week for an empty hash', () => {
    expect(normaliseRoute('')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute('#')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute('#/')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute(null)).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute(undefined)).toBe(DEFAULT_ROUTE)
  })

  it('falls back to This Week for unknown routes', () => {
    // Previously these rendered chrome around a completely empty <main>.
    expect(normaliseRoute('#/nope')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute('#/old-bookmark')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute('#/league/271619')).toBe('league')
  })

  it('normalises case and stray whitespace', () => {
    // 6.3/6.4: retired into Research, and the redirect is what a bookmark
    // from last week lands on.
    expect(normaliseRoute('#/Players')).toBe('research')
    expect(normaliseRoute('#/MY-TEAM')).toBe('my-team')
    expect(normaliseRoute('#/  planner  ')).toBe('planner')
  })

  it('strips query strings and trailing fragments', () => {
    expect(normaliseRoute('#/players?sort=xp')).toBe('research')
    expect(normaliseRoute('#/live&x=1')).toBe('live')
  })

  it('is idempotent — normalising twice cannot loop', () => {
    for (const raw of ['#/nope', '#/Players', '', '#/meta?sort=xp']) {
      const once = normaliseRoute(raw)
      expect(normaliseRoute(`#/${once}`)).toBe(once)
    }
  })

  it('keeps KNOWN_ROUTES in sync with everything the app can render', () => {
    // NAV_TABS alone stopped being the whole answer in 6.3: `Live` and
    // `Planner` are reachable destinations that no longer hold a permanent
    // tab. A route the app can render but this set does not know about would
    // silently normalise to home, which is the regression this guards.
    expect(KNOWN_ROUTES.size).toBe(NAV_TABS.length + SECONDARY_TABS.length)
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
    // The partition spans NAV_TABS plus SECONDARY_TABS after 6.3. The property
    // that matters is unchanged and is the one this test exists for: every
    // destination the app can route to appears in exactly one of the two
    // lists, so nothing becomes unreachable by being dropped from a menu.
    const covered = [...PRIMARY_TABS, ...MORE_TABS].map((t) => t.key)
    expect(covered.slice().sort()).toEqual([...KNOWN_ROUTES].sort())
    expect(new Set(covered).size).toBe(covered.length)
  })

  it('overlaps nowhere', () => {
    const primary = new Set(PRIMARY_TABS.map((t) => t.key))
    for (const t of MORE_TABS) expect(primary.has(t.key)).toBe(false)
  })

  it('accounts for every route', () => {
    // After 6.3 the header partition spans NAV_TABS plus the secondary
    // destinations, which are reachable but do not compete for prime
    // navigation. Every known route still appears in exactly one list --
    // that is the property worth holding, not the arithmetic against
    // NAV_TABS alone.
    expect(PRIMARY_TABS.length + MORE_TABS.length)
      .toBe(NAV_TABS.length + SECONDARY_TABS.length)
    expect(KNOWN_ROUTES.size).toBe(PRIMARY_TABS.length + MORE_TABS.length)
  })

  it('keeps the primary set small enough to fit beside the search box', () => {
    // Seven labelled buttons is where 1024px started to squeeze the search.
    expect(PRIMARY_TABS.length).toBeGreaterThanOrEqual(4)
    expect(PRIMARY_TABS.length).toBeLessThanOrEqual(6)
  })

  it('leads with the weekly questions, in NAV_TABS order', () => {
    expect(PRIMARY_TABS.map((t) => t.key)).toEqual(
      ['home', 'my-team', 'league', 'research', 'model'],
    )
  })

  it('preserves source order within each list', () => {
    const order = [...NAV_TABS, ...SECONDARY_TABS].map((t) => t.key)
    for (const list of [PRIMARY_TABS, MORE_TABS]) {
      const idx = list.map((t) => order.indexOf(t.key))
      expect(idx).toEqual(idx.slice().sort((a, b) => a - b))
    }
  })

  it('carries the same object identity as its source list, so labels cannot drift', () => {
    const source = [...NAV_TABS, ...SECONDARY_TABS]
    for (const t of [...PRIMARY_TABS, ...MORE_TABS]) {
      expect(source).toContain(t)
    }
  })

  it('still routes every partitioned key', () => {
    for (const t of [...PRIMARY_TABS, ...MORE_TABS]) {
      expect(normaliseRoute('#/' + t.key)).toBe(t.key)
    }
  })

  it('puts the reference pages behind More rather than off the right-hand edge', () => {
    // `Model` was behind More because eight destinations did not fit. Five
    // do, so the trust surface is now a first-class tab and More carries
    // the two secondary destinations instead.
    expect(PRIMARY_TABS.map((t) => t.key)).toContain('model')
    expect(MORE_TABS.map((t) => t.key)).toEqual(['live', 'planner'])
  })
})

describe('the phone bottom bar', () => {
  it('shows the four destinations a phone opens first', () => {
    // Live left the phone bar in 6.3: it advertised nothing for 91% of the
    // week and is now the Saturday state of Now, which links to it when it
    // matters.
    expect(BOTTOM_TABS.map((t) => t.label)).toEqual(
      ['Now', 'My Team', 'League', 'Research'],
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
    expect(normaliseRoute('#/Fixtures/')).toBe('research')
    expect(normaliseRoute('#/players')).toBe('research')
  })

  it('forwards a merged-away route to the page that absorbed it', () => {
    // Overview became Planner's "Model's ideal" tab. Someone's bookmark and
    // someone's home-screen icon still say #/overview, and dropping them on
    // This Week would look like the link had simply broken.
    expect(normaliseRoute('#/overview')).toBe('my-team')
    expect(normaliseRoute('#/Overview/')).toBe('my-team')
  })

  it('every redirect points at a route that exists', () => {
    for (const [from, to] of Object.entries(REDIRECTS)) {
      expect(KNOWN_ROUTES.has(to), `${from} -> ${to}`).toBe(true)
      expect(KNOWN_ROUTES.has(from), `${from} still routes`).toBe(false)
    }
  })

  it('does not accidentally route the More button label', () => {
    expect(KNOWN_ROUTES.has('more')).toBe(false)
  })
})

describe("routeSection (4.8 deep links)", () => {
  it("reads the anchor a Model-page link carried", () => {
    // `#acc-minutes` cannot work: the hash IS the router, so an ordinary
    // anchor navigates to the default page instead of scrolling.
    expect(routeSection("#/model/acc-minutes")).toBe("acc-minutes")
    expect(routeSection("#/model/acc-horizon")).toBe("acc-horizon")
  })

  it("still routes to the page itself", () => {
    expect(normaliseRoute("#/model/acc-minutes")).toBe("model")
  })

  it("is null when no section was asked for", () => {
    expect(routeSection("#/model")).toBeNull()
    expect(routeSection("")).toBeNull()
    expect(routeSection(null)).toBeNull()
  })

  it("refuses anything that is not a plain element id", () => {
    // A hash is attacker-adjacent input on a static site; it must never become
    // a selector or a script-bearing string.
    expect(routeSection("#/model/../etc")).toBeNull()
    expect(routeSection("#/model/<script>")).toBeNull()
    expect(routeSection("#/model/9lives")).toBeNull()
    expect(routeSection("#/model/" + "a".repeat(200))).toBeNull()
  })
})
