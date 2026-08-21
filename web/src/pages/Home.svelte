<script lang="ts">
  import type { Bundle } from '../lib/data'
  import {
    parseDecision, ACTION_LABELS, ACTION_TONE, signed, pctOf, money,
    type Card,
  } from '../lib/weekly'
  import { classifyFreshness } from '../lib/freshness'
  import { deadlineState } from '../lib/data'
  import Icon from '../components/Icon.svelte'
  import Crest from '../components/Crest.svelte'

  let { bundle, onnav, onpick, now = Date.now() }: {
    bundle: Bundle
    onnav: (r: string) => void
    onpick: (id: number) => void
    now?: number
  } = $props()

  const parsed = $derived(parseDecision(bundle.decision))
  const d = $derived(parsed.kind === 'ok' ? parsed.data : null)
  const body = $derived(d?.decision ?? null)
  const meta = $derived(bundle.meta)
  // Deadline-aware: near a deadline, and after one, plain age is the wrong
  // question. See lib/freshness.ts.
  const fresh = $derived(classifyFreshness(meta?.generated_at, now, meta?.deadline))
  const dl = $derived(meta?.deadline ? deadlineState(meta.deadline, now) : null)
  const cmp = $derived(body?.comparison ?? null)
  const exe = $derived(body?.executability ?? null)
  const chip = $derived((d?.chip ?? null) as { recommendation?: string; reason?: string; expected_gain?: number } | null)
  // Whether the squad below is the owner's or one the optimiser invented. The
  // artifact says so directly; `action` does not — an unavailable recommendation
  // and an unknown squad are separate facts that merely coincide pre-season.
  const squadKnown = $derived(d?.squad_state?.known === true)

  let showEvidence = $state(false)

  const tone = $derived(body ? ACTION_TONE[body.action] : 'info')
  // 'neutral' is the bare .chip: no colour at all, for a state that is neither
  // an outcome nor a fault.
  const toneClass = $derived(
    tone === 'good' ? 'chip-good' : tone === 'warn' ? 'chip-warn'
      : tone === 'bad' ? 'chip-bad' : tone === 'neutral' ? '' : 'chip-info',
  )
  const confidenceClass = $derived(
    body?.confidence === 'high' ? 'text-brand-light'
      : body?.confidence === 'medium' ? 'text-yellow' : 'text-muted',
  )

  function label(c: Card | null | undefined) {
    return c?.name ?? '—'
  }

  // The one-week projection the pipeline already attaches to every squad card
  // (`next_gw_xp`, the same field Players sorts on). Read, never recomputed: a
  // second implementation that disagreed by 0.1 would be worse than no number.
  function xp(c: Card | null | undefined): number | null {
    const v = c?.next_gw_xp
    return typeof v === 'number' && Number.isFinite(v) ? v : null
  }
  function xpText(c: Card | null | undefined): string {
    const v = xp(c)
    return v == null ? '—' : v.toFixed(1)
  }

  // A partial sum looks exactly like a complete one, so the total is offered
  // only when every starter carries a projection. The armband is added a second
  // time because that is what will be scored — not because the model says so.
  const xiTotal = $derived.by(() => {
    const xi = body?.starting ?? []
    if (!xi.length) return null
    const vals = xi.map(xp)
    if (vals.some((v) => v == null)) return null
    const sum = (vals as number[]).reduce((a, b) => a + (b ?? 0), 0)
    const armband = xp(body?.captain) ?? 0
    return { xi: sum, armband, total: sum + armband }
  })
</script>

<!-- One column on a phone, which is the layout that matters. At `lg` the same
     cards split into two so the answer and its arithmetic stop running off the
     bottom of a desktop viewport while 240px sits unused to the right. -->
<div class="rise flex flex-col gap-4 max-w-3xl lg:max-w-5xl mx-auto w-full">
  <!-- ── the week, at a glance ────────────────────────────────────── -->
  <div class="card p-4">
    <div class="flex items-start justify-between gap-3 flex-wrap">
      <div>
        <h1 class="font-black text-xl">{meta?.gw_name ?? `Gameweek ${meta?.current_gw ?? '?'}`}</h1>
        {#if dl?.state === 'until'}
          <p class="text-sm text-muted">
            Deadline in <b class="text-text">{dl.remaining}</b>
          </p>
        {:else if dl?.state === 'passed'}
          <p class="text-sm text-muted">
            <b class="text-text">Deadline passed</b>
          </p>
        {/if}
      </div>
      <div class="flex flex-col items-end gap-1">
        <span
          class="chip {fresh.state === 'fresh' ? 'chip-good' : fresh.state === 'critical' || fresh.state === 'expired' ? 'chip-bad' : 'chip-warn'}"
          title={fresh.title}
        >{fresh.label}</span>
        {#if meta?.build_mode === 'generic'}
          <span class="chip chip-warn">generic build — not your team</span>
        {/if}
      </div>
    </div>
  </div>

  {#if parsed.kind === 'missing'}
    <div class="card p-6 text-center">
      <h2 class="font-bold">No weekly decision in this build</h2>
      <p class="text-sm text-muted mt-2">
        Run the pipeline to generate <code>decision.json</code>.
      </p>
    </div>
  {:else if parsed.kind !== 'ok'}
    <div class="card p-6">
      <h2 class="font-bold text-red">This build can't render that decision</h2>
      <p class="text-sm text-muted mt-2">{parsed.detail}</p>
      <p class="text-mini text-muted2 mt-2">
        Refusing is deliberate — advice from a version this build no longer contains
        would look identical to advice it does.
      </p>
    </div>
  {:else if d && body}
    <!-- The two groups below are in phone reading order, so the desktop split
         cannot reshuffle the small screen: the answer, then whether it beats
         holding, then whether you can afford it; the squad and the small print
         move alongside rather than underneath. -->
    <div class="grid gap-4 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)] lg:items-start">
      <div class="flex flex-col gap-4 min-w-0">
        <!-- ── THE ANSWER ─────────────────────────────────────────── -->
        <section class="card p-4" aria-labelledby="decision-heading">
          <div class="flex items-center gap-2 flex-wrap">
            <span class="chip {toneClass}">{ACTION_LABELS[body.action]}</span>
            <span class="text-xs {confidenceClass}">{body.confidence} confidence</span>
          </div>
          <h2 id="decision-heading" class="font-black text-2xl mt-2 leading-tight">
            {body.headline}
          </h2>
          <p class="text-sm text-muted mt-1">{body.reason}</p>

          {#if body.transfers_out.length || body.transfers_in.length}
            <div class="grid grid-cols-2 gap-2 mt-3">
              <div class="rounded-lg bg-bg3 p-2">
                <div class="text-micro uppercase font-bold text-muted2 mb-1">Out</div>
                {#each body.transfers_out as p (p.id)}
                  <button class="flex items-center gap-2 w-full min-h-11 text-left" onclick={() => onpick(p.id)}>
                    <Crest code={p.team_code} short={p.team ?? ''} size={16} />
                    <span class="truncate">{label(p)}</span>
                    <span class="ml-auto shrink-0 text-mini font-semibold text-muted tabular-nums">{xpText(p)}</span>
                  </button>
                {/each}
              </div>
              <div class="rounded-lg bg-bg3 p-2">
                <div class="text-micro uppercase font-bold text-muted2 mb-1">In</div>
                {#each body.transfers_in as p (p.id)}
                  <button class="flex items-center gap-2 w-full min-h-11 text-left" onclick={() => onpick(p.id)}>
                    <Crest code={p.team_code} short={p.team ?? ''} size={16} />
                    <span class="truncate">{label(p)}</span>
                    <span class="ml-auto shrink-0 text-mini font-semibold text-brand-light tabular-nums">{xpText(p)}</span>
                  </button>
                {/each}
              </div>
            </div>
          {/if}

          <!-- captain / vice
               Buttons, not labels. On deadline day the captaincy is frequently the
               whole decision, and these were the only picks on this screen with no
               route to their own reasoning — the transfer chips either side of them
               already open it. Same `onpick` handler, so there is one way in. -->
          <div class="flex gap-2 mt-3 flex-wrap">
            {#if body.captain}
              {@const c = body.captain}
              <button
                class="chip chip-good"
                onclick={() => onpick(c.id)}
                aria-label="Captain {label(c)} — open player detail"
              >C: {label(c)}</button>
            {:else}
              <span class="chip chip-good">C: {label(body.captain)}</span>
            {/if}
            {#if body.vice}
              {@const v = body.vice}
              <button
                class="chip chip-info"
                onclick={() => onpick(v.id)}
                aria-label="Vice-captain {label(v)} — open player detail"
              >V: {label(v)}</button>
            {:else}
              <span class="chip chip-info">V: {label(body.vice)}</span>
            {/if}
            {#if chip?.recommendation && chip.recommendation !== 'hold'}
              <span class="chip chip-warn">
                Chip: {chip.recommendation} {signed(chip.expected_gain)}
              </span>
            {:else}
              <span class="chip">No chip</span>
            {/if}
          </div>

          <!-- the biggest reason this could be wrong -->
          {#if body.biggest_risk}
            <div class="mt-3 rounded-lg border border-yellow/40 bg-yellow/10 p-3">
              <div class="text-micro uppercase font-bold text-yellow mb-1">
                Biggest reason this could be wrong
              </div>
              <p class="text-sm">{body.biggest_risk}</p>
            </div>
          {/if}
        </section>

        <!-- ── versus holding ─────────────────────────────────────── -->
        {#if cmp}
          <section class="card p-4" aria-labelledby="vs-hold">
            <h3 id="vs-hold" class="font-bold text-sm">Versus doing nothing</h3>
            <div class="grid grid-cols-3 gap-2 mt-2 text-center">
              <div class="rounded-lg bg-bg3 p-2">
                <div class="text-micro uppercase text-muted2 font-bold">Gain</div>
                <div class="text-xl font-black tabular-nums">{signed(cmp.delta)}</div>
              </div>
              <div class="rounded-lg bg-bg3 p-2">
                <div class="text-micro uppercase text-muted2 font-bold">Beats hold</div>
                <div class="text-xl font-black tabular-nums">{pctOf(cmp.p_move_beats_hold)}</div>
              </div>
              <div class="rounded-lg bg-bg3 p-2">
                <div class="text-micro uppercase text-muted2 font-bold">Hit</div>
                <div class="text-xl font-black tabular-nums">{cmp.hit_cost ? `−${cmp.hit_cost}` : '0'}</div>
              </div>
            </div>
            <p class="text-mini text-muted2 mt-2">
              95% CI {cmp.delta_ci95[0].toFixed(1)} to {cmp.delta_ci95[1].toFixed(1)} over
              {cmp.simulations.toLocaleString()} shared scenarios ·
              this GW {signed(cmp.short_term_delta)}{#if cmp.horizon_delta != null}, over the
              horizon {signed(cmp.horizon_delta)}{/if}
            </p>
          </section>
        {/if}

        <!-- ── money and transfers ────────────────────────────────── -->
        {#if exe}
          <section class="card p-4" aria-labelledby="exe">
            <div class="flex items-center justify-between">
              <h3 id="exe" class="font-bold text-sm">Can you actually do it?</h3>
              <span class="chip {exe.affordable ? 'chip-good' : 'chip-bad'}">
                {exe.affordable ? 'Executable' : 'Not executable'}
              </span>
            </div>
            <table class="data mt-2">
              <tbody>
                <tr>
                  <td class="!text-left text-muted">Bank</td>
                  <td class="!text-left tabular-nums">
                    {money(exe.bank_before)} → {money(exe.bank_after)}
                  </td>
                </tr>
                <tr>
                  <td class="!text-left text-muted">Free transfers</td>
                  <td class="!text-left tabular-nums">
                    {exe.free_transfers_before} → {exe.free_transfers_after}
                  </td>
                </tr>
                {#if exe.paid_transfers}
                  <tr>
                    <td class="!text-left text-muted">Paid transfers</td>
                    <td class="!text-left tabular-nums text-red">{exe.paid_transfers}</td>
                  </tr>
                {/if}
              </tbody>
            </table>
            {#if exe.reason}
              <p class="text-mini text-yellow mt-2">{exe.reason}</p>
            {/if}
          </section>
        {/if}
      </div>

      <div class="flex flex-col gap-4 min-w-0">
        <!-- ── team sheet ─────────────────────────────────────────── -->
        {#if body.starting.length}
          <section class="card p-4" aria-labelledby="sheet">
            <!-- Before the first deadline the optimiser builds this XI from a blank
                 £100m budget, so it is not the owner's team and calling it "yours"
                 contradicts the risk note three cards above. -->
            <div class="flex items-baseline justify-between gap-2 mb-2">
              <h3 id="sheet" class="font-bold text-sm">
                {squadKnown
                  ? 'Your XI and bench order'
                  : "The model's suggested XI and bench order"}
              </h3>
              {#if xiTotal}
                <span
                  class="shrink-0 text-mini text-muted2 tabular-nums"
                  title="The eleven projections below add up to {xiTotal.xi.toFixed(1)}, plus {xiTotal.armband.toFixed(1)} for counting the armband a second time. Autosubs, bonus and price changes are not in it."
                ><b class="text-sm text-brand-light">{xiTotal.total.toFixed(1)}</b> xP</span>
              {/if}
            </div>
            <!-- Three across once there is room, back to two inside the narrower
                 right-hand column at `lg`. -->
            <ul class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-2 gap-1">
              {#each body.starting as p (p.id)}
                <li>
                  <button
                    class="flex items-center gap-1.5 w-full min-h-11 text-left text-sm px-1 rounded hover:bg-bg3"
                    onclick={() => onpick(p.id)}
                    aria-label="{label(p)}, {xpText(p)} projected points — open player detail"
                  >
                    <Crest code={p.team_code} short={p.team ?? ''} size={14} />
                    <span class="truncate">{label(p)}</span>
                    <!-- The armband markers move from `.chip` to the smaller
                         `.badge` to pay for the number: at 390px the two columns
                         leave ~157px a row and a chip ate 25 of them. -->
                    {#if p.id === body.captain?.id}<span class="badge badge-good shrink-0">C</span>{/if}
                    {#if p.id === body.vice?.id}<span class="badge shrink-0 bg-accent/20 text-accent-light">V</span>{/if}
                    <!-- The number the whole product exists to produce, pitched
                         below the name: this list is scanned, not read.
                         U18 asked for xP "on Home's XI". It was already here —
                         and in the aria-label — but sighted readers saw a bare
                         figure with no unit. A per-row "xP" would cost the ~20px
                         a row that the armband chip was demoted to a badge to
                         save, and a single column header cannot align across a
                         2/3/2 grid. So the unit rides on the title instead. -->
                    <span
                      class="ml-auto shrink-0 text-mini font-semibold text-muted tabular-nums"
                      title="{xpText(p)} projected points (xP) this gameweek"
                      >{xpText(p)}</span>
                  </button>
                </li>
              {/each}
            </ul>
            {#if body.bench.length}
              <h4 class="text-micro uppercase font-bold text-muted2 mt-3 mb-1">
                Bench, in order
              </h4>
              <ol class="flex flex-wrap gap-1">
                {#each body.bench as p, i (p.id)}
                  <li>
                    <button
                      class="flex items-center gap-1 min-h-11 px-2 rounded bg-bg3 text-sm"
                      onclick={() => onpick(p.id)}
                      aria-label="Bench {i + 1}, {label(p)}, {xpText(p)} projected points — open player detail"
                    >
                      <span class="text-muted2 text-mini">{i + 1}</span>
                      <Crest code={p.team_code} short={p.team ?? ''} size={14} />
                      <span class="truncate">{label(p)}</span>
                      <span
                        class="text-mini font-semibold text-muted2 tabular-nums"
                        title="{xpText(p)} projected points (xP) this gameweek"
                        >{xpText(p)}</span>
                    </button>
                  </li>
                {/each}
              </ol>
            {/if}
          </section>
        {/if}

        <!-- ── league implication ─────────────────────────────────── -->
        {#if body.league_note}
          <section class="card p-3">
            <h3 class="font-bold text-sm mb-1">League implication</h3>
            <p class="text-sm text-muted">{body.league_note}</p>
            <button class="text-mini text-accent-light hover:underline mt-1"
                    onclick={() => onnav('strategy')}>Full league strategy →</button>
          </section>
        {/if}

        <!-- ── evidence ───────────────────────────────────────────── -->
        <section class="card p-3">
          <button
            class="text-xs font-bold text-muted min-h-11"
            aria-expanded={showEvidence}
            onclick={() => (showEvidence = !showEvidence)}
          >{showEvidence ? '▾' : '▸'} What this rests on</button>
          {#if showEvidence}
            <ul class="text-mini text-muted2 mt-2 list-disc pl-4 space-y-0.5">
              {#each body.assumptions as a}<li>{a}</li>{/each}
              <li>
                Model <code>{d.versions.model_version}</code>, objective
                <code>{d.versions.objective_version}</code>, simulation
                <code>{d.versions.sim_version}</code> (seed {d.versions.seed}).
              </li>
              {#if meta?.projection_regime}
                <li>
                  One-week projection: <code>{meta.projection_regime}</code>{#if meta.projection_regime_reason} — {meta.projection_regime_reason}{/if}.
                </li>
              {/if}
              {#if d.squad_state.source_event}
                <li>Your squad was read from GW{d.squad_state.source_event}.</li>
              {/if}
            </ul>
          {/if}
        </section>
      </div>
    </div>
  {/if}
</div>
