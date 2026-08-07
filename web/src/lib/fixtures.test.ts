import { describe, expect, it } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  buildRotation, buildRows, gameweeksIn, isTeamFixtures, parseFixtures,
} from './fixtures'

// The production Fixtures route died with
//   TypeError: Cannot read properties of undefined (reading 'map')
// because `fixtures.json` carries `"season": "2026-27"` beside the twenty team
// records, and the page iterated every top-level value as though it were a team.
//
// Unit tests did not catch it: the hand-written fixture omitted the field that
// broke production. So the first test here is the REAL published artifact, and
// the hand-written ones only cover what it cannot.

const DEPLOYED = join(process.cwd(), 'public', 'data', 'fixtures.json')

const team = (short: string, gws: number[]) => ({
  team: `${short} FC`,
  fixtures: gws.map((gw) => ({ gw, opp: 'XXX', home: true, difficulty: 3, att: 3, def: 3 })),
})

/** Twenty teams plus the metadata key, exactly as the pipeline writes it. */
function deployedShape() {
  const out: Record<string, unknown> = { season: '2026-27' }
  for (let i = 0; i < 20; i++) out[`T${i}`] = team(`T${i}`, [1, 2, 3, 4, 5, 6])
  return out
}

describe('the real published artifact', () => {
  it.runIf(existsSync(DEPLOYED))('parses without touching the season string', () => {
    const raw = JSON.parse(readFileSync(DEPLOYED, 'utf8'))
    // The exact expression that crashed production, for the record.
    expect(() =>
      Object.values(raw).flatMap((v) => (v as { fixtures: { gw: number }[] }).fixtures.map((f) => f.gw)),
    ).toThrow(TypeError)

    const state = parseFixtures(raw)
    expect(state.kind).toBe('ok')
    if (state.kind !== 'ok') return
    expect(state.teams).toHaveLength(20)
    expect(state.season).toBe(raw.season)
    expect(state.skipped).toEqual([])
    expect(state.teams.map((t) => t.short)).not.toContain('season')
    expect(gameweeksIn(state.teams).length).toBeGreaterThan(0)
  })
})

describe('parseFixtures — deployed shape', () => {
  const raw = deployedShape()

  it('does not treat season metadata as a team', () => {
    const state = parseFixtures(raw)
    expect(state.kind).toBe('ok')
    if (state.kind !== 'ok') return
    expect(state.teams).toHaveLength(20)
    expect(state.teams.map((t) => t.short)).not.toContain('season')
  })

  it('preserves the season stamp', () => {
    const state = parseFixtures(raw)
    if (state.kind !== 'ok') throw new Error('expected ok')
    expect(state.season).toBe('2026-27')
  })

  it('reports every gameweek any team plays, ascending', () => {
    const state = parseFixtures(raw)
    if (state.kind !== 'ok') throw new Error('expected ok')
    expect(gameweeksIn(state.teams)).toEqual([1, 2, 3, 4, 5, 6])
  })

  it('skips any future metadata key without dereferencing it', () => {
    for (const extra of [
      { generated_at: '2026-08-07T00:00:00Z' },
      { schema_version: 3 },
      { some_future_key: 'a string' },
      { another: 42 },
    ]) {
      const state = parseFixtures({ ...raw, ...extra })
      expect(state.kind).toBe('ok')
      if (state.kind !== 'ok') continue
      expect(state.teams).toHaveLength(20)
    }
  })

  it('records a malformed team rather than crashing on it', () => {
    const state = parseFixtures({ ...raw, BAD: { team: 'Bad', fixtures: 'not an array' } })
    expect(state.kind).toBe('ok')
    if (state.kind !== 'ok') return
    expect(state.teams).toHaveLength(20)
    expect(state.skipped).toEqual(['BAD'])
  })

  it('sorts gameweeks numerically even when the artifact does not', () => {
    const state = parseFixtures({ season: '2026-27', A: team('A', [10, 2, 1]) })
    if (state.kind !== 'ok') throw new Error('expected ok')
    expect(gameweeksIn(state.teams)).toEqual([1, 2, 10])
  })
})

describe('parseFixtures — unavailable states', () => {
  it('reports zero valid team records rather than an empty grid', () => {
    const state = parseFixtures({ season: '2026-27' })
    expect(state.kind).toBe('unavailable')
    if (state.kind !== 'unavailable') return
    expect(state.reason).toContain('no team fixture records')
  })

  it('names the malformed entries when every team is broken', () => {
    const state = parseFixtures({ season: '2026-27', ARS: { team: 'Arsenal' }, CHE: {} })
    expect(state.kind).toBe('unavailable')
    if (state.kind !== 'unavailable') return
    expect(state.reason).toContain('ARS')
    expect(state.reason).toContain('malformed')
  })

  it.each([null, undefined, 'a string', 42, [1, 2, 3]])(
    'refuses a non-object artifact (%s)',
    (raw) => {
      const state = parseFixtures(raw)
      expect(state.kind).toBe('unavailable')
      if (state.kind !== 'unavailable') return
      expect(state.reason).toContain('not an object')
    },
  )
})

describe('isTeamFixtures', () => {
  it.each([
    ['the season string', '2026-27', false],
    ['a number', 3, false],
    ['null', null, false],
    ['an array', [], false],
    ['a record with no fixtures', { team: 'Arsenal' }, false],
    ['a record whose fixtures is not an array', { team: 'A', fixtures: {} }, false],
    ['a record with a non-fixture entry', { team: 'A', fixtures: [{ nope: 1 }] }, false],
    ['a record with no team name', { fixtures: [] }, false],
    ['an empty but valid fixture list (a fully blanked team)', { team: 'A', fixtures: [] }, true],
    ['a real record', { team: 'A', fixtures: [{ gw: 1, opp: 'B', home: true, difficulty: 3 }] }, true],
  ])('%s -> %s', (_label, value, expected) => {
    expect(isTeamFixtures(value)).toBe(expected)
  })
})

describe('the ticker, driven by the deployed shape', () => {
  const state = parseFixtures(deployedShape())
  const teams = state.kind === 'ok' ? state.teams : []
  const gws = gameweeksIn(teams)

  it('renders a row for all twenty teams', () => {
    expect(buildRows(teams, gws, 'difficulty')).toHaveLength(20)
  })

  it('gives every row one cell per gameweek header', () => {
    for (const row of buildRows(teams, gws, 'difficulty')) {
      expect(row.cells).toHaveLength(gws.length)
    }
  })

  it('switches mode without losing a team or a gameweek', () => {
    for (const mode of ['difficulty', 'att', 'def'] as const) {
      const rows = buildRows(teams, gws, mode)
      expect(rows).toHaveLength(20)
      expect(rows[0].cells).toHaveLength(6)
    }
  })

  it('reorders on the mode that is showing', () => {
    // Two teams identical on `difficulty` but opposite on `att`.
    const a = { team: 'A', fixtures: [{ gw: 1, opp: 'X', home: true, difficulty: 3, att: 1, def: 5 }] }
    const b = { team: 'B', fixtures: [{ gw: 1, opp: 'Y', home: true, difficulty: 3, att: 5, def: 1 }] }
    const s = parseFixtures({ season: '2026-27', A: a, B: b })
    if (s.kind !== 'ok') throw new Error('expected ok')
    const g = gameweeksIn(s.teams)
    expect(buildRows(s.teams, g, 'att')[0].short).toBe('A')
    expect(buildRows(s.teams, g, 'def')[0].short).toBe('B')
  })

  it('counts blanks and doubles per row', () => {
    const s = parseFixtures({
      season: '2026-27',
      DGW: { team: 'D', fixtures: [
        { gw: 1, opp: 'X', home: true, difficulty: 2 },
        { gw: 1, opp: 'Y', home: false, difficulty: 3 },
      ] },
      BGW: { team: 'B', fixtures: [{ gw: 2, opp: 'Z', home: true, difficulty: 2 }] },
    })
    if (s.kind !== 'ok') throw new Error('expected ok')
    const g = gameweeksIn(s.teams)
    const rows = buildRows(s.teams, g, 'difficulty')
    expect(rows.find((r) => r.short === 'DGW')!.doubles).toBe(1)
    expect(rows.find((r) => r.short === 'DGW')!.blanks).toBe(1)
    expect(rows.find((r) => r.short === 'BGW')!.blanks).toBe(1)
  })

  it('produces no rotation row until two teams are pinned', () => {
    expect(buildRotation(teams, gws, [], 'difficulty')).toBeNull()
    expect(buildRotation(teams, gws, ['T0'], 'difficulty')).toBeNull()
  })

  it('produces a rotation row when two teams are pinned', () => {
    const rot = buildRotation(teams, gws, ['T0', 'T1'], 'difficulty')
    expect(rot).not.toBeNull()
    expect(rot!.cells).toHaveLength(gws.length)
    expect(rot!.cells.every((c) => c === null || ['T0', 'T1'].includes(c.short))).toBe(true)
  })

  it('picks the easier of the two pinned fixtures each gameweek', () => {
    const s = parseFixtures({
      season: '2026-27',
      EASY: { team: 'E', fixtures: [{ gw: 1, opp: 'X', home: true, difficulty: 1 }] },
      HARD: { team: 'H', fixtures: [{ gw: 1, opp: 'Y', home: true, difficulty: 5 }] },
    })
    if (s.kind !== 'ok') throw new Error('expected ok')
    const rot = buildRotation(s.teams, gameweeksIn(s.teams), ['EASY', 'HARD'], 'difficulty')!
    expect(rot.cells[0]!.short).toBe('EASY')
    expect(rot.ease).toBe(1)
  })

  it('leaves a gameweek null when both pinned teams blank', () => {
    const s = parseFixtures({
      season: '2026-27',
      A: { team: 'A', fixtures: [{ gw: 1, opp: 'X', home: true, difficulty: 2 }] },
      B: { team: 'B', fixtures: [{ gw: 2, opp: 'Y', home: true, difficulty: 2 }] },
      C: { team: 'C', fixtures: [{ gw: 3, opp: 'Z', home: true, difficulty: 2 }] },
    })
    if (s.kind !== 'ok') throw new Error('expected ok')
    const rot = buildRotation(s.teams, gameweeksIn(s.teams), ['A', 'B'], 'difficulty')!
    expect(rot.cells.map((c) => c?.short ?? null)).toEqual(['A', 'B', null])
  })

  it.runIf(existsSync(DEPLOYED))('drives the real artifact end to end', () => {
    const s = parseFixtures(JSON.parse(readFileSync(DEPLOYED, 'utf8')))
    if (s.kind !== 'ok') throw new Error('expected ok')
    const g = gameweeksIn(s.teams)
    expect(g).toHaveLength(6)
    const rows = buildRows(s.teams, g, 'difficulty')
    expect(rows).toHaveLength(20)
    expect(rows.every((r) => r.cells.length === 6)).toBe(true)
    const two = [rows[0].short, rows[1].short]
    const rot = buildRotation(s.teams, g, two, 'difficulty')
    expect(rot!.cells).toHaveLength(6)
  })
})
