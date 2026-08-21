<script lang="ts">
  // Self-contained multi-series SVG line chart (no external libs — CSP-safe).
  // "You" is drawn thick in emerald; rivals use the categorical palette.
  //
  // Interactive: hover (or arrow-key) a gameweek for a ranked read-out of every
  // visible series, click a legend entry to mute it, and `focusKey` dims
  // everything except one series so the parent table can drive the chart.
  export type Series = {
    name: string
    color: string
    values: (number | null)[]
    you?: boolean
    key?: string | number
  }

  let {
    series,
    labels,
    height = 240,
    yLabel = '',
    format = (v: number) => String(Math.round(v)),
    focusKey = null,
  }: {
    series: Series[]
    labels: string[]
    height?: number
    yLabel?: string
    format?: (v: number) => string
    focusKey?: string | number | null
  } = $props()

  const PAD = { l: 40, r: 12, t: 12, b: 22 }
  const W = 720

  // Muted series are excluded from the scale too, so hiding a runaway rival
  // actually rescales the chart instead of leaving everyone squashed.
  let muted = $state<string[]>([])
  let hover = $state<number | null>(null)

  const visible = $derived(series.filter((s) => !muted.includes(s.name)))
  const allVals = $derived(visible.flatMap((s) => s.values).filter((v): v is number => v != null))
  const yMax = $derived(allVals.length ? Math.max(...allVals) : 1)
  const yMin = $derived(Math.min(0, ...(allVals.length ? allVals : [0])))
  const n = $derived(labels.length)

  const plotW = W - PAD.l - PAD.r
  const plotH = $derived(height - PAD.t - PAD.b)

  function x(i: number): number {
    return PAD.l + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW)
  }
  function y(v: number): number {
    const span = yMax - yMin || 1
    return PAD.t + plotH - ((v - yMin) / span) * plotH
  }
  function pathFor(vals: (number | null)[]): string {
    let d = ''
    let started = false
    vals.forEach((v, i) => {
      if (v == null) {
        started = false
        return
      }
      d += `${started ? 'L' : 'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)} `
      started = true
    })
    return d.trim()
  }


  // A path of one point is `M x y`, which renders NOTHING: an SVG path needs a
  // segment to paint. So a single-gameweek league — every league, the first
  // time anyone looks — drew a blank chart, and so did any manager whose points
  // sat between gaps. Points with no drawable neighbour get a dot instead.
  function isolated(vals: (number | null)[]): number[] {
    const out: number[] = []
    vals.forEach((v, i) => {
      if (v == null) return
      if (vals[i - 1] == null && vals[i + 1] == null) out.push(i)
    })
    return out
  }

  // ~4 horizontal gridlines at round-ish values
  const ticks = $derived.by(() => {
    const span = yMax - yMin || 1
    const step = niceStep(span / 4)
    const out: number[] = []
    for (let t = Math.ceil(yMin / step) * step; t <= yMax + 1e-9; t += step) out.push(t)
    return out
  })
  function niceStep(raw: number): number {
    const pow = Math.pow(10, Math.floor(Math.log10(raw || 1)))
    const norm = raw / pow
    const nice = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10
    return nice * pow
  }
  // thin out x labels if crowded
  const labelStep = $derived(Math.ceil(n / 12))

  function dimmed(s: Series): boolean {
    return focusKey != null && s.key !== focusKey
  }
  function toggle(name: string) {
    muted = muted.includes(name) ? muted.filter((m) => m !== name) : [...muted, name]
  }

  // -- hover ---------------------------------------------------------------
  // The SVG scales to its container, so a client x has to come back through the
  // viewBox before it means anything in plot coordinates.
  function indexAt(clientX: number, el: HTMLElement): number {
    const rect = el.getBoundingClientRect()
    if (!rect.width || n <= 1) return 0
    const vx = ((clientX - rect.left) / rect.width) * W
    const i = Math.round(((vx - PAD.l) / plotW) * (n - 1))
    return Math.max(0, Math.min(n - 1, i))
  }
  function onMove(e: PointerEvent) {
    if (!n) return
    hover = indexAt(e.clientX, e.currentTarget as HTMLElement)
  }
  function onKey(e: KeyboardEvent) {
    if (!n) return
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault()
      const cur = hover ?? (e.key === 'ArrowRight' ? -1 : n)
      hover = Math.max(0, Math.min(n - 1, cur + (e.key === 'ArrowRight' ? 1 : -1)))
    } else if (e.key === 'Escape') {
      hover = null
    }
  }

  const readout = $derived.by(() => {
    if (hover == null) return []
    return visible
      .map((s) => ({ s, v: s.values[hover as number] }))
      .filter((r): r is { s: Series; v: number } => r.v != null)
      .sort((a, b) => b.v - a.v)
  })
  // A slider that announces "3" is useless. Announce the gameweek and who is
  // where in it, so the chart is readable without seeing it.
  const valueText = $derived.by(() => {
    if (hover == null) return labels.length ? 'no gameweek selected' : 'no data'
    const top = readout
      .slice(0, 5)
      .map((r) => r.s.name + ' ' + format(r.v))
      .join(', ')
    return top ? labels[hover] + ': ' + top : String(labels[hover])
  })
  // Flip the tooltip to the left once the cursor passes the midpoint, so it
  // never runs off the right edge.
  const tipLeft = $derived(hover == null ? 0 : (x(hover) / W) * 100)
  const tipFlip = $derived(tipLeft > 55)
</script>

<div class="w-full overflow-x-auto">
  <div
    class="relative"
    style="min-width: 320px"
    role="slider"
    tabindex="0"
    aria-label="{yLabel || 'chart'} — left and right arrow keys read each gameweek"
    aria-orientation="horizontal"
    aria-valuemin={0}
    aria-valuemax={Math.max(0, n - 1)}
    aria-valuenow={hover ?? 0}
    aria-valuetext={valueText}
    onpointermove={onMove}
    onpointerleave={() => (hover = null)}
    onkeydown={onKey}
  >
    <svg viewBox="0 0 {W} {height}" class="w-full block" aria-hidden="true">
      <!-- gridlines + y ticks -->
      {#each ticks as t}
        <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} stroke="var(--color-line)" stroke-width="1" />
        <text x={PAD.l - 6} y={y(t) + 3} text-anchor="end" font-size="9" fill="var(--color-muted2)" style="font-variant-numeric:tabular-nums">{format(t)}</text>
      {/each}
      <!-- x labels -->
      {#each labels as lab, i}
        {#if i % labelStep === 0}
          <text x={x(i)} y={height - 6} text-anchor="middle" font-size="9" fill="var(--color-muted2)">{lab}</text>
        {/if}
      {/each}
      <!-- hover crosshair, drawn under the lines -->
      {#if hover != null}
        <line x1={x(hover)} x2={x(hover)} y1={PAD.t} y2={PAD.t + plotH} stroke="var(--color-muted2)" stroke-width="1" stroke-dasharray="3 3" />
      {/if}
      <!-- series -->
      {#each visible as s}
        <path
          d={pathFor(s.values)}
          fill="none"
          stroke={s.color}
          stroke-width={s.you ? 3 : 1.5}
          stroke-linecap="round"
          stroke-linejoin="round"
          opacity={dimmed(s) ? 0.15 : s.you ? 1 : 0.85}
        />
        {#each isolated(s.values) as i}
          <circle
            cx={x(i)}
            cy={y(s.values[i] as number)}
            r={s.you ? 3.5 : 2.5}
            fill={s.color}
            opacity={dimmed(s) ? 0.15 : s.you ? 1 : 0.85}
          />
        {/each}
      {/each}
      <!-- hover dots -->
      {#if hover != null}
        {#each visible as s}
          {#if s.values[hover] != null}
            <circle
              cx={x(hover)}
              cy={y(s.values[hover] as number)}
              r={s.you ? 4 : 3}
              fill="var(--color-bg2)"
              stroke={s.color}
              stroke-width="2"
              opacity={dimmed(s) ? 0.2 : 1}
            />
          {/if}
        {/each}
      {/if}
    </svg>

    {#if hover != null && readout.length}
      <div
        class="pointer-events-none absolute top-1 z-10 rounded-lg border border-line bg-bg2/95 px-2.5 py-2 shadow-lg backdrop-blur-sm"
        style="left: {tipLeft}%; transform: translateX({tipFlip ? '-100%' : '0'}) translateX({tipFlip ? '-8px' : '8px'}); max-width: 260px"
      >
        <div class="text-micro font-bold uppercase tracking-wide text-muted2 mb-1">{labels[hover]}</div>
        <div class="flex flex-col gap-0.5">
          {#each readout.slice(0, 12) as r}
            <div class="flex items-center gap-1.5 text-mini {r.s.you ? 'font-bold text-text' : 'text-muted'}">
              <span class="w-2 h-2 rounded-sm shrink-0" style="background:{r.s.color}"></span>
              <span class="truncate">{r.s.name}</span>
              <span class="ml-auto tabular-nums pl-2">{format(r.v)}</span>
            </div>
          {/each}
        </div>
      </div>
    {/if}
  </div>
</div>

{#if series.length}
  <div class="flex flex-wrap gap-x-3 gap-y-1 mt-2 text-mini">
    {#each series as s}
      <button
        type="button"
        onclick={() => toggle(s.name)}
        title="{muted.includes(s.name) ? 'Show' : 'Hide'} {s.name}"
        class="flex items-center gap-1 rounded px-0.5 transition hover:text-text {s.you ? 'font-bold text-text' : 'text-muted'} {muted.includes(s.name) ? 'opacity-40 line-through' : ''}"
      >
        <span class="w-2.5 h-2.5 rounded-sm" style="background:{s.color}"></span>{s.name}
      </button>
    {/each}
    {#if muted.length}
      <button type="button" onclick={() => (muted = [])} class="text-accent-light hover:underline">reset</button>
    {/if}
  </div>
{/if}
