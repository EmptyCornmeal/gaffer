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
  const modelPlan = $derived(bundle.plan) // the model's optimal multi-GW transfer path
  const planIsBuild = $derived(modelPlan?.mode === 'build')
  let showBlueprint = $state(false) // expand the initial XV in the model plan
  // Pair each week's outgoings with incomings into readable "out → in" swaps.
  function swapsOf(step: { transfers_out: RecPlayer[]; transfers_in: RecPlayer[] }) {
    const n = Math.max(step.transfers_out.length, step.transfers_in.length)
    return Array.from({ length: n }, (_, i) => ({
      out: step.transfers_out[i] ?? null,
      inp: step.transfers_in[i] ?? null,
    }))
  }
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
  // Availability watch: planned players who are flagged (injury/suspension, status
  // ≠ available) or a rotation risk (xMins badge 'bad'). A plan that leans on a
  // doubtful asset should say so before the deadline.
  const flagged = $derived(
    squad
      .filter((p) => (p.status && p.status !== 'a') || p.xmins_badge?.kind === 'bad')
      .map((p) => ({
        p,
        reason: p.status && p.status !== 'a'
          ? (p.news?.trim() || 'flagged — check status')
          : 'rotation risk',
        starting: plan.starters.includes(p.id),
      })),
  )

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
  // Ownership weighting is currently neutralised, so all three stances solve to
  // byte-identical squads — verified in the artifact: squad, XI, bench, captain
  // and headline match across differential/balanced/template at every horizon.
  // The pipeline already knew and said so in `risk_note`; nothing rendered it,
  // so the toggle looked like a live choice. Showing the note is the honest
  // interim — the control stays wired for when placing objectives land (T-17),
  // but it stops claiming to change an answer it does not change. See G13.
  const riskNote = $derived(
    bundle.recommendation.by_horizon?.[String(optimalHorizon)]?.risk_note ?? '',
  )

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
  // Clear throws away a squad that took minutes to assemble, and it used to sit
  // in the same row of identical pills as the horizon switches. Arm on the first
  // press, act on the second, and disarm shortly after so it never stays cocked.
  let clearArmed = $state(false)
  let disarm: ReturnType<typeof setTimeout> | undefined
  function clearAll() {
    clearTimeout(disarm)
    if (!clearArmed) {
      clearArmed = true
      disarm = setTimeout(() => (clearArmed = false), 4000)
      return
    }
    clearArmed = false
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

<div class="grid grid-cols-1 lg:grid-cols-2 gap-4 rise">
  <!-- LEFT: your squad -->
  <div class="flex flex-col gap-3 min-w-0">
    {#if modelPlan && modelPlan.steps?.length}
      <div class="card p-3">
        <div class="flex items-baseline justify-between mb-1 gap-2 flex-wrap">
          <h2 class="font-bold flex items-center gap-2">{planIsBuild ? "Model blueprint" : 'Your transfer plan'}
            <span class="text-xs text-muted font-normal">next {modelPlan.steps.length} GWs · {modelPlan.total_expected} pts</span>
          </h2>
          <span class="text-micro chip {planIsBuild ? 'chip-info' : 'chip-good'}">{planIsBuild ? 'not your team' : 'from your team'}</span>
        </div>
        {#if planIsBuild}
          <p class="text-mini text-accent-light bg-accent/8 border border-accent/25 rounded-lg px-2.5 py-1.5 mb-2">
            How the model would <b>build a squad from scratch and evolve it</b> — a reference, <b>not</b> based on your imported team below. It shows off your real picks once your FPL squad locks at the GW1 deadline.
          </p>
        {/if}
        <div class="space-y-1">
          {#each modelPlan.steps as s, si}
            <div class="flex items-start gap-2 py-0.5">
              <span class="text-mini font-bold text-muted2 w-9 shrink-0 pt-1">GW{s.gw}</span>
              <div class="flex-1 min-w-0">
                {#if si === 0 && planIsBuild}
                  <button onclick={() => (showBlueprint = !showBlueprint)} class="text-xs text-text hover:text-brand-light flex items-center gap-1">
                    Draft the starting XV
                    <span class="text-muted2">{showBlueprint ? '▴ hide' : '▾ show'}</span>
                  </button>
                  {#if showBlueprint}
                    <div class="mt-1 flex flex-wrap gap-1">
                      {#each [...s.starting, ...s.bench] as p}
                        <button onclick={() => onpick(p.id)} class="text-mini px-1.5 py-0.5 rounded bg-bg3 text-muted hover:text-text">{p.name}</button>
                      {/each}
                    </div>
                  {/if}
                {:else if s.transfers_in.length}
                  <div class="flex flex-wrap items-center gap-x-1 gap-y-1">
                    {#each swapsOf(s) as sw}
                      <span class="inline-flex items-center gap-1 rounded-md bg-bg3 px-1.5 py-0.5 text-mini">
                        {#if sw.out}<button onclick={() => onpick(sw.out.id)} class="text-red hover:opacity-80">{sw.out.name}</button>{/if}
                        <span class="text-muted2">→</span>
                        {#if sw.inp}<button onclick={() => onpick(sw.inp.id)} class="text-brand-light hover:opacity-80">{sw.inp.name}</button>{/if}
                      </span>
                    {/each}
                    {#if s.hits}<span class="chip chip-bad">−{s.hits * 4} hit</span>{/if}
                  </div>
                {:else}
                  <span class="text-xs text-muted2">Roll — bank the free transfer <span class="text-muted">(now {s.free_transfers})</span></span>
                {/if}
              </div>
              <span class="text-mini tabular-nums text-brand-light shrink-0 pt-0.5" title="Captain + projected XI points">(C) {s.captain.name} · {s.xi_expected}</span>
            </div>
          {/each}
        </div>
        <p class="text-micro text-muted2 mt-2 pt-2 border-t border-line/60">The optimal <b>sequence</b> — when to swap, bank a free transfer, or take a −4 — maximising points over the window (a banked transfer is worth ~1.5 pts, so a swap must beat that).</p>
      </div>
    {/if}
    <div class="card p-3">
      <h2 class="font-bold">Squad Planner</h2>
      <p class="text-mini text-muted mb-2">Import your real squad, plan transfers for the next GW, then compare its projection to the model.</p>

      <!-- Eight controls used to share one row of lookalike pills, so a filter, a
           mode and a verb were indistinguishable. Three treatments now: a sunken
           panel fences everything that REPLACES the 15 (the window switches read
           as filters while quietly overwriting the squad), a plain ghost button
           carries the one edit that does not, and the one that destroys work is
           red and asks twice. -->
      <div class="rounded-xl border border-line2 bg-bg/50 p-2">
        <div class="flex items-center gap-x-2 gap-y-1 flex-wrap mb-1.5">
          <span class="text-micro uppercase tracking-wider font-bold text-muted">Start from</span>
          <span class="text-micro text-muted2">— replaces all 15</span>
          <button class="btn text-xs ml-auto" onclick={importTeam}>Import my team</button>
        </div>
        <div class="grid gap-2 sm:grid-cols-2">
          <div>
            <div class="text-micro uppercase tracking-wide font-bold text-muted2 mb-1">Model optimal · window</div>
            <div class="flex gap-0.5 rounded-lg border border-line bg-bg2 p-0.5" title="Load the model's optimal squad for a planning window">
              {#each HORIZONS as o}
                <button
                  onclick={() => loadOptimal(o.h, optimalRisk)}
                  class="flex-1 min-w-0 px-1.5 py-1 rounded-md text-xs font-bold transition
                    {shownOptimal && optimalHorizon === o.h ? 'bg-brand text-[#05210f]' : 'text-muted hover:text-text'}"
                >{o.label}</button>
              {/each}
            </div>
          </div>
          <div>
            <div class="text-micro uppercase tracking-wide font-bold text-muted2 mb-1">Risk stance</div>
            <div class="flex gap-0.5 rounded-lg border border-line bg-bg2 p-0.5" title="Risk stance: differential chases value, template owns the crowd for rank safety">
              {#each RISKS as o}
                <button
                  onclick={() => loadOptimal(optimalHorizon, o.key)}
                  class="flex-1 min-w-0 px-1 py-1 rounded-md text-xs font-bold transition
                    {shownOptimal && optimalRisk === o.key ? 'bg-brand text-[#05210f]' : 'text-muted hover:text-text'}"
                >{o.label}</button>
              {/each}
            </div>
            {#if riskNote}
              <p class="text-micro leading-snug text-muted2 mt-1">{riskNote}</p>
            {/if}
          </div>
        </div>
      </div>
      {#if importMsg}<div class="text-xs chip-info rounded px-2 py-1 mt-2">{importMsg}</div>{/if}

      <div class="flex items-center gap-2 flex-wrap mt-2 mb-1">
        <button class="btn-ghost btn text-xs" onclick={autofill} disabled={squad.length < 11} title="Pick the best starting XI + captain from your current 15 (doesn't change your squad)">Auto-pick XI</button>
        <button
          onclick={clearAll}
          disabled={!squad.length}
          title="Remove every player from this plan"
          class="ml-auto rounded-lg border px-3 py-[7px] text-xs font-bold transition disabled:opacity-30 disabled:cursor-not-allowed
            {clearArmed ? 'bg-red/25 border-red text-text' : 'border-red/40 text-red hover:bg-red/10'}"
        >{clearArmed ? `Discard ${squad.length}?` : 'Clear squad'}</button>
      </div>

      <!-- budget + counts -->
      <div class="flex items-center gap-3 text-sm mb-1 min-w-0">
        <span class="text-muted shrink-0">Budget</span>
        <div class="flex-1 min-w-0 h-2 rounded-full bg-bg3 overflow-hidden">
          <div class="h-full {t.cost > BUDGET ? 'bg-red' : 'bg-brand'}" style="width:{Math.min(100, (t.cost / BUDGET) * 100)}%"></div>
        </div>
        <span class="tabular-nums shrink-0 whitespace-nowrap {BUDGET - t.cost < 0 ? 'text-red' : ''}">£{t.cost.toFixed(1)} / {BUDGET}m</span>
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

      {#if flagged.length}
        <div class="mt-2 text-xs chip-warn rounded px-2 py-1.5">
          <div class="font-bold mb-0.5">⚠ {flagged.length} availability {flagged.length === 1 ? 'flag' : 'flags'} in your squad{flagged.some((f) => f.starting) ? ` · ${flagged.filter((f) => f.starting).length} starting` : ''}</div>
          {#each flagged as f}
            <div class="leading-tight">
              <span class="font-semibold">{f.p.name}</span>{f.starting ? ' (XI)' : ' (bench)'} — {f.reason}
            </div>
          {/each}
        </div>
      {/if}

      <!-- save / load plans -->
      <div class="mt-3 flex items-center gap-2 flex-wrap">
        <input bind:value={planName} placeholder="Plan name" aria-label="Plan name" class="flex-1 min-w-[7rem] rounded-lg bg-bg2 border border-line px-3 py-1.5 text-sm focus:outline-none focus:border-accent" />
        <button class="btn text-xs shrink-0" onclick={doSave}>Save plan</button>
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
          <button onclick={() => setCaptain(p.id)} title="captain" class="w-6 h-6 rounded-full text-micro font-black {plan.captainId === p.id ? 'bg-brand text-[#05210f]' : 'bg-bg3 text-muted'}">C</button>
          <button onclick={() => setVice(p.id)} title="vice" class="w-6 h-6 rounded-full text-micro font-black {plan.viceId === p.id ? 'bg-accent text-white' : 'bg-bg3 text-muted'}">V</button>
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
  <div class="flex flex-col gap-3 min-w-0">
    <div class="flex flex-wrap gap-2">
      <input bind:value={query} placeholder="Add players… (accent-insensitive)" class="flex-1 min-w-[8rem] rounded-lg bg-card border border-line px-3 py-1.5 text-sm focus:outline-none focus:border-accent" />
      <div class="flex flex-wrap gap-1">
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
            <div class="text-micro text-muted">{p.pos} · {p.team} · £{p.price.toFixed(1)} · {p.owned_by}%</div>
          </button>
          <span class="text-brand-light font-bold tabular-nums text-sm w-8 text-right">{p.next_gw_xp.toFixed(1)}</span>
          <button onclick={() => add(p)} disabled={!!blocker} title={blocker ?? 'add'} class="btn text-xs py-1 px-2 disabled:opacity-30 disabled:cursor-not-allowed">+</button>
        </div>
      {/each}
    </div>
  </div>
</div>
