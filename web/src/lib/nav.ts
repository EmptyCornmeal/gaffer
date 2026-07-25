// Single source of truth for the app's pages, so the topbar, the mobile drawer,
// and the mobile bottom bar never drift out of sync.

export type Tab = { key: string; label: string; icon: string }

export const NAV_TABS: Tab[] = [
  { key: 'overview', label: 'Overview', icon: '⚡' },
  { key: 'my-team', label: 'My Team', icon: '👕' },
  { key: 'planner', label: 'Planner', icon: '🧭' },
  { key: 'players', label: 'Players', icon: '📋' },
  { key: 'fixtures', label: 'Fixtures', icon: '🗓️' },
  { key: 'chips', label: 'Chips', icon: '🎴' },
  { key: 'league', label: 'League', icon: '🏆' },
  { key: 'news', label: 'News', icon: '📰' },
  { key: 'accuracy', label: 'Accuracy', icon: '🎯' },
  { key: 'help', label: 'Help', icon: '❓' },
]

// The primary destinations shown directly on the phone bottom bar; the rest live
// behind the "More" button (which opens the settings/nav drawer).
export const BOTTOM_TABS: Tab[] = NAV_TABS.filter((t) =>
  ['overview', 'my-team', 'planner', 'players'].includes(t.key),
)
