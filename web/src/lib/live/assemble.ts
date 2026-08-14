// Assemble the whole live view from already-fetched payloads — a port of
// `assemble` in `src/gaffer/live.py`, emitting the identical `LiveState` shape
// so `Live.svelte` and `parseLive` render it unchanged.
//
// Deliberately pure: every argument is data. The caller does the I/O (see
// `./source.ts`), which is what lets every match state be reached from a
// recorded fixture instead of requiring a live Saturday afternoon.

import type { LiveState } from '../weekly'
import {
  bonusFinal, fixtureStates, fixtureSummary, LIVE_VERSION, provisionalBonus,
  round2, UNAVAILABLE_NO_GAMEWEEK, UNAVAILABLE_NO_LIVE_DATA, UNAVAILABLE_NO_SQUAD,
  UNAVAILABLE_NOT_STARTED, type FixtureState, type RawFixture,
} from './fixtures'
import {
  largestSwing, playerLive, playerTotal, scoreSquad, squadCurrent, squadProjected,
  type PlayerLive, type RawLivePayload, type SquadLive,
} from './scoring'

export interface LiveSquadInput {
  starting: number[]
  bench?: number[]
  captain?: number | null
  vice?: number | null
}

export interface LiveRivalInput extends LiveSquadInput {
  entry_id: number
  name?: string
  total?: number
  hits?: number
  active_chip?: string | null
}

export interface AssembleInput {
  gw: number
  livePayload: RawLivePayload | null
  fixturesPayload: RawFixture[] | null
  squad: LiveSquadInput | null
  positions: Map<number, string>
  teamOf: Map<number, number>
  now: Date
  predictions?: Map<number, number>
  rivals?: LiveRivalInput[]
  names?: Map<number, string>
  entryId?: number | null
  baseline?: number
  hits?: number
  activeChip?: string | null
  asOf?: string | null
}

function fixtureDict(s: FixtureState) {
  return {
    id: s.id, event: s.event, team_h: s.team_h, team_a: s.team_a,
    state: s.state, minutes: s.minutes, kickoff: s.kickoff,
    bonus_final: bonusFinal(s),
  }
}

function playerDict(p: PlayerLive) {
  return {
    id: p.id, minutes: p.minutes, confirmed: p.confirmed,
    provisional: p.provisional, predicted: round2(p.predicted),
    total: round2(playerTotal(p)), played: p.played, finished: p.finished,
    yet_to_play: p.yetToPlay, fixture_states: p.states,
  }
}

function squadDict(s: SquadLive) {
  return {
    entry_id: s.entry_id,
    confirmed: s.confirmed,
    provisional_bonus: s.provisional,
    predicted_remaining: round2(s.predicted),
    current: round2(squadCurrent(s)),
    projected: round2(squadProjected(s)),
    bench_points: s.benchPoints,
    players_played: s.playersPlayed,
    players_yet_to_play: s.playersYetToPlay,
    hits: s.hits,
    season_total_before: s.baseline,
    season_total_projected: round2(s.baseline + squadProjected(s)),
    autosubs: { ...s.autosubs },
  }
}

export function assembleLive(input: AssembleInput): LiveState {
  const {
    gw, livePayload, fixturesPayload, squad, positions, teamOf, now,
    predictions, rivals = [], names, entryId = null, baseline = 0, hits = 0,
    activeChip = null, asOf = null,
  } = input

  const states = fixtureStates(fixturesPayload, gw, now)
  const base = {
    live_version: LIVE_VERSION,
    gameweek: gw,
    as_of: asOf,
    fixtures: [...states.values()]
      .sort((a, b) => a.id - b.id)
      .map(fixtureDict),
    fixture_summary: fixtureSummary(states),
  }

  if (states.size === 0) {
    return {
      ...base, available: false, unavailable_reason: UNAVAILABLE_NO_GAMEWEEK,
      note: `no fixtures are scheduled for gameweek ${gw}`,
    } as unknown as LiveState
  }
  if (![...states.values()].some((s) => s.started)) {
    return {
      ...base, available: false, unavailable_reason: UNAVAILABLE_NOT_STARTED,
      note: 'no match in this gameweek has kicked off yet',
    } as unknown as LiveState
  }
  if (!squad || !squad.starting?.length) {
    return {
      ...base, available: false, unavailable_reason: UNAVAILABLE_NO_SQUAD,
      note: 'your squad is not readable, so there is nothing to score',
    } as unknown as LiveState
  }
  if (!livePayload?.elements?.length) {
    return {
      ...base, available: false, unavailable_reason: UNAVAILABLE_NO_LIVE_DATA,
      note: 'matches have started but the live endpoint is not serving player data yet',
    } as unknown as LiveState
  }

  const prov = provisionalBonus(fixturesPayload, states)
  const pl = playerLive(livePayload, states, prov, teamOf, predictions)

  const mine = scoreSquad(squad.starting, squad.bench ?? [], positions, pl, {
    captain: squad.captain, vice: squad.vice,
    benchBoost: activeChip === 'bboost',
    tripleCaptain: activeChip === '3xc',
    entryId, baseline, hits,
  })

  const rivalStates = rivals
    .filter((r) => r.starting?.length)
    .map((r) => scoreSquad(r.starting, r.bench ?? [], positions, pl, {
      captain: r.captain, vice: r.vice,
      benchBoost: r.active_chip === 'bboost',
      tripleCaptain: r.active_chip === '3xc',
      entryId: r.entry_id, baseline: Number(r.total ?? 0) || 0,
      hits: Number(r.hits ?? 0) || 0,
    }))

  const swing = largestSwing(mine, rivalStates, pl, names)

  const ranked = [
    {
      entry_id: mine.entry_id, name: 'You', you: true,
      current: round2(mine.baseline + squadCurrent(mine)),
      projected: round2(mine.baseline + squadProjected(mine)),
      gw_points: round2(squadCurrent(mine)),
      yet_to_play: mine.playersYetToPlay,
    },
    ...rivalStates.map((r) => ({
      entry_id: r.entry_id,
      name: rivals.find((x) => x.entry_id === r.entry_id)?.name || String(r.entry_id),
      you: false,
      current: round2(r.baseline + squadCurrent(r)),
      projected: round2(r.baseline + squadProjected(r)),
      gw_points: round2(squadCurrent(r)),
      yet_to_play: r.playersYetToPlay,
    })),
  ]
    // Python sorts by -projected; its sort is stable, and so is JS's.
    .sort((a, b) => b.projected - a.projected)
    .map((row, i) => ({ ...row, provisional_position: i + 1 }))

  return {
    ...base,
    available: true,
    unavailable_reason: null,
    active_chip: activeChip,
    squad: squadDict(mine),
    players: [...mine.autosubs.xi, ...mine.autosubs.bench]
      .filter((p) => pl.has(p))
      .map((p) => ({
        ...playerDict(pl.get(p) as PlayerLive),
        name: names?.get(p) ?? String(p),
        pos: positions.get(p) ?? null,
        in_xi: mine.autosubs.xi.includes(p),
        is_captain: p === mine.autosubs.captain,
      })),
    rivals: ranked,
    largest_swing: swing,
    separation: {
      confirmed: mine.confirmed,
      provisional_bonus: mine.provisional,
      predicted_remaining: round2(mine.predicted),
      note: 'Provisional bonus is computed from live BPS and changes until each '
        + 'match is finalised. It is not confirmed.',
    },
  } as unknown as LiveState
}
