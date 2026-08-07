import type { FixturesArtifact } from './fixtures'
import type {
  Meta, MyTeam, News, Player, Recommendation, TransferPlan, Verdict,
} from './types'

const BASE = import.meta.env.BASE_URL // './' in prod, '/' in dev

// One in-flight request per artifact. Two components mounting at once used to
// mean two identical downloads of a 2 MB players.json; sharing the promise makes
// that impossible without a cache that could go stale.
const inflight = new Map<string, Promise<unknown>>()
const settled = new Map<string, unknown>()

/** Artifacts whose content is fixed for a run and safe to reuse in-session. */
const IMMUTABLE_FOR_RUN = new Set([
  'players.json', 'fixtures.json', 'recommendation.json', 'plan.json',
  'my_team.json', 'strategy.json', 'decision.json', 'backtest.json',
  'verdict.json', 'news.json', 'review.json', 'notifications.json',
])

async function load<T>(file: string, { fresh = false } = {}): Promise<T> {
  if (!fresh && settled.has(file)) return settled.get(file) as T
  const existing = inflight.get(file)
  if (existing && !fresh) return existing as Promise<T>

  const p = (async () => {
    const res = await fetch(`${BASE}data/${file}?t=${Date.now()}`)
    if (!res.ok) throw new Error(`Failed to load ${file}: ${res.status}`)
    const v = (await res.json()) as T
    // live.json is never cached: it is only valid for the moment it was fetched.
    if (IMMUTABLE_FOR_RUN.has(file)) settled.set(file, v)
    return v
  })()
  inflight.set(file, p)
  try {
    return await p
  } finally {
    inflight.delete(file)
  }
}

async function optional<T>(file: string): Promise<T | null> {
  try {
    return await load<T>(file)
  } catch {
    return null
  }
}

/** Drop every cached artifact — used by an explicit user refresh. */
export function invalidate(): void {
  settled.clear()
  inflight.clear()
}

/**
 * The shell bundle: enough to render the topbar, freshness, gameweek, deadline
 * and build mode, and nothing else.
 *
 * `meta.json` is ~1 kB; `players.json` is megabytes. Loading them together meant
 * a blank screen until the largest artifact arrived, on a phone, on 4G. This
 * resolves first so the shell is honest within one request.
 */
export interface Shell {
  meta: Meta
}

export async function loadShell(): Promise<Shell> {
  return { meta: await load<Meta>('meta.json') }
}

export interface Bundle {
  meta: Meta
  players: Player[]
  fixtures: FixturesArtifact
  recommendation: Recommendation
  myTeam: MyTeam | null
  plan: TransferPlan | null
  verdict: Verdict | null
  news: News | null
  backtest: unknown
  strategy: unknown
  decision: unknown
  review: unknown
  notifications: unknown
}

/**
 * The full bundle. Required artifacts are awaited; optional ones resolve to null
 * rather than failing the app — a build without a review is a normal state, not
 * an error.
 */
export async function loadBundle(): Promise<Bundle> {
  const [meta, players, fixtures, recommendation] = await Promise.all([
    load<Meta>('meta.json'),
    load<Player[]>('players.json'),
    load<FixturesArtifact>('fixtures.json'),
    load<Recommendation>('recommendation.json'),
  ])
  const [myTeam, plan, verdict, news, backtest, strategy, decision, review, notifications] =
    await Promise.all([
      optional<MyTeam>('my_team.json'),
      optional<TransferPlan>('plan.json'),
      optional<Verdict>('verdict.json'),
      optional<News>('news.json'),
      optional<unknown>('backtest.json'),
      optional<unknown>('strategy.json'),
      optional<unknown>('decision.json'),
      optional<unknown>('review.json'),
      optional<unknown>('notifications.json'),
    ])
  // T-29: FPL reuses element ids, so an artifact left over from last season
  // parses perfectly and renders as this week's advice. Every artifact carries
  // the season it describes; anything that disagrees with `meta` is dropped
  // rather than shown. `backtest.json` is the deliberate exception — it reports
  // a historical season by design.
  const drop = staleSeasons(meta?.season, { decision, review, notifications, strategy, plan })
  return {
    meta, players, fixtures, recommendation, myTeam,
    plan: drop.has('plan') ? null : plan,
    verdict, news, backtest,
    strategy: drop.has('strategy') ? null : strategy,
    decision: drop.has('decision') ? null : decision,
    review: drop.has('review') ? null : review,
    notifications: drop.has('notifications') ? null : notifications,
  }
}

/**
 * Names of artifacts whose declared season is not `declared`.
 *
 * An artifact with no season at all is kept: an older build is allowed to be
 * silent, and refusing to render everything on a missing field would be worse
 * than the problem. An artifact that names a DIFFERENT season is not silent —
 * it is wrong, and it is dropped.
 */
export function staleSeasons(
  declared: string | undefined | null,
  artifacts: Record<string, unknown>,
): Set<string> {
  const out = new Set<string>()
  if (!declared) return out
  for (const [name, blob] of Object.entries(artifacts)) {
    if (blob == null || typeof blob !== 'object') continue
    const got = (blob as { season?: unknown }).season
    if (typeof got === 'string' && got !== declared) out.add(name)
  }
  return out
}

/** The published live snapshot. Never cached — it is only true when fetched. */
export async function loadLiveSnapshot(): Promise<unknown | null> {
  try {
    return await load<unknown>('live.json', { fresh: true })
  } catch {
    return null
  }
}

export function countdown(deadline: string, nowMs: number = Date.now()): string {
  if (!deadline) return ''
  const ms = new Date(deadline).getTime() - nowMs
  if (ms <= 0) return 'deadline passed'
  const d = Math.floor(ms / 86400000)
  const h = Math.floor((ms % 86400000) / 3600000)
  const m = Math.floor((ms % 3600000) / 60000)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}
