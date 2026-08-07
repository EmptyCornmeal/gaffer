// Single source of truth for the app's pages, so the topbar, the mobile drawer,
// and the mobile bottom bar never drift out of sync.

export type Tab = { key: string; label: string; icon: string }

// icon = a key into components/Icon.svelte (Lucide/Feather set), not an emoji.
export const NAV_TABS: Tab[] = [
  { key: 'home', label: 'This Week', icon: 'zap' },
  { key: 'live', label: 'Live', icon: 'flame' },
  { key: 'review', label: 'Review', icon: 'award' },
  { key: 'my-team', label: 'My Team', icon: 'shirt' },
  { key: 'planner', label: 'Planner', icon: 'compass' },
  { key: 'players', label: 'Players', icon: 'users' },
  { key: 'fixtures', label: 'Fixtures', icon: 'calendar' },
  { key: 'chips', label: 'Chips', icon: 'layers' },
  { key: 'meta', label: 'Meta', icon: 'chart' },
  { key: 'strategy', label: 'Strategy', icon: 'shield' },
  { key: 'league', label: 'League', icon: 'trophy' },
  { key: 'news', label: 'News', icon: 'news' },
  { key: 'overview', label: 'Overview', icon: 'search' },
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
 * Routes loaded as separate chunks (see App.svelte's LAZY map).
 *
 * Exported so a test can assert the two lists stay in step: a heavy route with
 * no lazy entry silently re-inflates the initial bundle, which is exactly the
 * regression the performance budget exists to catch.
 */
export const HEAVY_ROUTES: ReadonlySet<string> = new Set([
  'planner', 'players', 'chips', 'meta', 'strategy', 'live', 'review',
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
export function normaliseRoute(hash: string | null | undefined): string {
  const key = (hash ?? '')
    .replace(/^#\/?/, '')
    .split(/[?&#/]/)[0]
    .trim()
    .toLowerCase()
  return KNOWN_ROUTES.has(key) ? key : DEFAULT_ROUTE
}
