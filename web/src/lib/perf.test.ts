import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { NAV_TABS, HEAVY_ROUTES, KNOWN_ROUTES, normaliseRoute, DEFAULT_ROUTE } from './nav'

// --------------------------------------------------------------------------
// Performance budgets
//
// The site is opened on a phone, on mobile data, minutes before a deadline. The
// numbers below are budgets, not observations: they fail the build when a new
// import quietly re-inflates the entry chunk, which is the only way this stays
// fixed after the one commit that fixed it.
// --------------------------------------------------------------------------

const DIST = join(process.cwd(), 'dist', 'assets')

/** Entry JS a phone must download before anything renders. */
const ENTRY_JS_BUDGET_KB = 200
/** Total CSS. */
const CSS_BUDGET_KB = 80
/** Any single lazy route chunk. */
const ROUTE_CHUNK_BUDGET_KB = 60

function sizeKb(file: string): number {
  return readFileSync(join(DIST, file)).byteLength / 1024
}

const built = existsSync(DIST)
const files = built ? readdirSync(DIST) : []
// Vite names the entry `index-<hash>.js`; route chunks are named after the
// component they were split from.
const entryJs = files.filter((f) => /^index-.*\.js$/.test(f))
const sharedJs = files.filter((f) => /^disclose-version-.*\.js$/.test(f))
const css = files.filter((f) => f.endsWith('.css'))
const routeChunks = files.filter(
  (f) => f.endsWith('.js') && !entryJs.includes(f) && !sharedJs.includes(f))

describe.runIf(built)('bundle budgets (run `npm run build` first)', () => {
  it('ships an entry chunk', () => {
    expect(entryJs.length).toBe(1)
  })

  it('keeps the initial JS inside its budget', () => {
    const total = [...entryJs, ...sharedJs].reduce((s, f) => s + sizeKb(f), 0)
    expect(total, `initial JS is ${total.toFixed(1)} kB`)
      .toBeLessThan(ENTRY_JS_BUDGET_KB)
  })

  it('keeps CSS inside its budget', () => {
    const total = css.reduce((s, f) => s + sizeKb(f), 0)
    expect(total, `CSS is ${total.toFixed(1)} kB`).toBeLessThan(CSS_BUDGET_KB)
  })

  it('actually code-splits the heavy routes', () => {
    // One chunk per lazy route is the point; a single bundle means the dynamic
    // imports were inlined and the budget above is meaningless.
    expect(routeChunks.length).toBeGreaterThanOrEqual(HEAVY_ROUTES.size - 1)
  })

  it('keeps every route chunk small enough to fetch mid-navigation', () => {
    for (const f of routeChunks) {
      expect(sizeKb(f), `${f} is ${sizeKb(f).toFixed(1)} kB`)
        .toBeLessThan(ROUTE_CHUNK_BUDGET_KB)
    }
  })

  it('no chunk is empty (a sign of a broken split)', () => {
    for (const f of routeChunks) expect(sizeKb(f)).toBeGreaterThan(0.1)
  })
})

// --------------------------------------------------------------------------
// Routing — every nav entry must resolve, and deep links must survive Pages
// --------------------------------------------------------------------------

describe('routes', () => {
  it('every nav tab is a known route', () => {
    for (const t of NAV_TABS) expect(KNOWN_ROUTES.has(t.key)).toBe(true)
  })

  it('every heavy route is a real nav destination', () => {
    for (const r of HEAVY_ROUTES) expect(KNOWN_ROUTES.has(r)).toBe(true)
  })

  it('the default route is itself known', () => {
    expect(KNOWN_ROUTES.has(DEFAULT_ROUTE)).toBe(true)
  })

  it('the home screen is the default', () => {
    expect(DEFAULT_ROUTE).toBe('home')
    expect(normaliseRoute('')).toBe('home')
    expect(normaliseRoute(undefined)).toBe('home')
  })

  it.each([...KNOWN_ROUTES])('a direct link to #/%s resolves to itself', (r) => {
    expect(normaliseRoute(`#/${r}`)).toBe(r)
  })

  it('unknown and malformed hashes fall back rather than blanking', () => {
    expect(normaliseRoute('#/nope')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute('#//')).toBe(DEFAULT_ROUTE)
    expect(normaliseRoute('#/live/extra')).toBe('live')
    // 6.4: retired into Research, so the old key redirects rather than blanking.
    expect(normaliseRoute('#/players?x=1')).toBe('research')
  })

  it('hash routing is used, so GitHub Pages deep links cannot 404', () => {
    // A path-based route (/review) would need a server rewrite Pages does not
    // provide. Every route key must therefore be reachable via a hash.
    for (const r of KNOWN_ROUTES) {
      expect(normaliseRoute(`#/${r.toUpperCase()}`)).toBe(r)
    }
  })

  it('the new weekly-loop screens exist', () => {
    for (const r of ['home', 'live', 'my-team']) {
      expect(KNOWN_ROUTES.has(r)).toBe(true)
    }
  })
})

// A budget that skips itself is not a budget. `deploy.yml` and
// `scripts/verify.py` both build before they test, so `dist/` exists when these
// run; this asserts that ordering out loud, because the failure mode is silence.
describe('the budgets actually ran', () => {
  it('measured a real build', () => {
    expect(
      built,
      'dist/assets is missing — run `npm run build` before `npm run test`, ' +
        'or the performance budgets silently pass by not running',
    ).toBe(true)
  })
})
