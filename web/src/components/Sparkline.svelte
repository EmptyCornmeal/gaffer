<script lang="ts">
  // Tiny inline sparkline — highest signal-per-pixel element for form/xP trends.
  // No axes, 2px emerald line, last point dotted. Flat line when <2 points.
  let {
    values,
    width = 68,
    height = 22,
    color = 'var(--color-brand-light)',
  }: { values: number[]; width?: number; height?: number; color?: string } = $props()

  const pts = $derived(values.filter((v) => Number.isFinite(v)))
  const path = $derived.by(() => {
    if (pts.length < 2) return null
    const min = Math.min(...pts)
    const max = Math.max(...pts)
    const span = max - min || 1
    const pad = 2
    const w = width - pad * 2
    const h = height - pad * 2
    const step = w / (pts.length - 1)
    const xy = pts.map((v, i) => {
      const x = pad + i * step
      const y = pad + h - ((v - min) / span) * h
      return [x, y] as const
    })
    const d = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join(' ')
    const area = `${d} L${xy[xy.length - 1][0].toFixed(1)} ${height - pad} L${xy[0][0].toFixed(1)} ${height - pad} Z`
    return { d, area, last: xy[xy.length - 1] }
  })
</script>

{#if path}
  <svg {width} {height} viewBox="0 0 {width} {height}" class="overflow-visible">
    <defs>
      <linearGradient id="spark-fill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color={color} stop-opacity="0.22" />
        <stop offset="100%" stop-color={color} stop-opacity="0" />
      </linearGradient>
    </defs>
    <path d={path.area} fill="url(#spark-fill)" />
    <path d={path.d} fill="none" stroke={color} stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
    <circle cx={path.last[0]} cy={path.last[1]} r="2.1" fill={color} />
  </svg>
{:else}
  <div style="width:{width}px;height:{height}px" class="flex items-center">
    <div class="w-full border-t border-dashed border-line2"></div>
  </div>
{/if}
