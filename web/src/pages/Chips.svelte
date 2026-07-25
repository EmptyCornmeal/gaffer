<script lang="ts">
  import type { Bundle } from '../lib/data'
  import type { Player } from '../lib/types'
  import { loadCurrent, lineupErrors } from '../lib/squad'

  let { bundle, onnav }: { bundle: Bundle; onnav: (r: string) => void } = $props()

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

  const tc = $derived([...rows].sort((a, b) => b.bestCap.xp - a.bestCap.xp)[0])
  const bb = $derived([...rows].sort((a, b) => b.bench - a.bench)[0])
  const fh = $derived([...rows].sort((a, b) => a.starters - b.starters)[0])
</script>

<div class="rise flex flex-col gap-4 max-w-4xl">
  <div>
    <h2 class="font-bold text-lg">♟️ Chip Strategy</h2>
    <p class="text-sm text-muted">
      Best gameweeks to play each chip across the next {gws.length}, based on your
      {planValid ? 'team' : "team (showing the model squad — build yours in the Planner)"}'s
      projected points. {#if !planValid}<button class="text-accent-light hover:underline" onclick={() => onnav('planner')}>Build your team →</button>{/if}
    </p>
  </div>

  <!-- chip recommendations -->
  <div class="grid sm:grid-cols-3 gap-3">
    <div class="card p-3">
      <div class="text-xs font-bold uppercase text-brand-light mb-1">🅲 Triple Captain</div>
      <div class="text-2xl font-black">GW{tc?.gw}</div>
      <div class="text-sm text-muted">{tc?.bestCap.name} projects {tc?.bestCap.xp.toFixed(1)} — highest ceiling.</div>
    </div>
    <div class="card p-3">
      <div class="text-xs font-bold uppercase text-accent-light mb-1">🅱 Bench Boost</div>
      <div class="text-2xl font-black">GW{bb?.gw}</div>
      <div class="text-sm text-muted">Bench adds ~{bb?.bench.toFixed(1)} pts — your strongest bench week.</div>
    </div>
    <div class="card p-3">
      <div class="text-xs font-bold uppercase text-yellow mb-1">🅵 Free Hit</div>
      <div class="text-2xl font-black">GW{fh?.gw}</div>
      <div class="text-sm text-muted">Weakest XI week (~{fh?.starters.toFixed(1)}) — a candidate to field a one-off side.</div>
    </div>
  </div>

  <!-- per-GW projection -->
  <div class="card p-4">
    <div class="flex items-center justify-between mb-3">
      <h3 class="font-bold">Your projected points, next {gws.length} GWs</h3>
      <span class="text-xs text-muted">XI · bench</span>
    </div>
    <div class="space-y-2">
      {#each rows as r}
        <div class="flex items-center gap-3">
          <span class="text-xs text-muted w-10">GW{r.gw}</span>
          <div class="flex-1 h-6 rounded bg-bg3 overflow-hidden flex">
            <div class="h-full bg-brand/80 flex items-center justify-end pr-2 text-[10px] font-bold text-[#05210f]" style="width:{(r.starters / maxStart) * 100}%">{r.starters.toFixed(0)}</div>
            <div class="h-full bg-accent/50" style="width:{(r.bench / maxStart) * 100}%" title="bench {r.bench.toFixed(1)}"></div>
          </div>
          <span class="text-xs text-muted w-24 text-right">cap {r.bestCap.name}</span>
        </div>
      {/each}
    </div>
    <p class="text-[11px] text-muted2 mt-3">
      Pre-season the schedule has one fixture per team; double/blank gameweeks (which
      supercharge Bench Boost &amp; Free Hit) are announced later — this radar updates
      automatically when they are. First chip set must be used by the GW19 deadline.
    </p>
  </div>
</div>
