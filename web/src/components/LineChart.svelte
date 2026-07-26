<script lang="ts">
  // Self-contained multi-series SVG line chart (no external libs — CSP-safe).
  // "You" is drawn thick in emerald; rivals use the categorical palette.
  export type Series = { name: string; color: string; values: (number | null)[]; you?: boolean }

  let {
    series,
    labels,
    height = 240,
    yLabel = '',
  }: { series: Series[]; labels: string[]; height?: number; yLabel?: string } = $props()

  const PAD = { l: 40, r: 12, t: 12, b: 22 }
  const W = 720

  const allVals = $derived(series.flatMap((s) => s.values).filter((v): v is number => v != null))
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
</script>

<div class="w-full overflow-x-auto">
  <svg viewBox="0 0 {W} {height}" class="w-full" style="min-width: 320px" role="img" aria-label={yLabel}>
    <!-- gridlines + y ticks -->
    {#each ticks as t}
      <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} stroke="var(--color-line)" stroke-width="1" />
      <text x={PAD.l - 6} y={y(t) + 3} text-anchor="end" font-size="9" fill="var(--color-muted2)" style="font-variant-numeric:tabular-nums">{Math.round(t)}</text>
    {/each}
    <!-- x labels -->
    {#each labels as lab, i}
      {#if i % labelStep === 0}
        <text x={x(i)} y={height - 6} text-anchor="middle" font-size="9" fill="var(--color-muted2)">{lab}</text>
      {/if}
    {/each}
    <!-- series -->
    {#each series as s}
      <path d={pathFor(s.values)} fill="none" stroke={s.color} stroke-width={s.you ? 3 : 1.5} stroke-linecap="round" stroke-linejoin="round" opacity={s.you ? 1 : 0.85} />
    {/each}
  </svg>
</div>

{#if series.length}
  <div class="flex flex-wrap gap-x-3 gap-y-1 mt-2 text-[11px]">
    {#each series as s}
      <span class="flex items-center gap-1 {s.you ? 'font-bold text-text' : 'text-muted'}">
        <span class="w-2.5 h-2.5 rounded-sm" style="background:{s.color}"></span>{s.name}
      </span>
    {/each}
  </div>
{/if}
