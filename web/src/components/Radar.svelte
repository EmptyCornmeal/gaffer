<script lang="ts">
  // Dependency-free SVG radar/pizza chart (CSP-safe, theme-aware). Values are
  // normalised 0..1 (percentiles). Overlays up to 3 series for player comparison.
  interface Series {
    label: string
    color: string
    values: number[] // 0..1, one per axis, same order as `axes`
  }
  let { axes, series }: { axes: string[]; series: Series[] } = $props()

  const SIZE = 340
  const C = SIZE / 2
  const R = C - 54 // leave room for labels
  const n = $derived(axes.length)

  function pt(i: number, r: number): [number, number] {
    const ang = -Math.PI / 2 + (i / n) * Math.PI * 2
    return [C + Math.cos(ang) * r, C + Math.sin(ang) * r]
  }
  function poly(vals: number[]): string {
    return vals.map((v, i) => pt(i, R * Math.max(0.02, Math.min(1, v))).join(',')).join(' ')
  }
  const rings = [0.25, 0.5, 0.75, 1]
</script>

<div class="w-full flex flex-col items-center">
  <svg viewBox="0 0 {SIZE} {SIZE}" class="w-full max-w-[340px] h-auto" role="img" aria-label="Player comparison radar">
    <!-- rings -->
    {#each rings as rr}
      <polygon
        points={Array.from({ length: n }, (_, i) => pt(i, R * rr).join(',')).join(' ')}
        fill="none" class="stroke-line/50" stroke-width="1"
      />
    {/each}
    <!-- spokes + axis labels -->
    {#each axes as ax, i}
      {@const [ex, ey] = pt(i, R)}
      {@const [lx, ly] = pt(i, R + 18)}
      <line x1={C} y1={C} x2={ex} y2={ey} class="stroke-line/50" stroke-width="1" />
      <text x={lx} y={ly} text-anchor="middle" dominant-baseline="middle" class="fill-muted text-[10px] font-semibold">{ax}</text>
    {/each}
    <!-- series -->
    {#each series as s}
      <polygon points={poly(s.values)} fill={s.color} fill-opacity="0.16" stroke={s.color} stroke-width="2" />
      {#each s.values as v, i}
        {@const [px, py] = pt(i, R * Math.max(0.02, Math.min(1, v)))}
        <circle cx={px} cy={py} r="2.5" fill={s.color} />
      {/each}
    {/each}
  </svg>
  <div class="flex flex-wrap items-center gap-3 justify-center mt-1 text-[11px]">
    {#each series as s}
      <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full inline-block" style="background:{s.color}"></span>{s.label}</span>
    {/each}
  </div>
</div>
