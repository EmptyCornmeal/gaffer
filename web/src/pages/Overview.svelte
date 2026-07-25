<script lang="ts">
  import type { Bundle } from '../lib/data'
  import type { Player, RecPlayer } from '../lib/types'
  import Pitch from '../components/Pitch.svelte'
  import FixtureStrip from '../components/FixtureStrip.svelte'
  import { mdLite } from '../lib/mdlite'
  import { loadCurrent, lineupErrors, formationOf, planPoints } from '../lib/squad'
  import { generateTeamBrief } from '../lib/teamBrief'

  let { bundle, onpick, onnav }: { bundle: Bundle; onpick: (id: number) => void; onnav: (r: string) => void } = $props()
  const rec = $derived(bundle.recommendation)
  const verdict = $derived(bundle.verdict)

  // Your picked team (from the Planner). If a valid one exists, lead with it.
  const byId = $derived(new Map(bundle.players.map((p) => [p.id, p])))
  const plan = loadCurrent()
  const planSquad = $derived(plan.ids.map((id) => byId.get(id)).filter((p): p is Player => !!p))
  const planValid = $derived(
    planSquad.length === 15 && plan.starters.length === 11 && lineupErrors(planSquad, plan.starters).length === 0,
  )
  function toRec(p: Player): RecPlayer {
    return { id: p.id, code: p.code, team_code: p.team_code, name: p.name, team: p.team, pos: p.pos, price: p.price, next_gw_xp: p.next_gw_xp, confidence: p.confidence }
  }
  const yourStarters = $derived(planSquad.filter((p) => plan.starters.includes(p.id)).map(toRec))
  const yourCaptain = $derived(byId.get(plan.captainId))
  const modelCaptain = $derived(rec.captain)
  let view = $state<'your' | 'model'>('model')
  // default to "your" once we know a valid plan exists
  $effect(() => {
    if (planValid) view = 'your'
  })
  const teamBrief = $derived(
    planValid ? generateTeamBrief(planSquad, plan.starters, plan.captainId, bundle.players) : '',
  )
  const showYourBrief = $derived(view === 'your' && planValid)
  const P = $derived(bundle.players)

  const topCaptains = $derived([...P].sort((a, b) => b.next_gw_xp - a.next_gw_xp).slice(0, 5))
  const bestValue = $derived(
    [...P]
      .filter((p) => p.p_start > 0.6 && p.price >= 4.5)
      .sort((a, b) => b.next_gw_xp / b.price - a.next_gw_xp / a.price)
      .slice(0, 5),
  )
  const risers = $derived([...P].filter((p) => p.net_transfers > 0).sort((a, b) => b.net_transfers - a.net_transfers).slice(0, 5))
  const fallers = $derived([...P].filter((p) => p.net_transfers < 0).sort((a, b) => a.net_transfers - b.net_transfers).slice(0, 5))
  const hasMarket = $derived(risers.length > 0 || fallers.length > 0)
  const fmtK = (n: number) => (Math.abs(n) >= 1000 ? `${(n / 1000).toFixed(0)}k` : `${n}`)

  const hasForm = $derived(P.some((p) => p.form > 0))
  const inForm = $derived(
    hasForm
      ? [...P].filter((p) => p.form >= 4).sort((a, b) => b.form - a.form).slice(0, 5)
      : [...P].filter((p) => p.p_start > 0.6).sort((a, b) => b.xgi90 - a.xgi90).slice(0, 5),
  )
  const formTitle = $derived(hasForm ? 'In form' : 'Top underlying threat')
</script>

<div class="flex flex-col gap-4 rise">
  <!-- Gaffer's Verdict (AI briefing) -->
  {#if verdict || showYourBrief}
    <div class="card p-4 border-brand/40 bg-brand/8">
      <div class="flex items-center justify-between mb-1">
        <div class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-brand-light">
          <span>🧠 The Gaffer's Verdict</span>
          <span class="chip {showYourBrief ? 'chip-good' : 'chip-info'}">{showYourBrief ? 'your team' : 'model'}</span>
        </div>
        <span class="text-[10px] text-muted2">{showYourBrief ? 'live' : verdict && verdict.source.startsWith('ai') ? verdict.model : 'auto'}</span>
      </div>
      <div class="verdict text-[15px] leading-relaxed text-text">
        {@html mdLite(showYourBrief ? teamBrief : (verdict?.briefing_md ?? ''))}
      </div>
    </div>
  {/if}

  <!-- headline recommendation -->
  <div class="card p-4 bg-gradient-to-br from-bg3 to-card">
    <div class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-brand">
      <span class="w-2 h-2 rounded-full bg-brand"></span>
      {rec.mode === 'build' ? 'Model squad this week' : 'This week'}
    </div>
    <p class="mt-2 text-[15px] text-text/95">{rec.summary}</p>
    {#if rec.captain.rationale}
      <p class="mt-1 text-sm text-muted"><b class="text-brand-light">Captain {rec.captain.name}:</b> {rec.captain.rationale}</p>
    {/if}
  </div>

  <div class="grid lg:grid-cols-3 gap-4">
    <!-- pitch spans 2 -->
    <div class="lg:col-span-2 card p-3">
      <div class="flex items-center justify-between mb-2 px-1 gap-2 flex-wrap">
        <div class="flex items-center gap-1">
          {#if planValid}
            <button onclick={() => (view = 'your')} class="px-2.5 py-1 rounded-lg text-sm font-bold {view === 'your' ? 'bg-brand/15 text-brand-light' : 'text-muted'}">Your team</button>
            <button onclick={() => (view = 'model')} class="px-2.5 py-1 rounded-lg text-sm font-bold {view === 'model' ? 'bg-accent/15 text-accent-light' : 'text-muted'}">Model's ideal</button>
          {:else}
            <h2 class="font-bold">Model's ideal XI · {rec.formation}</h2>
          {/if}
        </div>
        {#if view === 'your' && planValid}
          <span class="text-xs text-muted">{formationOf(planSquad, plan.starters)} · {planPoints(planSquad, plan)} xP · model ideal {rec.xi_expected}</span>
        {:else}
          <span class="text-xs text-muted">£{rec.squad_value}m · {rec.xi_expected} xP</span>
        {/if}
      </div>

      {#if view === 'your' && planValid}
        <Pitch starting={yourStarters} captainId={plan.captainId} viceId={plan.viceId} onpick={(p) => onpick(p.id)} />
        {#if yourCaptain && yourCaptain.id !== modelCaptain.id}
          <div class="mt-2 text-xs chip-warn rounded px-2 py-1">
            You've captained {yourCaptain.name} ({yourCaptain.next_gw_xp} xP) — the model prefers {modelCaptain.name} ({modelCaptain.next_gw_xp} xP).
          </div>
        {/if}
      {:else}
        <Pitch starting={rec.starting} captainId={rec.captain.id} viceId={rec.vice.id} onpick={(p) => onpick(p.id)} />
        {#if !planValid}
          <button onclick={() => onnav('planner')} class="mt-2 w-full text-xs text-accent-light hover:underline">
            → Build your own team in the Planner to see it here
          </button>
        {/if}
      {/if}
    </div>

    <!-- top captains -->
    <div class="card p-3">
      <h2 class="font-bold mb-2">Top captain picks</h2>
      <div class="divide-y divide-line/60">
        {#each topCaptains as p}
          <button onclick={() => onpick(p.id)} class="w-full flex items-center justify-between py-2 text-left hover:opacity-80">
            <span class="text-sm"><b>{p.name}</b> <span class="text-muted">{p.team}</span></span>
            <span class="font-bold text-brand-light tabular-nums">{p.next_gw_xp.toFixed(1)}</span>
          </button>
        {/each}
      </div>
    </div>
  </div>

  <div class="grid md:grid-cols-2 gap-4">
    <div class="card p-3">
      <h2 class="font-bold mb-2">Best value <span class="text-xs text-muted font-normal">(xP per £m)</span></h2>
      <div class="divide-y divide-line/60">
        {#each bestValue as p}
          <button onclick={() => onpick(p.id)} class="w-full flex items-center justify-between py-2 text-left hover:opacity-80">
            <span class="text-sm"><b>{p.name}</b> <span class="text-muted">{p.pos} · {p.team} · £{p.price.toFixed(1)}</span></span>
            <span class="tabular-nums"><span class="text-brand-light font-bold">{(p.next_gw_xp / p.price).toFixed(2)}</span> <span class="text-muted text-xs">{p.next_gw_xp.toFixed(1)}xP</span></span>
          </button>
        {/each}
      </div>
    </div>

    <div class="card p-3">
      <h2 class="font-bold mb-2">{formTitle}</h2>
      <div class="divide-y divide-line/60">
        {#each inForm as p}
          <div class="flex items-center justify-between py-2">
            <button onclick={() => onpick(p.id)} class="text-sm text-left hover:opacity-80"><b>{p.name}</b> <span class="text-muted">{p.pos} · {p.team}</span></button>
            <FixtureStrip fixtures={p.fixtures} max={4} />
          </div>
        {/each}
      </div>
    </div>
  </div>

  <!-- price watch -->
  <div class="card p-3">
    <h2 class="font-bold mb-2">💰 Price watch <span class="text-xs text-muted font-normal">(transfer momentum this GW)</span></h2>
    {#if !hasMarket}
      <p class="text-sm text-muted">No transfer activity yet — the market is quiet pre-season. This lights up with predicted risers &amp; fallers once the season is under way.</p>
    {:else}
      <div class="grid sm:grid-cols-2 gap-4">
        <div>
          <div class="text-xs uppercase text-brand-light font-bold mb-1">▲ Rising</div>
          {#each risers as p}
            <button onclick={() => onpick(p.id)} class="w-full flex justify-between py-1 text-sm hover:opacity-80"><span><b>{p.name}</b> <span class="text-muted">{p.team}</span></span><span class="text-brand tabular-nums">+{fmtK(p.net_transfers)}</span></button>
          {/each}
        </div>
        <div>
          <div class="text-xs uppercase text-red font-bold mb-1">▼ Falling</div>
          {#each fallers as p}
            <button onclick={() => onpick(p.id)} class="w-full flex justify-between py-1 text-sm hover:opacity-80"><span><b>{p.name}</b> <span class="text-muted">{p.team}</span></span><span class="text-red tabular-nums">{fmtK(p.net_transfers)}</span></button>
          {/each}
        </div>
      </div>
    {/if}
  </div>
</div>
