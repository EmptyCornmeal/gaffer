<script lang="ts">
  import type { Bundle } from '../lib/data'
  import type { Player, RecPlayer } from '../lib/types'
  import Pitch from '../components/Pitch.svelte'
  import FixtureStrip from '../components/FixtureStrip.svelte'
  import Icon from '../components/Icon.svelte'
  import { mdLite } from '../lib/mdlite'
  import { loadCurrent, lineupErrors, formationOf, planPoints, captainScore } from '../lib/squad'
  import { generateTeamBrief } from '../lib/teamBrief'
  import { briefingCaveat } from '../lib/squadStatus'
  import { renderTeamCard, downloadBlob, type SharePlayer } from '../lib/shareImage'

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

  // U17: the page answered "what do I do this week?" twice. The generated
  // briefing opens with `rec.summary` verbatim and repeats the captain rationale
  // as its first bullet (see ai/verdict.py `_template_briefing`), and a second
  // card printed both again 200px below it. One card owns the answer now, and it
  // renders the best source that exists: your Planner brief, else the verdict,
  // else the optimiser's own summary line.
  //
  // The order of that fallback is the load-bearing part. `verdict.json` is an
  // optional artifact that already degrades to `source: 'template'` and can be
  // absent entirely, so the branch that must never disappear is the last one —
  // which is why `rec.summary` is the floor of this card rather than a card of
  // its own that duplicates whatever sits above it.
  const brief = $derived((showYourBrief ? teamBrief : '') || verdict?.briefing_md || '')

  // Decided once so the chip, the caveat and the prose cannot disagree: a caveat
  // reading "your team" over the model's scratch squad is the exact failure
  // squadStatus.ts exists to prevent.
  const subject = $derived(showYourBrief && teamBrief ? 'plan' : 'model')
  const provenance = $derived(
    subject === 'plan'
      ? 'live'
      : brief && verdict?.source.startsWith('ai')
        ? 'AI'
        : 'auto',
  )

  // What the briefing is actually about. Decided from meta.squad_status, not
  // from the calendar: a mid-season fetch failure must caveat exactly as loudly
  // as pre-season does.
  const caveat = $derived(briefingCaveat(bundle.meta, subject))

  // EO-aware so the widget agrees with the model/verdict (the rank-safe armband,
  // e.g. Haaland), not a raw-points list that would omit him.
  const topCaptains = $derived([...P].sort((a, b) => captainScore(b) - captainScore(a)).slice(0, 5))
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

  // Template check: high-owned players the value-optimizer punts. The model
  // maximises points-per-£ and is EO-blind, so it can leave out a near-must-own
  // like Haaland — a big rank risk. Surface it honestly.
  const modelSquadIds = $derived(new Set([...rec.starting, ...rec.bench].map((p) => p.id)))
  const templateMissing = $derived(
    [...P]
      .filter((p) => p.owned_by >= 30 && !modelSquadIds.has(p.id) && p.p_start > 0.5)
      .sort((a, b) => b.owned_by - a.owned_by)
      .slice(0, 5),
  )

  // projected DEFCON hitters (defenders/mids likely to bank the +2)
  const defconWatch = $derived(
    [...P]
      .filter((p) => p.defcon && p.defcon.p_hit >= 0.2 && p.p_start > 0.5)
      .sort((a, b) => (b.defcon?.p_hit ?? 0) - (a.defcon?.p_hit ?? 0))
      .slice(0, 6),
  )
  // highest ceiling — the boom picks, ranked by 90th-percentile outcome
  const topCeiling = $derived(
    [...P]
      .filter((p) => p.dist && p.dist.ceiling > 0 && p.p_start > 0.5)
      .sort((a, b) => (b.dist?.ceiling ?? 0) - (a.dist?.ceiling ?? 0))
      .slice(0, 6),
  )

  const hasForm = $derived(P.some((p) => p.form > 0))
  const inForm = $derived(
    hasForm
      ? [...P].filter((p) => p.form >= 4).sort((a, b) => b.form - a.form).slice(0, 5)
      : [...P].filter((p) => p.p_start > 0.6).sort((a, b) => b.xgi90 - a.xgi90).slice(0, 5),
  )
  const formTitle = $derived(hasForm ? 'In form' : 'Top underlying threat')

  // U26: below the XI the page was seven interchangeable cards — same size, same
  // border, same title-plus-rows-plus-right-aligned-number — so nothing was
  // sized by importance and nothing read as important. They are shortlists you
  // consult, not the answer, so they now collapse to a 44px row whose summary
  // already carries the top entry's name and number. Shut, the lower half of the
  // page is a six-line index readable in one screen; open, nothing is missing.
  const DASH = '—'
  const lead = $derived({
    value: bestValue[0]
      ? `${bestValue[0].name} ${(bestValue[0].next_gw_xp / bestValue[0].price).toFixed(2)}`
      : DASH,
    form: inForm[0]
      ? `${inForm[0].name} ${hasForm ? inForm[0].form.toFixed(1) : `${inForm[0].xgi90.toFixed(2)} xGI`}`
      : DASH,
    ceiling: topCeiling[0] ? `${topCeiling[0].name} ${topCeiling[0].dist?.ceiling}` : DASH,
    defcon: defconWatch[0]
      ? `${defconWatch[0].name} ${Math.round((defconWatch[0].defcon?.p_hit ?? 0) * 100)}%`
      : 'nothing projected',
    template: templateMissing[0] ? `${templateMissing[0].name} ${templateMissing[0].owned_by}%` : DASH,
    market: risers[0]
      ? `▲ ${risers[0].name} +${fmtK(risers[0].net_transfers)}`
      : fallers[0]
        ? `▼ ${fallers[0].name} ${fmtK(fallers[0].net_transfers)}`
        : 'quiet pre-season',
  })

  // Read once, at mount, and deliberately never re-read: a phone wants the index,
  // a wide screen has the two columns to show everything, and a mid-session
  // resize must not slam shut a section the reader just opened. `typeof` rather
  // than a direct call because a test environment has no matchMedia.
  const wide = typeof matchMedia === 'function' && matchMedia('(min-width: 768px)').matches
  let open = $state({
    value: wide, form: wide, ceiling: wide, defcon: wide, template: wide, market: wide,
  })

  // Shareable image of whichever XI is on screen (your team or the model's).
  let sharing = $state(false)
  let toast = $state('')
  async function share() {
    sharing = true
    try {
      const isYour = view === 'your' && planValid
      const xi = isYour ? yourStarters : rec.starting
      const capId = isYour ? plan.captainId : rec.captain.id
      const viceId = isYour ? plan.viceId : rec.vice.id
      const players: SharePlayer[] = xi.map((p) => ({
        name: p.name, pos: p.pos, team: p.team, isC: p.id === capId, isVC: p.id === viceId,
      }))
      const pts = isYour ? planPoints(planSquad, plan) : rec.xi_expected
      const blob = await renderTeamCard({
        title: isYour ? 'My XI' : "The model's XI",
        subtitle: `${bundle.meta.gw_name ?? 'Gameweek'} · ${isYour ? formationOf(planSquad, plan.starters) : rec.formation} · ${pts} projected pts`,
        players,
      })
      downloadBlob(blob, 'gaffer-team.png')
      toast = 'Team image downloaded ↓'
      setTimeout(() => (toast = ''), 2600)
    } finally {
      sharing = false
    }
  }
</script>

<div class="flex flex-col gap-4 rise">
  <!-- The Gaffer's Verdict — the page's single answer to "what do I do this
       week?", and the only place that answer is allowed to appear. Always
       rendered: the fallback body is built from `rec`, a required artifact, so
       this card cannot vanish the way an optional verdict can. -->
  <div class="card p-4 border-brand/40 bg-brand/8">
    <div class="flex items-center justify-between mb-1">
      <div class="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-brand-light">
        <span class="flex items-center gap-1.5"><Icon name="zap" size={14} /> The Gaffer's Verdict</span>
        <span class="chip {subject === 'plan' ? 'chip-good' : 'chip-info'}">{subject === 'plan' ? 'your team' : 'model'}</span>
      </div>
      <span class="text-[10px] text-muted2">{provenance}</span>
    </div>

    <!-- Above the briefing, never below it: by the time you have read "no hit
         needed" it is too late to learn it was said about a squad that is not
         yours. -->
    {#if caveat}
      <div
        data-testid="briefing-caveat"
        role="note"
        class="mb-3 rounded-lg px-3 py-2 border {caveat.tone === 'unknown'
          ? 'border-yellow/40 bg-yellow/10'
          : 'border-line bg-bg3'}"
      >
        <p class="flex items-start gap-1.5 text-sm font-bold text-text">
          <Icon name="alert" size={15} class="mt-0.5 shrink-0 {caveat.tone === 'unknown' ? 'text-yellow' : 'text-muted'}" />
          <span>{caveat.headline}</span>
        </p>
        <p class="mt-1 text-[13px] text-muted leading-snug">{caveat.body}</p>
        {#if caveat.reason}
          <p class="mt-1 text-[11px] text-muted2">Why: {caveat.reason}.</p>
        {/if}
      </div>
    {/if}

    {#if brief}
      <div class="verdict text-[15px] leading-relaxed text-text">
        {@html mdLite(brief)}
      </div>
    {:else}
      <!-- No briefing was generated this run. The optimiser's own line is not a
           lesser answer — it is the sentence the briefing quotes — so it fills
           the same slot rather than returning as a second card. -->
      <p class="text-[11px] font-bold uppercase tracking-wider text-muted2">
        {rec.mode === 'build' ? 'Model squad this week' : 'This week'}
      </p>
      <p class="mt-1 text-[15px] leading-relaxed text-text">{rec.summary}</p>
      {#if rec.captain.rationale}
        <p class="mt-1.5 text-sm text-muted"><b class="text-brand-light">Captain {rec.captain.name}:</b> {rec.captain.rationale}</p>
      {/if}
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
        <div class="flex items-center gap-2">
          {#if view === 'your' && planValid}
            <span class="text-xs text-muted">{formationOf(planSquad, plan.starters)} · {planPoints(planSquad, plan)} xP · model ideal {rec.xi_expected}</span>
          {:else}
            <span class="text-xs text-muted">£{rec.squad_value}m · {rec.xi_expected} xP</span>
          {/if}
          <button onclick={share} disabled={sharing} title="Download a shareable image of this XI" class="text-xs text-muted hover:text-brand-light disabled:opacity-50 flex items-center gap-1">
            <Icon name="share" size={13} /> {sharing ? '…' : 'Share'}
          </button>
        </div>
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
      <div class="flex items-baseline justify-between mb-2">
        <h2 class="font-bold">Top captain picks</h2>
        <span class="text-[10px] text-muted2">ceiling · xP</span>
      </div>
      <div class="divide-y divide-line/60">
        {#each topCaptains as p}
          <button onclick={() => onpick(p.id)} class="w-full flex items-center justify-between py-2 text-left hover:opacity-80">
            <span class="text-sm min-w-0"><b>{p.name}</b> <span class="text-muted">{p.team}</span></span>
            <span class="flex items-center gap-2 tabular-nums shrink-0">
              {#if p.dist}<span class="text-[11px] text-muted">ceil {p.dist.ceiling}</span>{/if}
              <span class="font-bold text-brand-light w-8 text-right">{p.next_gw_xp.toFixed(1)}</span>
            </span>
          </button>
        {/each}
      </div>
    </div>
  </div>

  <!-- One header for every shortlist below. The collapsed state has to be worth
       reading on its own, so the summary carries the top row's name and number —
       otherwise this is a table of contents, not an answer. -->
  {#snippet head(icon: string, tint: string, title: string, note: string, top: string)}
    <Icon name={icon} size={15} class="shrink-0 {tint}" />
    <span class="font-bold text-sm shrink-0">{title}</span>
    <span class="hidden sm:block truncate text-[11px] text-muted font-normal">({note})</span>
    <span class="ml-auto flex items-center gap-2 min-w-0">
      <span class="truncate text-xs text-muted2 tabular-nums">{top}</span>
      <Icon name="chevron-down" size={15} class="chev shrink-0 text-muted2" />
    </span>
  {/snippet}

  <!-- Two labelled bands. The XI, the verdict and the armband above are the
       decision; everything from here down is a shortlist you go looking for, and
       splitting them by what they answer stops seven cards competing at one
       weight. Nothing is removed — it is one tap away and stays keyboard
       reachable, because a details/summary disclosure is native. -->
  <section class="flex flex-col gap-2" aria-labelledby="band-points">
    <h2 id="band-points" class="band">Where the points are</h2>

    <div class="grid md:grid-cols-2 gap-2 md:gap-4">
      <details class="card" bind:open={open.value}>
        <summary class="sec">{@render head('target', 'text-brand-light', 'Best value', 'xP per £m', lead.value)}</summary>
        <div class="px-3 pb-2 divide-y divide-line/60">
          {#each bestValue as p}
            <button onclick={() => onpick(p.id)} class="w-full flex items-center justify-between gap-2 py-2 text-left hover:opacity-80">
              <span class="text-sm min-w-0 truncate"><b>{p.name}</b> <span class="text-muted">{p.pos} · {p.team} · £{p.price.toFixed(1)}</span></span>
              <span class="tabular-nums shrink-0"><span class="text-brand-light font-bold">{(p.next_gw_xp / p.price).toFixed(2)}</span> <span class="text-muted text-xs">{p.next_gw_xp.toFixed(1)}xP</span></span>
            </button>
          {/each}
        </div>
      </details>

      <details class="card" bind:open={open.form}>
        <summary class="sec">{@render head('zap', 'text-brand-light', formTitle, hasForm ? 'form, last 30 days' : 'xGI per 90', lead.form)}</summary>
        <div class="px-3 pb-2 divide-y divide-line/60">
          {#each inForm as p}
            <div class="flex items-center justify-between gap-2 py-2">
              <button onclick={() => onpick(p.id)} class="text-sm text-left hover:opacity-80 min-w-0 truncate"><b>{p.name}</b> <span class="text-muted">{p.pos} · {p.team}</span></button>
              <FixtureStrip fixtures={p.fixtures} max={4} />
            </div>
          {/each}
        </div>
      </details>

      <details class="card" bind:open={open.ceiling}>
        <summary class="sec">{@render head('flame', 'text-brand-light', 'Highest ceiling', 'boom potential', lead.ceiling)}</summary>
        <div class="px-3 pb-2 divide-y divide-line/60">
          {#each topCeiling as p}
            <button onclick={() => onpick(p.id)} class="w-full flex items-center justify-between gap-2 py-2 text-left hover:opacity-80">
              <span class="text-sm min-w-0 truncate"><b>{p.name}</b> <span class="text-muted">{p.pos} · {p.team}</span></span>
              <span class="flex items-center gap-2 shrink-0 tabular-nums">
                <span class="text-[11px] text-muted2">{p.dist?.boom}% haul</span>
                <span class="font-bold text-brand-light w-8 text-right">{p.dist?.ceiling}</span>
              </span>
            </button>
          {/each}
        </div>
      </details>

      <details class="card" bind:open={open.defcon}>
        <summary class="sec">{@render head('shield', 'text-brand-light', 'DEFCON watch', 'projected +2 this GW', lead.defcon)}</summary>
        <div class="px-3 pb-2 divide-y divide-line/60">
          {#if defconWatch.length}
            {#each defconWatch as p}
              <button onclick={() => onpick(p.id)} class="w-full flex items-center justify-between gap-2 py-2 text-left hover:opacity-80">
                <span class="text-sm min-w-0 flex items-center gap-1.5"><b>{p.name}</b> <span class="text-muted">{p.pos} · {p.team}</span>{#if p.defcon?.near_hit}<span class="chip chip-warn">near-hit</span>{/if}</span>
                <span class="flex items-center gap-2 shrink-0 tabular-nums">
                  <span class="text-[11px] text-muted2">{p.defcon?.per90}/{p.defcon?.threshold}</span>
                  <span class="font-bold text-brand-light w-9 text-right">{Math.round((p.defcon?.p_hit ?? 0) * 100)}%</span>
                </span>
              </button>
            {/each}
          {:else}
            <p class="py-2 text-sm text-muted">No standout defensive-contribution picks this week.</p>
          {/if}
        </div>
      </details>
    </div>
  </section>

  <section class="flex flex-col gap-2" aria-labelledby="band-field">
    <h2 id="band-field" class="band">What the field is doing</h2>

    <div class="grid md:grid-cols-2 gap-2 md:gap-4">
      <!-- template check: high-owned picks the value model leaves out -->
      {#if templateMissing.length}
        <details class="card border-yellow/30" bind:open={open.template}>
          <summary class="sec">{@render head('users', 'text-yellow', 'Template check', 'popular picks the model leaves out', lead.template)}</summary>
          <div class="px-3 pb-2">
            <p class="text-xs text-muted mb-2">The model optimises points-per-£ and doesn't weigh ownership — so it punts these heavily-owned picks. If they haul (and you don't own them), your rank slips. Owning them is the safer play against the field; backing the model's alternatives is the differential bet.</p>
            <div class="divide-y divide-line/60">
              {#each templateMissing as p}
                <button onclick={() => onpick(p.id)} class="w-full flex items-center justify-between gap-2 py-2 text-left hover:opacity-80">
                  <span class="text-sm min-w-0 truncate"><b>{p.name}</b> <span class="text-muted">{p.pos} · {p.team} · £{p.price.toFixed(1)}</span></span>
                  <span class="flex items-center gap-3 shrink-0 tabular-nums">
                    {#if p.dist}<span class="text-[11px] text-muted2">ceil {p.dist.ceiling}</span>{/if}
                    <span class="text-[11px] text-muted">{p.next_gw_xp.toFixed(1)} xP</span>
                    <span class="font-bold text-yellow w-12 text-right">{p.owned_by}%</span>
                  </span>
                </button>
              {/each}
            </div>
          </div>
        </details>
      {/if}

      <details class="card" bind:open={open.market}>
        <summary class="sec">{@render head('chart', 'text-brand-light', 'Price watch', 'transfer momentum this GW', lead.market)}</summary>
        <div class="px-3 pb-3">
          {#if !hasMarket}
            <p class="text-sm text-muted">No transfer activity yet — the market is quiet pre-season. This lights up with predicted risers &amp; fallers once the season is under way.</p>
          {:else}
            <div class="grid sm:grid-cols-2 gap-4">
              <div>
                <div class="text-xs uppercase text-brand-light font-bold mb-1">▲ Rising</div>
                {#each risers as p}
                  <button onclick={() => onpick(p.id)} class="w-full flex justify-between gap-2 py-1 text-sm hover:opacity-80"><span class="min-w-0 truncate"><b>{p.name}</b> <span class="text-muted">{p.team}</span></span><span class="text-brand tabular-nums shrink-0">+{fmtK(p.net_transfers)}</span></button>
                {/each}
              </div>
              <div>
                <div class="text-xs uppercase text-red font-bold mb-1">▼ Falling</div>
                {#each fallers as p}
                  <button onclick={() => onpick(p.id)} class="w-full flex justify-between gap-2 py-1 text-sm hover:opacity-80"><span class="min-w-0 truncate"><b>{p.name}</b> <span class="text-muted">{p.team}</span></span><span class="text-red tabular-nums shrink-0">{fmtK(p.net_transfers)}</span></button>
                {/each}
              </div>
            </div>
          {/if}
        </div>
      </details>
    </div>
  </section>
</div>

{#if toast}
  <div class="fixed left-1/2 -translate-x-1/2 z-50 rounded-full bg-brand text-[#05210f] text-sm font-semibold px-4 py-2 shadow-lg rise"
    style="bottom: calc(var(--gaffer-bottomnav, 0px) + env(safe-area-inset-bottom) + 1.25rem);" role="status">{toast}</div>
{/if}

<style>
  /* Band label: a rule that runs to the edge, not another card. The bands exist
     to break the wall of identical cards, so they must not look like one. */
  .band {
    display: flex; align-items: center; gap: 0.6rem;
    margin: 0.25rem 0 0;
    font-size: 11px; font-weight: 700; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--color-muted2);
  }
  .band::after { content: ''; flex: 1; height: 1px; background: var(--color-line); }

  /* The whole 44px row is the target, not the 14px triangle. The native marker
     cannot be laid out inside a flex row, so it is removed in both engines and
     redrawn as the chevron, which is the only thing that then needs animating. */
  .sec {
    display: flex; align-items: center; gap: 0.5rem;
    min-height: 44px; padding: 0 0.75rem;
    cursor: pointer; list-style: none; -webkit-tap-highlight-color: transparent;
  }
  .sec::-webkit-details-marker { display: none; }
  .sec :global(.chev) { transition: transform 0.15s ease; }
  details[open] > .sec :global(.chev) { transform: rotate(180deg); }
</style>
