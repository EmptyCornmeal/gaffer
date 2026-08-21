// Single source of truth for the app's pages, so the topbar, the mobile drawer,
// and the mobile bottom bar never drift out of sync.

export type Tab = { key: string; label: string; icon: string }

// icon = a key into components/Icon.svelte (Lucide/Feather set), not an emoji.
export const NAV_TABS: Tab[] = [
  { key: 'home', label: 'This Week', icon: 'zap' },
  { key: 'live', label: 'Live', icon: 'flame' },
  { key: 'my-team', label: 'My Team', icon: 'shirt' },
  { key: 'planner', label: 'Planner', icon: 'compass' },
  { key: 'players', label: 'Players', icon: 'users' },
  { key: 'fixtures', label: 'Fixtures', icon: 'calendar' },
  { key: 'league', label: 'League', icon: 'trophy' },
  { key: 'news', label: 'News', icon: 'news' },
  { key: 'accuracy', label: 'Accuracy', icon: 'target' },
  { key: 'help', label: 'Help', icon: 'help' },
]

// The primary destinations shown directly on the phone bottom bar; the rest live
// behind the "More" button (which opens the settings/nav drawer). "This Week" is
// first because it answers the question a phone user actually opened the app to
// ask.
export const BOTTOM_TABS: Tab[] = NAV_TABS.filter((t) =>
  ['home', 'live', 'my-team', 'players'].includes(t.key),
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
const PRIMARY_KEYS = ['home', 'live', 'my-team', 'players', 'fixtures', 'league'] as const

export const PRIMARY_TABS: Tab[] = NAV_TABS.filter((t) =>
  (PRIMARY_KEYS as readonly string[]).includes(t.key),
)

/** Everything NAV_TABS has that the header does not show directly. */
export const MORE_TABS: Tab[] = NAV_TABS.filter((t) =>
  !(PRIMARY_KEYS as readonly string[]).includes(t.key),
)

/**
 * Routes loaded as separate chunks (see App.svelte's LAZY map).
 *
 * Exported so a test can assert the two lists stay in step: a heavy route with
 * no lazy entry silently re-inflates the initial bundle, which is exactly the
 * regression the performance budget exists to catch.
 */
export const HEAVY_ROUTES: ReadonlySet<string> = new Set([
  'planner', 'players', 'live',
  'league', 'news', 'accuracy',
])

export const KNOWN_ROUTES: ReadonlySet<string> = new Set(NAV_TABS.map((t) => t.key))
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
  overview: 'planner',
  strategy: 'league',
  meta: 'league',
  chips: 'my-team',
  review: 'home',
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
