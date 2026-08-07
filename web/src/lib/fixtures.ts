// The fixtures artifact, and the one place its shape is checked.
//
// `fixtures.json` is a map of team short-name to that team's fixture list —
// *plus* artifact metadata alongside it. T-29 added `"season": "2026-27"` at the
// top level, and the page was iterating `Object.values()` as though every value
// were a team record. It reached the season string, read `.fixtures` off it, and
// the whole route died with
//
//     TypeError: Cannot read properties of undefined (reading 'map')
//
// leaving the previous route's DOM on screen. Unit tests did not catch it
// because the hand-written fixture omitted the field that broke production.
//
// So the artifact is now typed as what it actually is, and one guard separates
// team records from metadata. Adding another top-level key cannot resurrect the
// bug: an unrecognised value is skipped, not dereferenced.

import type { TeamFixture } from './types'

/** One team's row in the ticker. */
export interface TeamFixtures {
  /** FPL short name — the artifact key, e.g. `ARS`. */
  short: string
  team: string
  fixtures: TeamFixture[]
}

/**
 * The published artifact: team records keyed by short name, plus metadata.
 *
 * Deliberately `unknown` rather than a union — every value has to go through
 * `isTeamFixtures` regardless, and a union would tempt a cast at the call site.
 */
export type FixturesArtifact = Record<string, unknown>

export type FixturesState =
  | { kind: 'ok'; season: string | null; teams: TeamFixtures[]; skipped: string[] }
  | { kind: 'unavailable'; reason: string }

/** Metadata keys the artifact carries beside the team records. */
const METADATA_KEYS = new Set(['season', 'generated_at', 'schema_version'])

function isFixture(v: unknown): v is TeamFixture {
  return !!v && typeof v === 'object' && typeof (v as TeamFixture).gw === 'number'
}

/** A value is a team record only if it carries a real fixture array. */
export function isTeamFixtures(v: unknown): v is { team: string; fixtures: TeamFixture[] } {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return false
  const rec = v as { team?: unknown; fixtures?: unknown }
  if (typeof rec.team !== 'string') return false
  if (!Array.isArray(rec.fixtures)) return false
  return rec.fixtures.every(isFixture)
}

/**
 * Split the artifact into team records and everything else.
 *
 * `skipped` is returned rather than discarded: a key that looks like a team but
 * fails the guard is a real problem worth surfacing, not something to swallow.
 */
export function parseFixtures(raw: unknown): FixturesState {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return {
      kind: 'unavailable',
      reason: `fixtures.json is ${Array.isArray(raw) ? 'an array' : typeof raw}, not an object of team records`,
    }
  }

  const teams: TeamFixtures[] = []
  const skipped: string[] = []
  for (const [short, value] of Object.entries(raw as FixturesArtifact)) {
    if (METADATA_KEYS.has(short) || typeof value === 'string' || typeof value === 'number') {
      continue // artifact metadata, not a team
    }
    if (isTeamFixtures(value)) {
      teams.push({ short, team: value.team, fixtures: value.fixtures })
    } else {
      skipped.push(short)
    }
  }

  if (teams.length === 0) {
    return {
      kind: 'unavailable',
      reason: skipped.length
        ? `no valid team fixture records — ${skipped.length} malformed entr${skipped.length === 1 ? 'y' : 'ies'} (${skipped.slice(0, 5).join(', ')})`
        : 'no team fixture records in fixtures.json',
    }
  }

  const season = (raw as { season?: unknown }).season
  return {
    kind: 'ok',
    season: typeof season === 'string' ? season : null,
    teams,
    skipped,
  }
}

/** Every gameweek any team plays in, ascending — so blanks and doubles line up. */
export function gameweeksIn(teams: TeamFixtures[]): number[] {
  return [...new Set(teams.flatMap((t) => t.fixtures.map((f) => f.gw)))].sort((a, b) => a - b)
}

/** Which difficulty axis the ticker is showing. */
export type FixtureMode = 'difficulty' | 'att' | 'def'

export const difficultyOf = (f: TeamFixture, mode: FixtureMode): number =>
  (f[mode] ?? f.difficulty) as number

export interface TickerRow {
  short: string
  team: string
  /** One entry per header gameweek: 0 fixtures = blank, 2+ = double. */
  cells: TeamFixture[][]
  played: number
  blanks: number
  doubles: number
  ease: number
}

/**
 * One row per team, sorted best fixture-run first.
 *
 * Blanks are penalised and doubles rewarded so the sort still means "best run"
 * once BGWs and DGWs appear — a team with four fixtures in six gameweeks should
 * not outrank one with six just because its average looks softer.
 */
export function buildRows(
  teams: TeamFixtures[], gws: number[], mode: FixtureMode,
): TickerRow[] {
  return teams
    .map((t) => {
      const byGw = new Map<number, TeamFixture[]>()
      for (const f of t.fixtures) {
        const list = byGw.get(f.gw) ?? []
        list.push(f)
        byGw.set(f.gw, list)
      }
      const cells = gws.map((gw) => byGw.get(gw) ?? [])
      const sumDiff = t.fixtures.reduce((s, f) => s + difficultyOf(f, mode), 0)
      const blanks = cells.filter((c) => c.length === 0).length
      return {
        short: t.short,
        team: t.team,
        cells,
        played: t.fixtures.length,
        blanks,
        doubles: cells.filter((c) => c.length > 1).length,
        ease: (sumDiff + blanks * 6) / (gws.length || 1),
      }
    })
    .sort((a, b) => a.ease - b.ease)
}

export interface RotationCell {
  short: string
  f: TeamFixture
}

export interface Rotation {
  cells: (RotationCell | null)[]
  ease: number
}

/**
 * For each gameweek, the easiest of the pinned teams' fixtures.
 *
 * This is the effective difficulty of rotating those teams through one squad
 * slot. `null` means every pinned team blanks that week.
 */
export function buildRotation(
  teams: TeamFixtures[], gws: number[], pinned: string[], mode: FixtureMode,
): Rotation | null {
  if (pinned.length < 2) return null
  const byShort = new Map(teams.map((t) => [t.short, t]))
  const cells = gws.map((gw) => {
    let best: RotationCell | null = null
    for (const short of pinned) {
      for (const f of (byShort.get(short)?.fixtures ?? []).filter((x) => x.gw === gw)) {
        if (!best || difficultyOf(f, mode) < difficultyOf(best.f, mode)) best = { short, f }
      }
    }
    return best
  })
  const rated = cells.filter((c): c is RotationCell => !!c)
  const ease = rated.length
    ? rated.reduce((s, c) => s + difficultyOf(c.f, mode), 0) / rated.length
    : 6
  return { cells, ease: Math.round(ease * 10) / 10 }
}
