import type {
  Backtest, Fixtures, Meta, MyTeam, News, Player, Recommendation, Verdict,
} from './types'

const BASE = import.meta.env.BASE_URL // './' in prod, '/' in dev

async function load<T>(file: string): Promise<T> {
  const res = await fetch(`${BASE}data/${file}?t=${Date.now()}`)
  if (!res.ok) throw new Error(`Failed to load ${file}: ${res.status}`)
  return res.json() as Promise<T>
}

export interface Bundle {
  meta: Meta
  players: Player[]
  fixtures: Fixtures
  recommendation: Recommendation
  myTeam: MyTeam | null
  verdict: Verdict | null
  news: News | null
  backtest: Backtest | null
}

export async function loadBundle(): Promise<Bundle> {
  const [meta, players, fixtures, recommendation, myTeam, verdict, news, backtest] =
    await Promise.all([
      load<Meta>('meta.json'),
      load<Player[]>('players.json'),
      load<Fixtures>('fixtures.json'),
      load<Recommendation>('recommendation.json'),
      load<MyTeam | null>('my_team.json').catch(() => null),
      load<Verdict | null>('verdict.json').catch(() => null),
      load<News | null>('news.json').catch(() => null),
      load<Backtest | null>('backtest.json').catch(() => null),
    ])
  return { meta, players, fixtures, recommendation, myTeam, verdict, news, backtest }
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

export const posColor: Record<string, string> = {
  GKP: 'text-amber',
  DEF: 'text-cyan',
  MID: 'text-green',
  FWD: 'text-magenta',
}
