<script lang="ts">
  import type { Bundle } from '../lib/data'
  import type { Player } from '../lib/types'
  import Crest from '../components/Crest.svelte'
  import Scatter from '../components/Scatter.svelte'

  let { bundle, onpick }: { bundle: Bundle; onpick: (id: number) => void } = $props()

  // Only consider players who'll actually feature (a nailed-ish starter), so the
  // lists surface real picks, not fringe names with a flukey rate.
  const pool = $derived(bundle.players.filter((p) => p.next_gw_xp > 1.5 && p.p_start > 0.35))

  const DIFF_MAX_OWN = 12 // "differential" = owned by fewer than this %
  const TEMPLATE_MIN_OWN = 25

  // Ownership-vs-xP scatter: the single highest-signal meta view. Plot the pool
  // (label the biggest names so the chart is readable), split into quadrants at
  // the differential threshold and mean projected points.
  const scatterPts = $derived(
    [...pool]
      .sort((a, b) => b.xp_window - a.xp_window)
      .slice(0, 90)
      .map((p) => ({ id: p.id, x: p.owned_by, y: p.xp_window, label: p.name, pos: p.pos })),
  )

  const template = $derived(
    [...pool].filter((p) => p.owned_by >= TEMPLATE_MIN_OWN)
      .sort((a, b) => b.owned_by - a.owned_by).slice(0, 12),
  )
  const differentials = $derived(
    [...pool].filter((p) => p.owned_by < DIFF_MAX_OWN)
      .sort((a, b) => b.xp_window - a.xp_window).slice(0, 12),
  )
  const value = $derived(
    [...pool].map((p) => ({ p, v: p.xp_window / Math.max(p.price / 10, 0.1) }))
      .sort((a, b) => b.v - a.v).slice(0, 12),
  )

  const hasCrowd = $derived(pool.some((p) => p.owned_by > 0))
</script>

<div class="rise flex flex-col gap-4">
  <div class="flex items-center justify-between">
    <h2 class="font-bold text-lg">Meta &amp; differentials</h2>
    <span class="text-xs text-muted">ownership vs projected points</span>
  </div>

  {#if !hasCrowd}
    <div class="card p-4 text-sm text-muted">
      Ownership data appears once the season opens — these lists sharpen as real
      ownership settles. For now they rank on projected points.
    </div>
  {/if}

  {#if hasCrowd}
    <section class="card p-3">
      <div class="flex items-baseline justify-between mb-1">
        <h3 class="font-bold">Ownership vs projected points</h3>
        <span class="text-[10px] text-muted">6-GW xP · top 90 by projection</span>
      </div>
      <p class="text-[11px] text-muted2 mb-2">
        Top-left = high projection, low ownership (differentials you gain rank with).
        Top-right = the template you own for safety. Bottom-right = popular traps the
        model rates below their ownership. Click a dot to open the player.
      </p>
      <Scatter points={scatterPts} xLabel="Ownership %" yLabel="6-GW projected pts" xThreshold={DIFF_MAX_OWN} {onpick} />
    </section>
  {/if}

  <div class="grid gap-4 lg:grid-cols-3">
    <!-- Differentials -->
    <section class="card p-3">
      <div class="flex items-baseline justify-between mb-2">
        <h3 class="font-bold text-brand-light">Differentials</h3>
        <span class="text-[10px] text-muted">&lt;{DIFF_MAX_OWN}% owned · by 6-GW xP</span>
      </div>
      <p class="text-[11px] text-muted2 mb-2">Low-owned, high-projected — where you gain rank on the crowd.</p>
      {#each differentials as p}
        {@render row(p, `${p.owned_by.toFixed(1)}%`, p.xp_window)}
      {:else}
        <p class="text-xs text-muted2">No clear differentials right now.</p>
      {/each}
    </section>

    <!-- Template -->
    <section class="card p-3">
      <div class="flex items-baseline justify-between mb-2">
        <h3 class="font-bold text-accent-light">Template</h3>
        <span class="text-[10px] text-muted">≥{TEMPLATE_MIN_OWN}% owned</span>
      </div>
      <p class="text-[11px] text-muted2 mb-2">The essential core — not owning these is itself a punt.</p>
      {#each template as p}
        {@render row(p, `${p.owned_by.toFixed(1)}%`, p.next_gw_xp)}
      {:else}
        <p class="text-xs text-muted2">Ownership settles once the season starts.</p>
      {/each}
    </section>

    <!-- Value -->
    <section class="card p-3">
      <div class="flex items-baseline justify-between mb-2">
        <h3 class="font-bold text-yellow">Best value</h3>
        <span class="text-[10px] text-muted">6-GW xP per £m</span>
      </div>
      <p class="text-[11px] text-muted2 mb-2">Most projected points per million — squad-builder fuel.</p>
      {#each value as { p, v }}
        {@render row(p, `£${p.price / 10}m`, v, ' /£m')}
      {/each}
    </section>
  </div>
</div>

{#snippet row(p: Player, sub: string, metric: number, suffix = '')}
  <button
    onclick={() => onpick(p.id)}
    class="w-full flex items-center gap-2 py-1.5 border-b border-line/40 last:border-0 text-left hover:bg-card2 rounded px-1"
  >
    <Crest code={p.team_code} short={p.team} size={18} />
    <div class="min-w-0 flex-1">
      <div class="text-sm font-semibold truncate">{p.name}</div>
      <div class="text-[10px] text-muted">{p.pos} · {p.team} · {sub}</div>
    </div>
    <div class="text-right shrink-0">
      <div class="text-sm font-bold text-brand-light tabular-nums">{metric.toFixed(metric < 10 ? 1 : 0)}{suffix}</div>
    </div>
  </button>
{/snippet}
