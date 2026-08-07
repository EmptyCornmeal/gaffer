<script lang="ts">
  import type { Bundle } from '../lib/data'
  import type { Player, Pos } from '../lib/types'
  import { formatPrice, valuePerMillion } from '../lib/format'
  import Crest from '../components/Crest.svelte'
  import Scatter from '../components/Scatter.svelte'

  let { bundle, onpick }: { bundle: Bundle; onpick: (id: number) => void } = $props()

  // Pick the highest-`score` legal XI (1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD, 11 total).
  function bestXI(pool: Player[], score: (p: Player) => number): Player[] {
    const byPos: Record<Pos, Player[]> = { GKP: [], DEF: [], MID: [], FWD: [] }
    for (const p of pool) byPos[p.pos].push(p)
    for (const k of Object.keys(byPos) as Pos[]) byPos[k].sort((a, b) => score(b) - score(a))
    const xi: Player[] = byPos.GKP.slice(0, 1)
    const min: Record<string, number> = { DEF: 3, MID: 2, FWD: 1 }
    const max: Record<string, number> = { DEF: 5, MID: 5, FWD: 3 }
    const count: Record<string, number> = { DEF: 0, MID: 0, FWD: 0 }
    for (const pos of ['DEF', 'MID', 'FWD']) {
      for (let i = 0; i < min[pos] && i < byPos[pos as Pos].length; i++) {
        xi.push(byPos[pos as Pos][i]); count[pos]++
      }
    }
    const chosen = new Set(xi.map((p) => p.id))
    const rest = pool.filter((p) => p.pos !== 'GKP' && !chosen.has(p.id)).sort((a, b) => score(b) - score(a))
    for (const p of rest) {
      if (xi.length >= 11) break
      if (count[p.pos] < max[p.pos]) { xi.push(p); count[p.pos]++ }
    }
    return xi
  }

  // Only consider players who'll actually feature (a nailed-ish starter), so the
  // lists surface real picks, not fringe names with a flukey rate.
  const pool = $derived(bundle.players.filter((p) => p.next_gw_xp > 1.5 && p.p_start > 0.35))

  const DIFF_MAX_OWN = 12 // "differential" = owned by fewer than this %
  const TEMPLATE_MIN_OWN = 25

  // Ownership-vs-xP scatter: the single highest-signal meta view. Union the
  // top-by-projection (fills Differentials/Template) with the top-by-ownership
  // (fills Traps — high-owned, lower-projected), so all four quadrants populate.
  const scatterPts = $derived.by(() => {
    const nailed = bundle.players.filter((p) => p.p_start > 0.4 && p.owned_by > 0)
    const byXp = [...nailed].sort((a, b) => b.xp_window - a.xp_window).slice(0, 100)
    const byOwn = [...nailed].sort((a, b) => b.owned_by - a.owned_by).slice(0, 55)
    const seen = new Set<number>()
    return [...byXp, ...byOwn]
      .filter((p) => (seen.has(p.id) ? false : seen.add(p.id)))
      .map((p) => ({ id: p.id, x: p.owned_by, y: p.xp_window, label: p.name, pos: p.pos }))
  })

  const template = $derived(
    [...pool].filter((p) => p.owned_by >= TEMPLATE_MIN_OWN)
      .sort((a, b) => b.owned_by - a.owned_by).slice(0, 12),
  )
  const differentials = $derived(
    [...pool].filter((p) => p.owned_by < DIFF_MAX_OWN)
      .sort((a, b) => b.xp_window - a.xp_window).slice(0, 12),
  )
  const value = $derived(
    [...pool].map((p) => ({ p, v: valuePerMillion(p.xp_window, p.price) }))
      .sort((a, b) => b.v - a.v).slice(0, 12),
  )

  const hasCrowd = $derived(pool.some((p) => p.owned_by > 0))

  // Model XI (the balanced optimal) vs the Template XI (most-owned legal XI). The
  // players unique to each side ARE the bets: model-only = differentials the model
  // backs; template-only = crowd picks the model fades (rank risk if they haul).
  const POS_ORD: Record<Pos, number> = { GKP: 0, DEF: 1, MID: 2, FWD: 3 }
  const modelXIids = $derived(new Set(bundle.recommendation.starting.map((p) => p.id)))
  const modelXI = $derived(
    bundle.players
      .filter((p) => modelXIids.has(p.id))
      .sort((a, b) => POS_ORD[a.pos] - POS_ORD[b.pos] || b.next_gw_xp - a.next_gw_xp),
  )
  const templateXI = $derived(
    bestXI(bundle.players.filter((p) => p.owned_by > 0 && p.p_start > 0.35), (p) => p.owned_by)
      .sort((a, b) => POS_ORD[a.pos] - POS_ORD[b.pos] || b.owned_by - a.owned_by),
  )
  const templateIds = $derived(new Set(templateXI.map((p) => p.id)))
  const overlap = $derived(modelXI.filter((p) => templateIds.has(p.id)).length)
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
        <span class="text-[10px] text-muted">6-GW xP · likely starters</span>
      </div>
      <p class="text-[11px] text-muted2 mb-2">
        Top-left = high projection, low ownership (differentials you gain rank with).
        Top-right = the template you own for safety. Bottom-right = popular traps the
        model rates below their ownership. Click a dot to open the player.
      </p>
      <Scatter points={scatterPts} xLabel="Ownership %" yLabel="6-GW projected pts" xThreshold={DIFF_MAX_OWN} {onpick} />
    </section>
  {/if}

  {#if hasCrowd && templateXI.length === 11}
    <section class="card p-3">
      <div class="flex items-baseline justify-between mb-1">
        <h3 class="font-bold">Model XI vs the Template</h3>
        <span class="text-[10px] text-muted">{overlap}/11 shared</span>
      </div>
      <p class="text-[11px] text-muted2 mb-3">
        Gaffer's balanced optimal XI beside the most-owned legal XI. Players
        <span class="text-brand-light font-semibold">highlighted</span> are unique to that side —
        the model's differentials on the left, the crowd picks it fades on the right (a rank risk if they haul).
      </p>
      <div class="grid grid-cols-2 gap-3">
        <div>
          <div class="text-xs font-bold text-brand-light mb-1">Gaffer XI</div>
          {#each modelXI as p}
            <button onclick={() => onpick(p.id)} class="w-full flex items-center gap-2 py-1 text-left hover:bg-card2 rounded px-1 {templateIds.has(p.id) ? '' : 'bg-brand/8'}">
              <Crest code={p.team_code} short={p.team} size={16} />
              <span class="text-[13px] font-semibold truncate flex-1 {templateIds.has(p.id) ? '' : 'text-brand-light'}">{p.name}</span>
              <span class="text-[10px] text-muted tabular-nums">{p.owned_by}%</span>
            </button>
          {/each}
        </div>
        <div>
          <div class="text-xs font-bold text-accent-light mb-1">Template XI</div>
          {#each templateXI as p}
            <button onclick={() => onpick(p.id)} class="w-full flex items-center gap-2 py-1 text-left hover:bg-card2 rounded px-1 {modelXIids.has(p.id) ? '' : 'bg-yellow/10'}">
              <Crest code={p.team_code} short={p.team} size={16} />
              <span class="text-[13px] font-semibold truncate flex-1 {modelXIids.has(p.id) ? '' : 'text-yellow'}">{p.name}</span>
              <span class="text-[10px] text-muted tabular-nums">{p.owned_by}%</span>
            </button>
          {/each}
        </div>
      </div>
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
        {@render row(p, `${p.owned_by.toFixed(1)}%`, p.xp_window, ' xP')}
      {:else}
        <p class="text-xs text-muted2">No clear differentials right now.</p>
      {/each}
    </section>

    <!-- Template -->
    <section class="card p-3">
      <div class="flex items-baseline justify-between mb-2">
        <h3 class="font-bold text-accent-light">Template</h3>
        <span class="text-[10px] text-muted">≥{TEMPLATE_MIN_OWN}% owned · next-GW xP</span>
      </div>
      <p class="text-[11px] text-muted2 mb-2">The essential core — not owning these is itself a punt.</p>
      {#each template as p}
        {@render row(p, `${p.owned_by.toFixed(1)}%`, p.next_gw_xp, ' xP')}
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
        {@render row(p, formatPrice(p.price), v, ' /£m')}
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
