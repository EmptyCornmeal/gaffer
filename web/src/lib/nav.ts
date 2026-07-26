// Single source of truth for the app's pages, so the topbar, the mobile drawer,
// and the mobile bottom bar never drift out of sync.

export type Tab = { key: string; label: string; icon: string }

// icon = a key into components/Icon.svelte (Lucide/Feather set), not an emoji.
export const NAV_TABS: Tab[] = [
  { key: 'overview', label: 'Overview', icon: 'zap' },
  { key: 'my-team', label: 'My Team', icon: 'shirt' },
  { key: 'planner', label: 'Planner', icon: 'compass' },
  { key: 'players', label: 'Players', icon: 'users' },
  { key: 'fixtures', label: 'Fixtures', icon: 'calendar' },
  { key: 'chips', label: 'Chips', icon: 'layers' },
  { key: 'meta', label: 'Meta', icon: 'chart' },
  { key: 'league', label: 'League', icon: 'trophy' },
  { key: 'news', label: 'News', icon: 'news' },
  { key: 'accuracy', label: 'Accuracy', icon: 'target' },
  { key: 'help', label: 'Help', icon: 'help' },
]

// The primary destinations shown directly on the phone bottom bar; the rest live
// behind the "More" button (which opens the settings/nav drawer).
export const BOTTOM_TABS: Tab[] = NAV_TABS.filter((t) =>
  ['overview', 'my-team', 'planner', 'players'].includes(t.key),
)
