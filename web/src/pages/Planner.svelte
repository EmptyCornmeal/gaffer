<script lang="ts">
  import type { Bundle } from '../lib/data'
  import type { OptimalHorizon, Player, Pos, RecPlayer, RiskStance } from '../lib/types'
  import {
    QUOTA, BUDGET, totals, addBlocker, squadValidity, autoLineup, lineupErrors,
    formationOf, planPoints, loadCurrent, saveCurrent, listPlans, savePlan, deletePlan,
    planFromPicks, type Plan,
  } from '../lib/squad'
  import { matches } from '../lib/search'
  import { fpl } from '../lib/fpl'
  import { getEntryId } from '../lib/config'
  import Pitch from '../components/Pitch.svelte'
  import Crest from '../components/Crest.svelte'

  let { bundle, onpick }: { bundle: Bundle; onpick: (id: number) => void } = $props()

  const byId = $derived(new Map(bundle.players.map((p) => [p.id, p])))
  const initialPlan = loadCurrent()
  let plan = $state<Plan>(initialPlan)
  let plans = $state<Plan[]>(listPlans())
  let planName = $state(initialPlan.name)

  const squad = $derived(plan.ids.map((id) => byId.get(id)).filter((p): p is Player => !!p))
  const t = $derived(totals(squad))
  const squadErrs = $derived(squadValidity(squad))
  const starters = $derived(squad.filter((p) => plan.starters.includes(p.id)))
  const bench = $derived(squad.filter((p) => !plan.starters.includes(p.id)))
  const xiErrs = $derived(lineupErrors(squad, plan.starters))
  const isValid = $derived(squad.length === 15 && squadErrs.length === 0 && xiErrs.length === 0)

  $effect(() => saveCurrent(plan))

  function autofill() {
    const a = autoLineup(squad)
    plan = { ...plan, ...a }
  }
  function add(p: Player) {
    if (addBlocker(squad, p)) return
    const ids = [...plan.ids, p.id]
    plan = { ...plan, ids }
    if (ids.length === 15 && plan.starters.length === 0) {
      // fill lineup on next tick once squad derived updates
      queueMicrotask(() => (plan = { ...plan, ...autoLineup(ids.map((id) => byId.get(id)!).filter(Boolean)) }))
    }
  }
  function remove(id: number) {
    plan = {
      ...plan,
      ids: plan.ids.filter((x) => x !== id),
      starters: plan.starters.filter((x) => x !== id),
      captainId: plan.captainId === id ? -1 : plan.captainId,
      viceId: plan.viceId === id ? -1 : plan.viceId,
    }
  }
  function toggleStart(id: number) {
    const isStarter = plan.starters.includes(id)
    if (isStarter) {
      plan = {
        ...plan,
        starters: plan.starters.filter((x) => x !== id),
        captainId: plan.captainId === id ? -1 : plan.captainId,
        viceId: plan.viceId === id ? -1 : plan.viceId,
      }
    } else {
      plan = { ...plan, starters: [...plan.starters, id] }
    }
  }
  function setCaptain(id: number) {
    if (!plan.starters.includes(id)) return
    plan = { ...plan, captainId: id, viceId: plan.viceId === id ? plan.captainId : plan.viceId }
  }
  function setVice(id: number) {
    if (!plan.starters.includes(id)) return
    plan = { ...plan, viceId: id, captainId: plan.captainId === id ? plan.viceId : plan.captainId }
  }
  // Planning window for "Load optimal": 1 = this GW, 3 = next 3, 5 = next 5. Each
  // is a distinct server-side solve (future GWs decayed), so the squad shifts with
  // the horizon you care about.
  const HORIZONS = [
    { h: 1, label: 'This GW' },
    { h: 3, label: 'Next 3' },
    { h: 5, label: 'Next 5' },
  ]
  // Risk stance = the effective-ownership dial. Differential chases points-per-£
  // (may drop the crowd's premiums); template owns the crowd for rank safety.
  const RISKS: { key: RiskStance; label: string }[] = [
    { key: 'differential', label: 'Differential' },
    { key: 'balanced', label: 'Balanced' },
    { key: 'template', label: 'Template' },
  ]
  let optimalHorizon = $state(3)
  let optimalRisk = $state<RiskStance>('balanced')
  let shownOptimal = $state<OptimalHorizon | null>(null)

  function loadOptimal(h = optimalHorizon, r = optimalRisk) {
    optimalHorizon = h
    optimalRisk = r
    const oh = bundle.recommendation.by_horizon?.[String(h)]?.by_risk?.[r]
    const src = oh ?? bundle.recommendation
    // Use the solver's exact XI, bench order and captain/vice — don't let the
    // frontend auto-lineup re-pick them (it would re-captain by raw xP and lose
    // the model's EO-aware captain, e.g. flip Haaland back to Bruno).
    const starters = src.starting.map((p) => p.id)
    const bench = src.bench.map((p) => p.id)
    const ids = [...starters, ...bench]
    plan = {
      ...plan,
      ids,
      starters,
      captainId: src.captain?.id ?? -1,
      viceId: src.vice?.id ?? -1,
    }
    shownOptimal = oh ?? null
  }

  let importMsg = $state('')
  async function importTeam() {
    const entry = getEntryId()
    if (!entry || !fpl.configured()) {
      importMsg = 'Add your Entry ID + a proxy in Settings first.'
      return
    }
    importMsg = 'Importing…'
    const gw = Number(bundle.meta.last_finished_gw || bundle.meta.current_gw || 1)
    try {
      const picks = await fpl.picks(entry, gw)
      plan = planFromPicks(picks.picks, 'My team')
      planName = 'My team'
      importMsg = `Imported your GW${gw} squad — now plan your transfers.`
    } catch {
      importMsg = 'Picks unavailable yet — they appear once the GW1 deadline passes.'
    }
  }
  function clearAll() {
    plan = { name: planName, ids: [], starters: [], captainId: -1, viceId: -1 }
  }
  function doSave() {
    const name = planName.trim() || `Plan ${plans.length + 1}`
    const saved = { ...plan, name }
    savePlan(saved)
    plan = saved
    planName = name
    plans = listPlans()
  }
  function loadPlan(p: Plan) {
    plan = { ...p }
    planName = p.name
  }
  function removePlan(name: string) {
    deletePlan(name)
    plans = listPlans()
  }

  // Render the model's **bold** markers in explanation bullets, escaping the rest.
  function mdBold(s: string): string {
    const esc = s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    return esc.replace(/\*\*(.+?)\*\*/g, '<strong class="text-text">$1</strong>')
  }

  function toRec(p: Player): RecPlayer {
    return { id: p.id, code: p.code, team_code: p.team_code, name: p.name, team: p.team, pos: p.pos, price: p.price, next_gw_xp: p.next_gw_xp, confidence: p.confidence }
  }

  // picker
  let query = $state('')
  let pickPos = $state<'ALL' | Pos>('ALL')
  const picker = $derived(
    bundle.players
      .filter((p) => pickPos === 'ALL' || p.pos === pickPos)
      .filter((p) => matches(p, query))
      .slice()
      .sort((a, b) => b.next_gw_xp - a.next_gw_xp)
      .slice(0, 60),
  )

  const posOrder: Record<Pos, number> = { GKP: 0, DEF: 1, MID: 2, FWD: 3 }
  const sortedStart = $derived([...starters].sort((a, b) => posOrder[a.pos] - posOrder[b.pos]))
</script>

<div class="grid lg:grid-cols-2 gap-4 rise">
  <!-- LEFT: your squad -->
  <div class="flex flex-col gap-3">
    <div class="card p-3">
      <div class="flex items-center justify-between mb-1 gap-2 flex-wrap">
        <h2 class="font-bold">Squad Planner</h2>
        <div class="flex gap-2 flex-wrap">
          <button class="btn text-xs" onclick={importTeam}>Import my team</button>
          <div class="inline-flex items-center gap-0.5 rounded-lg border border-line bg-bg2 p-0.5" title="Load the model's optimal squad for a planning window">
            <span class="text-[10px] uppercase text-muted px-1.5 font-bold">Optimal</span>
            {#each HORIZONS as o}
              <button
                onclick={() => loadOptimal(o.h, optimalRisk)}
                class="px-2 py-1 rounded-md text-xs font-bold transition
                  {shownOptimal && optimalHorizon === o.h ? 'bg-brand text-[#05210f]' : 'text-muted hover:text-text'}"
              >{o.label}</button>
            {/each}
          </div>
          <div class="inline-flex items-center gap-0.5 rounded-lg border border-line bg-bg2 p-0.5" title="Risk stance: differential chases value, template owns the crowd for rank safety">
            {#each RISKS as o}
              <button
                onclick={() => loadOptimal(optimalHorizon, o.key)}
                class="px-2 py-1 rounded-md text-xs font-bold transition
                  {shownOptimal && optimalRisk === o.key ? 'bg-accent text-white' : 'text-muted hover:text-text'}"
              >{o.label}</button>
            {/each}
          </div>
          <button class="btn-ghost btn text-xs" onclick={autofill} disabled={squad.length < 11}>Auto-pick XI</button>
          <button class="btn-ghost btn text-xs" onclick={clearAll}>Clear</button>
        </div>
      </div>
      <p class="text-[11px] text-muted mb-2">Import your real squad, plan transfers for the next GW, then compare its projection to the model.</p>
      {#if importMsg}<div class="text-xs chip-info rounded px-2 py-1 mb-2">{importMsg}</div>{/if}

      <!-- budget + counts -->
      <div class="flex items-center gap-3 text-sm mb-1">
        <span class="text-muted">Budget</span>
        <div class="flex-1 h-2 rounded-full bg-bg3 overflow-hidden">
          <div class="h-full {t.cost > BUDGET ? 'bg-red' : 'bg-brand'}" style="width:{Math.min(100, (t.cost / BUDGET) * 100)}%"></div>
        </div>
        <span class="tabular-nums {BUDGET - t.cost < 0 ? 'text-red' : ''}">£{t.cost.toFixed(1)} / {BUDGET}m</span>
      </div>
      <div class="flex gap-3 text-xs text-muted">
        {#each ['GKP', 'DEF', 'MID', 'FWD'] as pos}
          <span class="{t.byPos[pos as Pos] === QUOTA[pos as Pos] ? 'text-brand-light' : ''}">{pos} {t.byPos[pos as Pos]}/{QUOTA[pos as Pos]}</span>
        {/each}
        <span class="ml-auto">{t.count}/15</span>
      </div>

      {#if isValid}
        <div class="mt-2 text-xs chip-good rounded px-2 py-1">Valid · {formationOf(squad, plan.starters)} · projected {planPoints(squad, plan)} pts · (C) {byId.get(plan.captainId)?.name ?? '—'}</div>
      {:else if squad.length === 15}
        <div class="mt-2 text-xs chip-warn rounded px-2 py-1">{[...squadErrs, ...xiErrs].join(' · ')}</div>
      {:else}
        <div class="mt-2 text-xs chip-warn rounded px-2 py-1">Add players ({squad.length}/15){squadErrs.length ? ' · ' + squadErrs.join(' · ') : ''}</div>
      {/if}

      <!-- save / load plans -->
      <div class="mt-3 flex items-center gap-2">
        <input bind:value={planName} placeholder="Plan name" class="flex-1 rounded-lg bg-bg2 border border-line px-3 py-1.5 text-sm focus:outline-none focus:border-accent" />
        <button class="btn text-xs" onclick={doSave}>Save plan</button>
      </div>
      {#if plans.length}
        <div class="mt-2 flex flex-wrap gap-1.5">
          {#each plans as p}
            <span class="chip chip-info flex items-center gap-1">
              <button onclick={() => loadPlan(p)} class="hover:underline">{p.name}</button>
              <button onclick={() => removePlan(p.name)} class="text-red">×</button>
            </span>
          {/each}
        </div>
      {/if}
    </div>

    {#if shownOptimal}
      <div class="card p-3 border-brand/30">
        <div class="flex items-start justify-between gap-2 mb-1">
          <h3 class="font-bold text-sm">Why this squad <span class="text-muted font-normal">· {shownOptimal.label} · <span class="capitalize">{shownOptimal.risk}</span></span></h3>
          <button class="text-muted text-sm leading-none hover:text-text" onclick={() => (shownOptimal = null)} aria-label="dismiss explanation">✕</button>
        </div>
        <p class="text-xs text-brand-light mb-2">{shownOptimal.explanation.headline}</p>
        <ul class="space-y-1.5 text-xs text-muted">
          {#each shownOptimal.explanation.bullets as b}
            <li class="flex gap-2"><span class="text-brand-light shrink-0">›</span><span>{@html mdBold(b)}</span></li>
          {/each}
        </ul>
      </div>
    {/if}

    {#if starters.length}
      <div class="card p-3">
        <Pitch starting={sortedStart.map(toRec)} captainId={plan.captainId} viceId={plan.viceId} onpick={(p) => onpick(p.id)} />
      </div>
    {/if}

    <!-- Starting XI -->
    <div class="card">
      <div class="px-3 py-2 text-xs font-bold uppercase text-muted border-b border-line">Starting XI ({starters.length}/11)</div>
      {#each sortedStart as p (p.id)}
        <div class="flex items-center gap-2 px-3 py-1.5 border-b border-line/50">
          <Crest code={p.team_code} short={p.team} size={20} />
          <button onclick={() => onpick(p.id)} class="flex-1 text-left text-sm hover:opacity-80 truncate"><b>{p.name}</b> <span class="text-muted">{p.pos} · £{p.price.toFixed(1)}</span></button>
          <span class="text-brand-light font-bold tabular-nums text-sm w-8 text-right">{p.next_gw_xp.toFixed(1)}</span>
          <button onclick={() => setCaptain(p.id)} title="captain" class="w-6 h-6 rounded-full text-[10px] font-black {plan.captainId === p.id ? 'bg-brand text-[#05210f]' : 'bg-bg3 text-muted'}">C</button>
          <button onclick={() => setVice(p.id)} title="vice" class="w-6 h-6 rounded-full text-[10px] font-black {plan.viceId === p.id ? 'bg-accent text-white' : 'bg-bg3 text-muted'}">V</button>
          <button onclick={() => toggleStart(p.id)} title="move to bench" class="text-xs text-muted hover:text-text px-1">▼</button>
          <button onclick={() => remove(p.id)} class="text-red text-lg leading-none px-1" aria-label="remove">×</button>
        </div>
      {/each}
      {#if !starters.length}<div class="p-4 text-center text-muted text-sm">No XI yet — add 15 then Auto-pick, or start players from the bench below.</div>{/if}
    </div>

    <!-- Bench -->
    <div class="card">
      <div class="px-3 py-2 text-xs font-bold uppercase text-muted border-b border-line">Bench ({bench.length})</div>
      {#each bench as p (p.id)}
        <div class="flex items-center gap-2 px-3 py-1.5 border-b border-line/50">
          <Crest code={p.team_code} short={p.team} size={20} />
          <button onclick={() => onpick(p.id)} class="flex-1 text-left text-sm hover:opacity-80 truncate"><b>{p.name}</b> <span class="text-muted">{p.pos} · £{p.price.toFixed(1)}</span></button>
          <span class="text-brand-light font-bold tabular-nums text-sm w-8 text-right">{p.next_gw_xp.toFixed(1)}</span>
          <button onclick={() => toggleStart(p.id)} title="move to XI" class="text-xs text-brand-light hover:text-brand px-1">▲</button>
          <button onclick={() => remove(p.id)} class="text-red text-lg leading-none px-1" aria-label="remove">×</button>
        </div>
      {/each}
      {#if !bench.length}<div class="p-3 text-center text-muted2 text-xs">—</div>{/if}
    </div>
  </div>

  <!-- RIGHT: picker -->
  <div class="flex flex-col gap-3">
    <div class="flex gap-2">
      <input bind:value={query} placeholder="Add players… (accent-insensitive)" class="flex-1 rounded-lg bg-card border border-line px-3 py-1.5 text-sm focus:outline-none focus:border-accent" />
      <div class="flex gap-1">
        {#each ['ALL', 'GKP', 'DEF', 'MID', 'FWD'] as p}
          <button onclick={() => (pickPos = p as 'ALL' | Pos)} class="px-2 py-1 rounded-lg text-xs font-bold border {pickPos === p ? 'bg-accent/15 text-accent-light border-accent/40' : 'bg-card border-line text-muted'}">{p}</button>
        {/each}
      </div>
    </div>

    <div class="card overflow-y-auto max-h-[78vh]">
      {#each picker as p (p.id)}
        {@const blocker = addBlocker(squad, p)}
        <div class="flex items-center gap-2 px-3 py-1.5 border-b border-line/50">
          <Crest code={p.team_code} short={p.team} size={22} />
          <button onclick={() => onpick(p.id)} class="flex-1 min-w-0 text-left hover:opacity-80">
            <div class="text-sm font-semibold truncate">{p.full_name || p.name}</div>
            <div class="text-[10px] text-muted">{p.pos} · {p.team} · £{p.price.toFixed(1)} · {p.owned_by}%</div>
          </button>
          <span class="text-brand-light font-bold tabular-nums text-sm w-8 text-right">{p.next_gw_xp.toFixed(1)}</span>
          <button onclick={() => add(p)} disabled={!!blocker} title={blocker ?? 'add'} class="btn text-xs py-1 px-2 disabled:opacity-30 disabled:cursor-not-allowed">+</button>
        </div>
      {/each}
    </div>
  </div>
</div>
