<script lang="ts">
  // Lightweight dependency-free SVG scatter (CSP-safe, theme-aware).
  // Built for the ownership-vs-xP view: quadrants split differentials / template
  // / traps / fringe, dots coloured by position, click a dot to open the player.
  import type { Pos } from '../lib/types'

  interface Pt {
    id: number
    x: number
    y: number
    label: string
    pos: Pos
  }
  let {
    points,
    xLabel = 'Ownership %',
    yLabel = 'Projected pts',
    xThreshold,
    yThreshold,
    quadrants = true,
    onpick,
  }: {
    points: Pt[]
    xLabel?: string
    yLabel?: string
    xThreshold?: number
    yThreshold?: number
    quadrants?: boolean
    onpick?: (id: number) => void
  } = $props()

  const POS_COLOR: Record<Pos, string> = {
    GKP: '#a78bfa',
    DEF: '#34d399',
    MID: '#60a5fa',
    FWD: '#f472b6',
  }

  // viewBox space; CSS scales it responsively.
  const W = 640
  const H = 420
  const M = { t: 16, r: 16, b: 40, l: 44 }
  const iw = W - M.l - M.r
  const ih = H - M.t - M.b

  // Round "nice" ceiling + tick step so axes read 0/20/40/60 not 15.6/31.3/…
  function niceStep(max: number, target = 5): number {
    const raw = max / target
    const mag = 10 ** Math.floor(Math.log10(raw))
    const norm = raw / mag
    const step = norm >= 5 ? 5 : norm >= 2 ? 2 : 1
    return step * mag
  }
  const xStep = $derived(niceStep(Math.max(1, ...points.map((p) => p.x))))
  const yStep = $derived(niceStep(Math.max(1, ...points.map((p) => p.y))))
  const xMax = $derived(Math.ceil((Math.max(1, ...points.map((p) => p.x)) * 1.05) / xStep) * xStep)
  const yMax = $derived(Math.ceil((Math.max(1, ...points.map((p) => p.y)) * 1.05) / yStep) * yStep)
  const xThr = $derived(xThreshold ?? xMax / 3)
  const yThr = $derived(
    yThreshold ??
      (points.length ? points.reduce((s, p) => s + p.y, 0) / points.length : yMax / 2),
  )

  const sx = (x: number) => M.l + (x / xMax) * iw
  const sy = (y: number) => M.t + ih - (y / yMax) * ih

  let hover = $state<Pt | null>(null)

  const xTicks = $derived(Array.from({ length: Math.floor(xMax / xStep) + 1 }, (_, i) => i * xStep))
  const yTicks = $derived(Array.from({ length: Math.floor(yMax / yStep) + 1 }, (_, i) => i * yStep))

  // Directly label the standout dots so the chart is readable, not just a blob:
  // the highest-projection picks + the strongest low-owned differentials.
  const labelled = $derived.by(() => {
    const byY = [...points].sort((a, b) => b.y - a.y).slice(0, 6)
    const diffs = [...points]
      .filter((p) => p.x < xThr)
      .sort((a, b) => b.y - a.y)
      .slice(0, 4)
    const ids = new Set<number>()
    return [...byY, ...diffs].filter((p) => (ids.has(p.id) ? false : ids.add(p.id)))
  })

  // Every name used to be pinned directly above its dot, so neighbours
  // overprinted into an unreadable smear (Gabriel over Mbeumo, Tarkowski over
  // Saka). Each name now takes the first anchor whose box clears everything
  // already placed — earlier names and the quadrant captions — and a name with
  // nowhere left to sit is dropped. Dots are drawn from `points` regardless, so
  // losing a label never costs a click target.
  const CHAR_W = 4.7 // ≈ advance width at 9px semibold; only needs to reserve space
  const LINE_H = 11
  type Box = [x1: number, y1: number, x2: number, y2: number]
  type Anchor = 'middle' | 'start' | 'end'
  type Placed = { p: Pt; x: number; y: number; anchor: Anchor }
  // SVG text is positioned by baseline, so the box hangs above `y`.
  function boxOf(x: number, y: number, w: number, anchor: Anchor): Box {
    const x1 = anchor === 'middle' ? x - w / 2 : anchor === 'start' ? x : x - w
    return [x1, y - LINE_H + 2, x1 + w, y + 2]
  }
  const overlaps = (a: Box, b: Box) => a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1]

  const placed = $derived.by(() => {
    const taken: Box[] = []
    if (quadrants) {
      // 10px bold uppercase with tracking — wider per char than the dot labels.
      const cap = (t: string, x: number, y: number, a: Anchor) =>
        boxOf(x, y, t.length * 6.2 + 4, a)
      taken.push(
        cap('Differentials', M.l + 6, M.t + 13, 'start'),
        cap('Template', M.l + iw - 6, M.t + 13, 'end'),
        cap('Traps', M.l + iw - 6, M.t + ih - 6, 'end'),
        cap('Fringe', M.l + 6, M.t + ih - 6, 'start'),
      )
    }
    const out: Placed[] = []
    for (const p of labelled) {
      const w = p.label.length * CHAR_W + 4
      const cx = sx(p.x)
      const cy = sy(p.y)
      // Nearest-first: above, below, right, left, then the two diagonals — a
      // label only drifts as far from its dot as it has to.
      const tries: Placed[] = [
        { p, x: cx, y: cy - 7, anchor: 'middle' },
        { p, x: cx, y: cy + 15, anchor: 'middle' },
        { p, x: cx + 7, y: cy + 3, anchor: 'start' },
        { p, x: cx - 7, y: cy + 3, anchor: 'end' },
        { p, x: cx + 6, y: cy - 6, anchor: 'start' },
        { p, x: cx - 6, y: cy + 14, anchor: 'end' },
      ]
      for (const t of tries) {
        const b = boxOf(t.x, t.y, w, t.anchor)
        const inPlot =
          b[0] >= M.l - 2 && b[2] <= M.l + iw + 2 && b[1] >= M.t && b[3] <= M.t + ih
        if (!inPlot || taken.some((o) => overlaps(b, o))) continue
        taken.push(b)
        out.push(t)
        break
      }
    }
    return out
  })
</script>

<div class="w-full max-w-2xl mx-auto">
  <svg viewBox="0 0 {W} {H}" class="w-full h-auto select-none" role="img" aria-label="{yLabel} versus {xLabel}">
    <!-- grid + axis ticks -->
    {#each xTicks as t}
      <line x1={sx(t)} y1={M.t} x2={sx(t)} y2={M.t + ih} class="stroke-line/40" stroke-width="1" />
      <text x={sx(t)} y={H - 22} text-anchor="middle" class="fill-muted2 text-micro">{t}</text>
    {/each}
    {#each yTicks as t}
      <line x1={M.l} y1={sy(t)} x2={M.l + iw} y2={sy(t)} class="stroke-line/40" stroke-width="1" />
      <text x={M.l - 6} y={sy(t) + 3} text-anchor="end" class="fill-muted2 text-micro">{t}</text>
    {/each}

    <!-- quadrant dividers + labels (muted so they don't clash with dot colours) -->
    {#if quadrants}
      <line x1={sx(xThr)} y1={M.t} x2={sx(xThr)} y2={M.t + ih} class="stroke-line2" stroke-width="1" stroke-dasharray="4 4" />
      <line x1={M.l} y1={sy(yThr)} x2={M.l + iw} y2={sy(yThr)} class="stroke-line2" stroke-width="1" stroke-dasharray="4 4" />
      <text x={M.l + 6} y={M.t + 13} class="fill-muted2 text-micro font-bold uppercase tracking-wide">Differentials</text>
      <text x={M.l + iw - 6} y={M.t + 13} text-anchor="end" class="fill-muted2 text-micro font-bold uppercase tracking-wide">Template</text>
      <text x={M.l + iw - 6} y={M.t + ih - 6} text-anchor="end" class="fill-muted2 text-micro font-bold uppercase tracking-wide">Traps</text>
      <text x={M.l + 6} y={M.t + ih - 6} class="fill-muted2 text-micro font-bold uppercase tracking-wide">Fringe</text>
    {/if}

    <!-- points -->
    {#each points as p (p.id)}
      <circle
        cx={sx(p.x)}
        cy={sy(p.y)}
        r={hover?.id === p.id ? 6 : 4}
        fill={POS_COLOR[p.pos]}
        fill-opacity={hover && hover.id !== p.id ? 0.3 : 0.8}
        stroke="#0b1220"
        stroke-width="0.75"
        class="cursor-pointer transition-all"
        onmouseenter={() => (hover = p)}
        onmouseleave={() => (hover = null)}
        onclick={() => onpick?.(p.id)}
        onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onpick?.(p.id) } }}
        role="button"
        tabindex="0"
        aria-label="{p.label}, {p.x.toFixed(1)}% owned, {p.y.toFixed(1)} expected points"
      ><title>{p.label} · {p.x.toFixed(1)}% owned · {p.y.toFixed(1)}</title></circle>
    {/each}

    <!-- persistent labels for standout dots, at the anchors `placed` cleared -->
    {#each placed as l (l.p.id)}
      {#if hover?.id !== l.p.id}
        <text x={l.x} y={l.y} text-anchor={l.anchor} class="fill-muted text-[9px] font-semibold pointer-events-none"
          style="paint-order:stroke;stroke:#0b1220;stroke-width:2.5px">{l.p.label}</text>
      {/if}
    {/each}

    <!-- hover label with values -->
    {#if hover}
      <text
        x={Math.min(W - M.r - 30, Math.max(M.l + 30, sx(hover.x)))}
        y={sy(hover.y) - 10}
        text-anchor="middle"
        class="fill-text text-mini font-bold"
        style="paint-order:stroke;stroke:#0b1220;stroke-width:3.5px"
      >{hover.label} · {hover.x.toFixed(1)}% · {hover.y.toFixed(0)}xP</text>
    {/if}

    <!-- axis titles -->
    <text x={M.l + iw / 2} y={H - 4} text-anchor="middle" class="fill-muted text-mini font-semibold">{xLabel}</text>
    <text x={12} y={M.t + ih / 2} text-anchor="middle" transform="rotate(-90 12 {M.t + ih / 2})" class="fill-muted text-mini font-semibold">{yLabel}</text>
  </svg>

  <!-- position legend -->
  <div class="flex items-center gap-3 justify-center mt-1 text-mini text-muted">
    {#each Object.entries(POS_COLOR) as [pos, color]}
      <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded-full inline-block" style="background:{color}"></span>{pos}</span>
    {/each}
  </div>
</div>
