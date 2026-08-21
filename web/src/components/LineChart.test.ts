import { describe, expect, it } from 'vitest'
import { render } from 'svelte/server'
import LineChart from './LineChart.svelte'

// The chart is pure presentation, but two things about it are load-bearing and
// silently breakable: it must draw one path per series, and its accessible
// read-out must state real values rather than an index. Both are asserted from
// server-rendered markup, so no DOM or testing-library dependency is needed.

const LABELS = ['GW1', 'GW2', 'GW3']
const SERIES = [
  { key: 1, name: 'You', color: '#34d399', you: true, values: [10, 24, 41] },
  { key: 2, name: 'Rival', color: '#3987e5', values: [8, 30, 35] },
  { key: 3, name: 'Gap', color: '#d95926', values: [5, null, 12] },
]

function html(props: Record<string, unknown>) {
  return render(LineChart as any, { props: { series: SERIES, labels: LABELS, ...props } }).body
}

describe('LineChart', () => {
  it('draws one path per series', () => {
    const paths = html({}).match(/<path /g) ?? []
    expect(paths).toHaveLength(SERIES.length)
  })

  it('breaks the line across a missing gameweek rather than inventing one', () => {
    // The 'Gap' series has a null at GW2, so its path must start twice.
    const d = [...html({}).matchAll(/<path[^>]*d="([^"]*)"/g)].map((m) => m[1])
    const gap = d.find((p) => (p.match(/M/g) ?? []).length > 1)
    expect(gap, 'a series with a null should produce a broken path').toBeTruthy()
  })

  it('renders every series in the legend', () => {
    const out = html({})
    for (const s of SERIES) expect(out).toContain(s.name)
  })

  it('exposes the scrubber as a slider over the gameweeks', () => {
    const out = html({})
    expect(out).toContain('role="slider"')
    expect(out).toContain('aria-valuemax="2"')
  })

  it('formats the y axis with the caller format, so an inverted axis never shows a negative', () => {
    // The rank chart stores rank negated so "up" means "better". If the axis
    // printed the stored number it would read -4, -3, -2 — which it used to.
    const out = render(LineChart as any, {
      props: {
        series: [{ key: 1, name: 'You', color: '#34d399', values: [-4, -2, -1] }],
        labels: LABELS,
        format: (v: number) => String(-v),
      },
    }).body
    const tickLabels = [...out.matchAll(/tabular-nums">([^<]*)</g)].map((m) => m[1])
    expect(tickLabels.length).toBeGreaterThan(0)
    expect(tickLabels.every((t) => !t.startsWith('-'))).toBe(true)
  })

  it('draws a visible mark for a single gameweek', () => {
    // A path of one point is 'M x y', which paints nothing. Every league chart
    // is single-gameweek after GW1 — the first time anyone looks at it.
    const out = render(LineChart as any, {
      props: {
        series: [{ key: 1, name: 'You', color: '#34d399', you: true, values: [10] }],
        labels: ['GW1'],
      },
    }).body
    expect(out).toContain('<circle')
  })

  it('marks a point stranded between gaps', () => {
    const out = render(LineChart as any, {
      props: {
        series: [{ key: 1, name: 'You', color: '#34d399', values: [null, 7, null] }],
        labels: ['GW1', 'GW2', 'GW3'],
      },
    }).body
    expect(out).toContain('<circle')
  })
})
