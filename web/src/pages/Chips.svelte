<script lang="ts">
  import type { Bundle } from '../lib/data'
  import type { Player } from '../lib/types'
  import { loadCurrent, lineupErrors } from '../lib/squad'
  import { parseStrategy, CHIP_LABELS } from '../lib/strategy'
  import Icon from '../components/Icon.svelte'

  let { bundle, onnav }: { bundle: Bundle; onnav: (r: string) => void } = $props()

  // T-20: when the pipeline shipped a real chip evaluation — chips valued in the
  // same simulated football as the squad, against the season's actual chip
  // windows — that is the answer. The per-GW projection below stays as the
  // *shape* of the next few weeks, but it is no longer the recommendation.
  const stratState = $derived(parseStrategy(bundle.strategy))
  const chipPlan = $derived(stratState.kind === 'ok' ? stratState.data.chips : null)

  const byId = $derived(new Map(bundle.players.map((p) => [p.id, p])))
  const plan = loadCurrent()
  const planValid = $derived(
    plan.ids.length === 15 &&
      plan.starters.length === 11 &&
      lineupErrors(plan.ids.map((id) => byId.get(id)).filter((p): p is Player => !!p), plan.starters).length === 0,
  )

  // squad + starters: your team if valid, else the model's recommended squad
  const squadIds = $derived(
    planValid ? plan.ids : [...bundle.recommendation.starting, ...bundle.recommendation.bench].map((p) => p.id),
  )
  const starterIds = $derived(planValid ? plan.starters : bundle.recommendation.starting.map((p) => p.id))
  const squad = $derived(squadIds.map((id) => byId.get(id)).filter((p): p is Player => !!p))
  const starters = $derived(squad.filter((p) => starterIds.includes(p.id)))

  // union of GWs across the squad (a blank/short player list shouldn't drop
  // columns); per-player gw_xp already sums doubles into one per-GW value.
  const gws = $derived(
    [...new Set(squad.flatMap((p) => p.gw_xp.map((g) => g.gw)))].sort((a, b) => a - b),
  )
  function xpAt(p: Player, gw: number) {
    return p.gw_xp.find((g) => g.gw === gw)?.xp ?? 0
  }

  interface Row {
    gw: number
    starters: number
    bench: number
    squad: number
    bestCap: { name: string; xp: number }
  }
  const rows = $derived<Row[]>(
    gws.map((gw) => {
      const st = starters.reduce((s, p) => s + xpAt(p, gw), 0)
      const sq = squad.reduce((s, p) => s + xpAt(p, gw), 0)
      const cap = [...starters].sort((a, b) => xpAt(b, gw) - xpAt(a, gw))[0]
      return { gw, starters: st, bench: sq - st, squad: sq, bestCap: { name: cap?.name ?? '—', xp: cap ? xpAt(cap, gw) : 0 } }
    }),
  )
  const maxStart = $derived(Math.max(1, ...rows.map((r) => r.starters)))
  // Bars scale to the XI+bench total so segment lengths are comparable across GWs.
  const axisMax = $derived(Math.max(1, ...rows.map((r) => r.squad)))

  // Chip EV = the extra points the chip banks that week, so we rank on *value
  // gained*, not just which GW looks busy:
  //   Triple Captain — the captain already doubles, so TC adds one more ×captain.
  //   Bench Boost    — the bench points that would otherwise be discarded.
  //   Free Hit       — best measured as (optimal one-off XI − your XI); without a
  //                    per-GW solve we surface the weakest XI week as the trigger.
  const tc = $derived([...rows].sort((a, b) => b.bestCap.xp - a.bestCap.xp)[0])
  const bb = $derived([...rows].sort((a, b) => b.bench - a.bench)[0])
  const fh = $derived([...rows].sort((a, b) => a.starters - b.starters)[0])
  const avgStart = $derived(rows.length ? rows.reduce((s, r) => s + r.starters, 0) / rows.length : 0)
  // The immediate-GW captain's Monte-Carlo ceiling/boom — the one week we have a
  // distribution for, so Triple Captain can be framed on upside, not just mean.
  const nextCap = $derived(
    gws.length && tc?.gw === gws[0]
      ? [...starters].map((p) => byId.get(p.id)).sort((a, b) => xpAt(b!, gws[0]) - xpAt(a!, gws[0]))[0]
      : null,
  )
</script>

<div class="rise flex flex-col gap-4 max-w-4xl">
  <div>
    <h2 class="font-bold text-lg flex items-center gap-2"><Icon name="layers" size={18} /> Chip Strategy</h2>
    <p class="text-sm text-muted">
      Best gameweeks to play each chip across the next {gws.length}, from the projected points of
      {planValid ? 'your team' : 'the model squad'}.
      {#if !planValid}<button class="text-accent-light hover:underline" onclick={() => onnav('planner')}>Build your own team →</button>{/if}
    </p>
  </div>

  {#if chipPlan}
    <!-- the real evaluation: same scenarios as the squad, real chip windows -->
    <div class="card p-4">
      <div class="flex items-baseline justify-between gap-2 flex-wrap">
        <h3 class="font-bold text-sm">Recommendation</h3>
        {#if chipPlan.recommendation !== 'hold'}
          <span class="text-sm font-bold text-brand-light tabular-nums">+{chipPlan.expected_gain.toFixed(1)} pts</span>
        {/if}
      </div>
      <div class="text-2xl font-black mt-1">
        {CHIP_LABELS[chipPlan.recommendation] ?? chipPlan.recommendation}{#if chipPlan.gameweek}
          <span class="text-lg text-muted font-bold">GW{chipPlan.gameweek}</span>{/if}
      </div>
      <p class="text-sm text-muted mt-1">{chipPlan.reason}</p>
      <div class="flex gap-1.5 flex-wrap mt-3">
        {#each chipPlan.available as w}
          <span class="chip chip-info">{CHIP_LABELS[w.name] ?? w.name} · GW{w.start_event}–{w.stop_event}</span>
        {/each}
        {#each chipPlan.used as u}
          <span class="chip chip-bad">{CHIP_LABELS[u] ?? u} · played</span>
        {/each}
      </div>
      <button class="text-[11px] text-accent-light hover:underline mt-3" onclick={() => onnav('strategy')}>
        Full breakdown, assumptions and confidence intervals →
      </button>
    </div>
  {/if}

  <!-- chip recommendations (per-GW shape; superseded above when a real plan ships) -->
  <div class="grid sm:grid-cols-3 gap-3" class:opacity-70={!!chipPlan}>
    <div class="card p-3">
      <div class="flex items-baseline justify-between">
        <div class="text-xs font-bold uppercase text-brand-light mb-1">Triple Captain</div>
        <div class="text-sm font-bold text-brand-light tabular-nums">+{tc?.bestCap.xp.toFixed(1)} pts</div>
      </div>
      <div class="text-2xl font-black">GW{tc?.gw}</div>
      <div class="text-sm text-muted">
        {tc?.bestCap.name} — the extra ×1 captain haul.{#if nextCap?.dist} Boom {nextCap.dist.boom}%, ceiling {nextCap.dist.ceiling}.{/if}
      </div>
    </div>
    <div class="card p-3">
      <div class="flex items-baseline justify-between">
        <div class="text-xs font-bold uppercase text-accent-light mb-1">Bench Boost</div>
        <div class="text-sm font-bold text-accent-light tabular-nums">+{bb?.bench.toFixed(1)} pts</div>
      </div>
      <div class="text-2xl font-black">GW{bb?.gw}</div>
      <div class="text-sm text-muted">Your bench's points, otherwise discarded — strongest bench week.</div>
    </div>
    <div class="card p-3">
      <div class="flex items-baseline justify-between">
        <div class="text-xs font-bold uppercase text-yellow mb-1">Free Hit</div>
        <div class="text-sm font-bold text-yellow tabular-nums">−{(avgStart - (fh?.starters ?? 0)).toFixed(1)} vs avg</div>
      </div>
      <div class="text-2xl font-black">GW{fh?.gw}</div>
      <div class="text-sm text-muted">Weakest XI week (~{fh?.starters.toFixed(1)}) — field a one-off side, or save for a blank/double.</div>
    </div>
  </div>

  <!-- per-GW projection -->
  <div class="card p-4">
    <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
      <h3 class="font-bold">Projected points, next {gws.length} GWs</h3>
      <div class="flex items-center gap-3 text-[11px]">
        <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm bg-brand/80 inline-block"></span>Starting XI</span>
        <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm bg-accent/60 inline-block"></span>Bench</span>
      </div>
    </div>
    <div class="space-y-1.5">
      {#each rows as r}
        {@const isBB = r.gw === bb?.gw}
        {@const isTC = r.gw === tc?.gw}
        <div class="flex items-center gap-2">
          <span class="text-[11px] w-9 tabular-nums {isBB || isTC ? 'text-brand-light font-bold' : 'text-muted2'}">GW{r.gw}</span>
          <div class="flex-1 h-6 rounded bg-bg3 overflow-hidden flex {isBB ? 'ring-1 ring-accent/60' : ''}">
            <div class="h-full bg-brand/80 flex items-center justify-end pr-1.5 text-[10px] font-bold text-[#05210f]" style="width:{(r.starters / axisMax) * 100}%">{r.starters.toFixed(0)}</div>
            <div class="h-full bg-accent/60 flex items-center justify-end pr-1.5 text-[9px] font-semibold text-white/90" style="width:{(r.bench / axisMax) * 100}%" title="bench {r.bench.toFixed(1)}">{r.bench >= 2 ? '+' + r.bench.toFixed(0) : ''}</div>
          </div>
          <span class="w-16 text-right shrink-0">
            {#if isBB}<span class="chip chip-info">BB</span>{:else if isTC}<span class="chip chip-good">TC</span>{/if}
          </span>
          <span class="text-[11px] text-muted w-20 text-right truncate hidden sm:inline">C: {r.bestCap.name}</span>
        </div>
      {/each}
    </div>
    <p class="text-[11px] text-muted2 mt-3">
      Pre-season every team has one fixture, so weeks look flat; double/blank gameweeks —
      which supercharge Bench Boost &amp; Free Hit — are announced later and this chart
      updates automatically.
      {#if chipPlan}Chip windows above come from the live API, not a hard-coded split.{/if}
    </p>
  </div>
</div>
