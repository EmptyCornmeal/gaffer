// Single source of truth for the app's pages, so the topbar, the mobile drawer,
// and the mobile bottom bar never drift out of sync.

export type Tab = { key: string; label: string; icon: string }

// icon = a key into components/Icon.svelte (Lucide/Feather set), not an emoji.
/**
 * 6.3 -- five destinations, one per question a reader actually has.
 *
 * There were eight, and the shape of them was the problem rather than the
 * count. `Live` held prime navigation for the 9% of the week football is being
 * played and was a dead tab for the other 91%; it is now the Saturday state of
 * `Now` (see lib/now.ts), reachable from the surface that knows when it
 * matters. `Players` and `Fixtures` are reference material behind a decision,
 * not decisions, so they are one `Research` destination. `Planner` is the
 * multi-week view of a squad, which is what `My Team` is for.
 *
 * Every retired key keeps working: see REDIRECTS. A bookmark from last week
 * must not land on a blank page.
 */
export const NAV_TABS: Tab[] = [
  { key: 'home', label: 'Now', icon: 'zap' },
  { key: 'my-team', label: 'My Team', icon: 'shirt' },
  { key: 'league', label: 'League', icon: 'trophy' },
  { key: 'research', label: 'Research', icon: 'search' },
  { key: 'model', label: 'Model', icon: 'target' },
]

// The primary destinations shown directly on the phone bottom bar; the rest live
// behind the "More" button (which opens the settings/nav drawer). "This Week" is
// first because it answers the question a phone user actually opened the app to
// ask.
export const BOTTOM_TABS: Tab[] = NAV_TABS.filter((t) =>
  ['home', 'my-team', 'league', 'research'].includes(t.key),
)

/**
 * The destinations shown as labelled buttons in the desktop header.
 *
 * Fifteen of them could not coexist with the logo, the deadline countdown, the
 * freshness chip, the player search and the theme button: at 1024-1440px the
 * document was 1667px wide, Help sat entirely off-screen, and the search
 * collapsed to 26px. Compressing the labels until they fit would have made
 * fifteen illegible buttons instead of one usable row.
 *
 * So the header carries the six destinations that answer a weekly question, and
 * everything else lives behind a "More" menu. Both lists are derived from
 * NAV_TABS by key, so a route added there appears in exactly one of them and
 * neither can drift.
 */
/**
 * 6.3 -- reachable, but not competing for prime navigation.
 *
 * `Live` is the Saturday state of `Now`, which links to it when football is
 * actually being played; the rest of the week it should not be a permanent tab
 * advertising nothing. `Planner` is the multi-week view of a squad and belongs
 * with `My Team`.
 *
 * They are NOT redirected away. The plan folds the multi-week path into a
 * `My Team` tab, and until that tab exists, redirecting `planner` would delete
 * a working screen rather than move it -- which is a regression wearing a
 * consolidation's clothes.
 */
export const SECONDARY_TABS: Tab[] = [
  { key: 'live', label: 'Live', icon: 'flame' },
  { key: 'planner', label: 'Planner', icon: 'compass' },
]

const PRIMARY_KEYS = ['home', 'my-team', 'league', 'research', 'model'] as const

export const PRIMARY_TABS: Tab[] = NAV_TABS.filter((t) =>
  (PRIMARY_KEYS as readonly string[]).includes(t.key),
)

/**
 * Everything the header does not show directly.
 *
 * After 6.3 the five primary destinations fit, so this carries the secondary
 * ones instead of the overflow it used to hold. Nothing is unreachable: that
 * is the rule the consolidation is held to.
 */
export const MORE_TABS: Tab[] = [
  ...NAV_TABS.filter((t) => !(PRIMARY_KEYS as readonly string[]).includes(t.key)),
  ...SECONDARY_TABS,
]

/**
 * Routes loaded as separate chunks (see App.svelte's LAZY map).
 *
 * Exported so a test can assert the two lists stay in step: a heavy route with
 * no lazy entry silently re-inflates the initial bundle, which is exactly the
 * regression the performance budget exists to catch.
 */
export const HEAVY_ROUTES: ReadonlySet<string> = new Set([
  'planner', 'research', 'live', 'league', 'model',
])

export const KNOWN_ROUTES: ReadonlySet<string> = new Set(
  [...NAV_TABS, ...SECONDARY_TABS].map((t) => t.key))
export const DEFAULT_ROUTE = 'home'

/**
 * Map a raw `location.hash` to a canonical route key.
 *
 * Unknown, empty or malformed hashes resolve to the home screen rather than
 * rendering an empty <main>. This normalises rather than redirecting, so
 * back/forward keep working and there is no possibility of a rewrite loop.
 *
 * Hash routing (not history routing) is deliberate: GitHub Pages serves no
 * rewrite rules, so a deep link to /review would 404. `#/review` cannot.
 */
/**
 * Routes that used to exist, and the page that absorbed them.
 *
 * A merged page's URL is in someone's history and possibly on their home
 * screen. Falling through to DEFAULT_ROUTE would silently drop them on This
 * Week with no explanation; mapping them forward lands them where the thing
 * they asked for actually lives now. Entries are permanent — the cost of
 * keeping one is a line, and the cost of dropping one is a dead bookmark.
 */
export const REDIRECTS: Readonly<Record<string, string>> = {
  overview: 'my-team',
  strategy: 'league',
  meta: 'league',
  chips: 'my-team',
  review: 'home',
  news: 'research',
  help: 'model',
  accuracy: 'model',
  // 6.4 -- retired in the 6.3 consolidation. Every one of these was a real
  // destination a reader could have bookmarked, and a bookmark that silently
  // lands on the default page is indistinguishable from a broken app.
  players: 'research',
  fixtures: 'research',
  // NOT `planner`. It is a SECONDARY_TAB, and REDIRECTS is consulted before
  // KNOWN_ROUTES -- listing it here would have made the Planner unreachable
  // while the secondary list claimed otherwise. It moves into `My Team` when
  // that tab exists, and not one release before.
}

/**
 * 4.8 -- the anchor a deep link carried, if any.
 *
 * `#/model/acc-minutes` routes to `model` (normaliseRoute already splits on
 * `/`) and asks for the `acc-minutes` section. A bare `#acc-minutes` cannot
 * work in this app: the hash IS the router, so an anchor written the ordinary
 * way navigates to the default page instead of scrolling.
 *
 * Returns null for anything that is not a plain element id, so a hash cannot
 * become a selector.
 */
export function routeSection(hash: string | null | undefined): string | null {
  const rest = (hash ?? '').replace(/^#\/?/, '').split('/')[1]
  if (!rest) return null
  const id = rest.split(/[?&#]/)[0].trim().toLowerCase()
  return /^[a-z][a-z0-9-]{0,63}$/.test(id) ? id : null
}

export function normaliseRoute(hash: string | null | undefined): string {
  const key = (hash ?? '')
    .replace(/^#\/?/, '')
    .split(/[?&#/]/)[0]
    .trim()
    .toLowerCase()
  if (key in REDIRECTS) return REDIRECTS[key]
  return KNOWN_ROUTES.has(key) ? key : DEFAULT_ROUTE
}
