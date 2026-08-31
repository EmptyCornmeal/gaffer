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
  differentialIds, effectiveMultiplier, largestSwing, playerLive, playerTotal,
  scoreSquad, squadCurrent, squadProjected,
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

/**
 * Squad members the live payload carried no row for.
 *
 * C13. `playerLive` invents a row for anyone it has a fixture for but no live
 * data on, and that invented row holds the player's full PRE-MATCH projection
 * against zero confirmed points. Before kick-off that is exactly right and is
 * what lets a squad render at all. Once his match is running it is a guess
 * wearing a live score's clothes: a payload truncated at 70 minutes reports
 * "yet to kick off" for a man who may already have scored twice, and the squad
 * total absorbs his projection without a word.
 *
 * The invention is deliberately left alone — it is `gaffer.live`'s behaviour
 * too, and the parity tests hold both sides to it. What this adds is the means
 * to say so: the caller (`./source.ts`, mirroring `_mark_live_gaps` in
 * `pipeline.py`) names the gap rather than presenting the result as whole.
 *
 * An empty payload returns nothing: that is `no_live_data`, a state assemble
 * already reports on its own, not fifteen individually missing players.
 */
export function missingFromLive(
  squad: LiveSquadInput | null | undefined,
  livePayload: RawLivePayload | null | undefined,
): number[] {
  const have = new Set<number>()
  for (const el of livePayload?.elements ?? []) {
    if (typeof el?.id === 'number') have.add(el.id)
  }
  if (!squad || have.size === 0) return []
  const ours = [...(squad.starting ?? []), ...(squad.bench ?? [])]
  return [...new Set(ours)].filter((p) => !have.has(p)).sort((a, b) => a - b)
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

/** One published row of a rival's fifteen. See `rival_player_row` in
 *  src/gaffer/live.py for why it is not shaped like `playerDict`. */
export interface RivalPlayer {
  element: number
  name: string
  pos: string | null
  minutes: number
  confirmed: number
  provisional: number
  predicted: number
  multiplier: number
  /** `(confirmed + provisional) * multiplier` — this player's contribution to
   *  the manager's CURRENT score. `sum(product) - hits === gw_points`.
   *  `predicted` is deliberately NOT in it: it is a projection, and it is this
   *  player's own share, so multiply it by `multiplier` yourself to reach his
   *  contribution to the projected total. Two sums, kept looking like it. */
  product: number
  yet_to_play: boolean
}

/** One rival, scored, with the fifteen rows his total is made of. */
export interface RivalSquad {
  entry_id: number | null
  name: string
  provisional_position: number
  gw_points: number
  hits: number
  yet_to_play: number
  autosubs: SquadLive['autosubs']
  players: RivalPlayer[]
  /** Ids that separate you from this rival, spanning both squads. */
  differential: number[]
}

/**
 * One published row of a rival's fifteen, or null when he has no live state.
 *
 * Deliberately NOT `playerDict` with a name bolted on, which is what the
 * manager's own `players` block publishes. Seven rivals times fifteen is 105
 * rows in an artifact the browser re-reads whenever the live proxy cannot
 * answer, and measured against real GW2 data the full-fat row costs 258 bytes
 * against this one's 151. `played`, `finished` and `fixture_states` are the
 * fields dropped: all three are properties of the FOOTBALL rather than of the
 * manager, identical for every rival who owns the player, and none is needed to
 * answer either question this block exists for — what is his score, and who has
 * not kicked off yet.
 *
 * Mirrors `rival_player_row` in src/gaffer/live.py.
 */
function rivalPlayerRow(
  pid: number, squad: SquadLive, live: Map<number, PlayerLive>,
  names: Map<number, string> | undefined, positions: Map<number, string>,
): RivalPlayer | null {
  const st = live.get(pid)
  if (!st) return null
  const mult = effectiveMultiplier(pid, squad)
  return {
    element: pid,
    name: names?.get(pid) ?? String(pid),
    pos: positions.get(pid) ?? null,
    minutes: st.minutes,
    confirmed: st.confirmed,
    provisional: st.provisional,
    predicted: round2(st.predicted),
    multiplier: mult,
    product: round2((st.confirmed + st.provisional) * mult),
    yet_to_play: st.yetToPlay,
  }
}

/**
 * One rival, scored, with the fifteen rows his total is made of.
 *
 * All fifteen, not the scoring eleven: "how are they doing" is asked alongside
 * "which of their players have not kicked off", and a bench player who is yet to
 * play is one of them — his starter blanking is exactly how he comes on.
 * Publishing eleven would also have left this block's row count disagreeing with
 * `rivals[].yet_to_play`, which counts all fifteen. Under Bench Boost the same
 * fifteen rows simply all carry a multiplier, because `SquadLive.scoring`
 * already knows, so the chip needs no case here.
 *
 * Mirrors `rival_squad_dict` in src/gaffer/live.py.
 */
function rivalSquadDict(
  squad: SquadLive, mine: SquadLive, live: Map<number, PlayerLive>,
  names: Map<number, string> | undefined, positions: Map<number, string>,
  name: string, place: number,
): RivalSquad {
  return {
    entry_id: squad.entry_id,
    name,
    provisional_position: place,
    gw_points: round2(squadCurrent(squad)),
    hits: squad.hits,
    yet_to_play: squad.playersYetToPlay,
    autosubs: { ...squad.autosubs },
    players: [...squad.autosubs.xi, ...squad.autosubs.bench]
      .map((p) => rivalPlayerRow(p, squad, live, names, positions))
      .filter((r): r is RivalPlayer => r !== null),
    differential: differentialIds(mine, squad),
  }
}

export function assembleLive(input: AssembleInput): LiveState {
  const {
    gw, livePayload, fixturesPayload, squad, positions, teamOf, now,
    predictions, rivals = [], names, entryId = null, baseline = 0, hits = 0,
    activeChip = null, asOf = null,
  } = input

  // A5. A manager is a member of his own mini-league, so the rivals list he
  // arrives with contains him. `ranked` below prepends a synthetic "You" row,
  // which listed him twice — and the duplicate is a rival at distance ZERO from
  // himself, so `largestSwing` picked it as the closest, found `edge === 0` for
  // every player because both squads were his, and returned null every run.
  // `gatherRivals` in ./source.ts already drops him, which is the only reason
  // the browser escaped it; this is the defence that does not depend on the
  // fetch layer, and it mirrors `assemble` in src/gaffer/live.py exactly.
  const contenders = entryId == null
    ? rivals
    : rivals.filter((r) => r.entry_id !== entryId)
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

  const rivalStates = contenders
    .filter((r) => r.starting?.length)
    .map((r) => scoreSquad(r.starting, r.bench ?? [], positions, pl, {
      captain: r.captain, vice: r.vice,
      benchBoost: r.active_chip === 'bboost',
      tripleCaptain: r.active_chip === '3xc',
      entryId: r.entry_id, baseline: Number(r.total ?? 0) || 0,
      hits: Number(r.hits ?? 0) || 0,
    }))

  const swing = largestSwing(mine, rivalStates, pl, names)

  const rivalName = (eid: number | null): string =>
    contenders.find((x) => x.entry_id === eid)?.name || String(eid)
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
      name: rivalName(r.entry_id),
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
  const placeOf = new Map(ranked.map((row) => [row.entry_id, row.provisional_position]))
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
    // B5. Every rival's fifteen, with the multiplier that scores each of them
    // and the product it produces. `rivals` above is aggregates only, and no
    // table anywhere holds a rival's picks — so "how are they doing", which is
    // asked as often as "how am I doing", could only be answered with a number
    // nobody could check and no way to see whose players had not kicked off.
    // The detail was already in memory here and was thrown away one line before
    // the artifact.
    //
    // `sum(product) - hits === gw_points` holds for every entry, computed by the
    // same scorer over the same `pl` — never a second fetch, or the two halves
    // of the league could disagree about a goal that had just gone in.
    // Mirrors `assemble` in src/gaffer/live.py.
    rival_squads: rivalStates
      .map((r) => rivalSquadDict(
        r, mine, pl, names, positions, rivalName(r.entry_id),
        placeOf.get(r.entry_id) ?? 0,
      ))
      .sort((a, b) => a.provisional_position - b.provisional_position),
    // A6. The manager's own row, lifted out of the table so a consumer does not
    // have to hunt the league for `you`. Nothing wrote this key on either side,
    // and `mcp_server.publish` reads `live["me"]` (and `me.substitutions`, and
    // `me.yet_to_play`) directly — so all three published null while the numbers
    // sat two lines away in `rivals`. Mirrors `assemble` in src/gaffer/live.py.
    me: {
      entry_id: mine.entry_id,
      current: round2(mine.baseline + squadCurrent(mine)),
      projected: round2(mine.baseline + squadProjected(mine)),
      gw_points: round2(squadCurrent(mine)),
      yet_to_play: mine.playersYetToPlay,
      provisional_position: ranked.findIndex((r) => r.you) + 1,
      substitutions: { ...mine.autosubs },
    },
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
