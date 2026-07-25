export type Pos = 'GKP' | 'DEF' | 'MID' | 'FWD'

export interface Breakdown {
  appearance: number
  goals: number
  assists: number
  clean_sheet: number
  defcon: number
  bonus: number
}

export interface Tag {
  label: string
  kind: 'good' | 'info' | 'warn' | 'bad'
}

export interface XminsBadge {
  label: string
  kind: 'good' | 'warn' | 'bad'
  hint: string
}

export interface TeamFixture {
  gw: number
  opp: string
  home: boolean
  difficulty: number
}

export interface PricePred {
  dir: 'up' | 'down' | 'stable'
  momentum: number
}

export interface Player {
  id: number
  code: number | null
  name: string
  full_name: string
  team: string
  team_id: number
  team_code: number | null
  pos: Pos
  price: number
  owned_by: number
  net_transfers: number
  cost_change_event: number
  price_pred: PricePred
  status: string | null
  news: string
  set_pieces: string
  form: number
  xgi90: number
  defcon90: number
  next_gw_xp: number
  horizon_xp: number
  xp_window: number
  p_start: number
  confidence: number
  xmins_badge: XminsBadge
  rationale: string
  tags: Tag[]
  fixtures: TeamFixture[]
  breakdown: Breakdown
}

export interface Meta {
  current_gw: string
  gw_name: string
  deadline: string
  last_finished_gw: string
  squad_status: string
  entry_name: string | null
  manager_name: string | null
  overall_rank: string | null
  bank: string | null
  team_value: string | null
  active_chip: string | null
  model_version: string
  generated_at: string
  season: string
}

export interface RecPlayer {
  id: number
  code: number | null
  name: string
  team: string
  team_code?: number | null
  pos: Pos
  price: number
  next_gw_xp: number
  confidence: number
  rationale?: string
  tags?: Tag[]
  xmins_badge?: XminsBadge
}

export interface Recommendation {
  mode: string
  status: string
  formation: string
  squad_value: number
  xi_expected: number
  captain: RecPlayer
  vice: RecPlayer
  starting: RecPlayer[]
  bench: RecPlayer[]
  transfers_in: RecPlayer[]
  transfers_out: RecPlayer[]
  hits: number
  summary: string
}

export type Fixtures = Record<string, { team: string; fixtures: TeamFixture[] }>

export interface MyTeam {
  gw: number
  players: Player[]
}

export interface Verdict {
  briefing_md: string
  model: string | null
  source: string
  generated_at: string
}

export interface NewsItem {
  source: string
  title: string
  summary: string
  link: string
  published: string
}

export interface News {
  items: NewsItem[]
  digest_md: string
  source: string
  count: number
  generated_at: string
}

export interface Backtest {
  season: string
  n_predictions: number
  gameweeks: string
  mae: { gaffer: number; fpl_xp: number; naive: number }
  rank_corr: { gaffer: number; fpl_xp: number; naive: number }
  lift: { gaffer: { top: number; bottom: number }; fpl_xp: { top: number; bottom: number } }
  note: string
  generated_at: string
}
