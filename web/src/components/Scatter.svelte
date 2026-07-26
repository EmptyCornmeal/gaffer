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
</script>

<div class="w-full max-w-2xl mx-auto">
  <svg viewBox="0 0 {W} {H}" class="w-full h-auto select-none" role="img" aria-label="{yLabel} versus {xLabel}">
    <!-- grid + axis ticks -->
    {#each xTicks as t}
      <line x1={sx(t)} y1={M.t} x2={sx(t)} y2={M.t + ih} class="stroke-line/40" stroke-width="1" />
      <text x={sx(t)} y={H - 22} text-anchor="middle" class="fill-muted2 text-[10px]">{t}</text>
    {/each}
    {#each yTicks as t}
      <line x1={M.l} y1={sy(t)} x2={M.l + iw} y2={sy(t)} class="stroke-line/40" stroke-width="1" />
      <text x={M.l - 6} y={sy(t) + 3} text-anchor="end" class="fill-muted2 text-[10px]">{t}</text>
    {/each}

    <!-- quadrant dividers + labels (muted so they don't clash with dot colours) -->
    {#if quadrants}
      <line x1={sx(xThr)} y1={M.t} x2={sx(xThr)} y2={M.t + ih} class="stroke-line2" stroke-width="1" stroke-dasharray="4 4" />
      <line x1={M.l} y1={sy(yThr)} x2={M.l + iw} y2={sy(yThr)} class="stroke-line2" stroke-width="1" stroke-dasharray="4 4" />
      <text x={M.l + 6} y={M.t + 13} class="fill-muted2 text-[10px] font-bold uppercase tracking-wide">Differentials</text>
      <text x={M.l + iw - 6} y={M.t + 13} text-anchor="end" class="fill-muted2 text-[10px] font-bold uppercase tracking-wide">Template</text>
      <text x={M.l + iw - 6} y={M.t + ih - 6} text-anchor="end" class="fill-muted2 text-[10px] font-bold uppercase tracking-wide">Traps</text>
      <text x={M.l + 6} y={M.t + ih - 6} class="fill-muted2 text-[10px] font-bold uppercase tracking-wide">Fringe</text>
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
        role="button"
        tabindex="-1"
        aria-label={p.label}
        onmouseenter={() => (hover = p)}
        onmouseleave={() => (hover = null)}
        onclick={() => onpick?.(p.id)}
      ><title>{p.label} · {p.x.toFixed(1)}% owned · {p.y.toFixed(1)}</title></circle>
    {/each}

    <!-- persistent labels for standout dots -->
    {#each labelled as p (p.id)}
      {#if hover?.id !== p.id}
        <text x={sx(p.x)} y={sy(p.y) - 7} text-anchor="middle" class="fill-muted text-[9px] font-semibold pointer-events-none"
          style="paint-order:stroke;stroke:#0b1220;stroke-width:2.5px">{p.label}</text>
      {/if}
    {/each}

    <!-- hover label with values -->
    {#if hover}
      <text
        x={Math.min(W - M.r - 30, Math.max(M.l + 30, sx(hover.x)))}
        y={sy(hover.y) - 10}
        text-anchor="middle"
        class="fill-text text-[11px] font-bold"
        style="paint-order:stroke;stroke:#0b1220;stroke-width:3.5px"
      >{hover.label} · {hover.x.toFixed(1)}% · {hover.y.toFixed(0)}xP</text>
    {/if}

    <!-- axis titles -->
    <text x={M.l + iw / 2} y={H - 4} text-anchor="middle" class="fill-muted text-[11px] font-semibold">{xLabel}</text>
    <text x={12} y={M.t + ih / 2} text-anchor="middle" transform="rotate(-90 12 {M.t + ih / 2})" class="fill-muted text-[11px] font-semibold">{yLabel}</text>
  </svg>

  <!-- position legend -->
  <div class="flex items-center gap-3 justify-center mt-1 text-[11px] text-muted">
    {#each Object.entries(POS_COLOR) as [pos, color]}
      <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded-full inline-block" style="background:{color}"></span>{pos}</span>
    {/each}
  </div>
</div>
