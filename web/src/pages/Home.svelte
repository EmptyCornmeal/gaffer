<script lang="ts">
  import type { Bundle } from '../lib/data'
  import {
    parseDecision, ACTION_LABELS, ACTION_TONE, signed, pctOf, money,
    type Card,
  } from '../lib/weekly'
  import { classifyFreshness } from '../lib/freshness'
  import { countdown } from '../lib/data'
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
  const cmp = $derived(body?.comparison ?? null)
  const exe = $derived(body?.executability ?? null)
  const chip = $derived((d?.chip ?? null) as { recommendation?: string; reason?: string; expected_gain?: number } | null)

  let showEvidence = $state(false)

  const tone = $derived(body ? ACTION_TONE[body.action] : 'info')
  const toneClass = $derived(
    tone === 'good' ? 'chip-good' : tone === 'warn' ? 'chip-warn'
      : tone === 'bad' ? 'chip-bad' : 'chip-info',
  )
  const confidenceClass = $derived(
    body?.confidence === 'high' ? 'text-brand-light'
      : body?.confidence === 'medium' ? 'text-yellow' : 'text-muted',
  )

  function label(c: Card | null | undefined) {
    return c?.name ?? '—'
  }
</script>

<div class="rise flex flex-col gap-4 max-w-3xl mx-auto w-full">
  <!-- ── the week, at a glance ────────────────────────────────────── -->
  <div class="card p-4">
    <div class="flex items-start justify-between gap-3 flex-wrap">
      <div>
        <h1 class="font-black text-xl">{meta?.gw_name ?? `Gameweek ${meta?.current_gw ?? '?'}`}</h1>
        {#if meta?.deadline}
          <p class="text-sm text-muted">
            Deadline in <b class="text-text">{countdown(meta.deadline, now)}</b>
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
      <p class="text-[11px] text-muted2 mt-2">
        Refusing is deliberate — advice from a version this build no longer contains
        would look identical to advice it does.
      </p>
    </div>
  {:else if d && body}
    <!-- ── THE ANSWER ─────────────────────────────────────────────── -->
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
            <div class="text-[10px] uppercase font-bold text-muted2 mb-1">Out</div>
            {#each body.transfers_out as p (p.id)}
              <button class="flex items-center gap-2 w-full min-h-11 text-left" onclick={() => onpick(p.id)}>
                <Crest code={p.team_code} short={p.team ?? ''} size={16} />
                <span class="truncate">{label(p)}</span>
              </button>
            {/each}
          </div>
          <div class="rounded-lg bg-bg3 p-2">
            <div class="text-[10px] uppercase font-bold text-muted2 mb-1">In</div>
            {#each body.transfers_in as p (p.id)}
              <button class="flex items-center gap-2 w-full min-h-11 text-left" onclick={() => onpick(p.id)}>
                <Crest code={p.team_code} short={p.team ?? ''} size={16} />
                <span class="truncate">{label(p)}</span>
              </button>
            {/each}
          </div>
        </div>
      {/if}

      <!-- captain / vice -->
      <div class="flex gap-2 mt-3 flex-wrap">
        <span class="chip chip-good">C: {label(body.captain)}</span>
        <span class="chip chip-info">V: {label(body.vice)}</span>
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
          <div class="text-[10px] uppercase font-bold text-yellow mb-1">
            Biggest reason this could be wrong
          </div>
          <p class="text-sm">{body.biggest_risk}</p>
        </div>
      {/if}
    </section>

    <!-- ── versus holding ─────────────────────────────────────────── -->
    {#if cmp}
      <section class="card p-4" aria-labelledby="vs-hold">
        <h3 id="vs-hold" class="font-bold text-sm">Versus doing nothing</h3>
        <div class="grid grid-cols-3 gap-2 mt-2 text-center">
          <div class="rounded-lg bg-bg3 p-2">
            <div class="text-[10px] uppercase text-muted2 font-bold">Gain</div>
            <div class="text-xl font-black tabular-nums">{signed(cmp.delta)}</div>
          </div>
          <div class="rounded-lg bg-bg3 p-2">
            <div class="text-[10px] uppercase text-muted2 font-bold">Beats hold</div>
            <div class="text-xl font-black tabular-nums">{pctOf(cmp.p_move_beats_hold)}</div>
          </div>
          <div class="rounded-lg bg-bg3 p-2">
            <div class="text-[10px] uppercase text-muted2 font-bold">Hit</div>
            <div class="text-xl font-black tabular-nums">{cmp.hit_cost ? `−${cmp.hit_cost}` : '0'}</div>
          </div>
        </div>
        <p class="text-[11px] text-muted2 mt-2">
          95% CI {cmp.delta_ci95[0].toFixed(1)} to {cmp.delta_ci95[1].toFixed(1)} over
          {cmp.simulations.toLocaleString()} shared scenarios ·
          this GW {signed(cmp.short_term_delta)}{#if cmp.horizon_delta != null}, over the
          horizon {signed(cmp.horizon_delta)}{/if}
        </p>
      </section>
    {/if}

    <!-- ── money and transfers ────────────────────────────────────── -->
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
          <p class="text-[11px] text-yellow mt-2">{exe.reason}</p>
        {/if}
      </section>
    {/if}

    <!-- ── team sheet ─────────────────────────────────────────────── -->
    {#if body.starting.length}
      <section class="card p-4" aria-labelledby="sheet">
        <h3 id="sheet" class="font-bold text-sm mb-2">Your XI and bench order</h3>
        <ul class="grid grid-cols-2 sm:grid-cols-3 gap-1">
          {#each body.starting as p (p.id)}
            <li>
              <button
                class="flex items-center gap-1.5 w-full min-h-11 text-left text-sm px-1 rounded hover:bg-bg3"
                onclick={() => onpick(p.id)}
              >
                <Crest code={p.team_code} short={p.team ?? ''} size={14} />
                <span class="truncate">{label(p)}</span>
                {#if p.id === body.captain?.id}<span class="chip chip-good">C</span>{/if}
                {#if p.id === body.vice?.id}<span class="chip chip-info">V</span>{/if}
              </button>
            </li>
          {/each}
        </ul>
        {#if body.bench.length}
          <h4 class="text-[10px] uppercase font-bold text-muted2 mt-3 mb-1">
            Bench, in order
          </h4>
          <ol class="flex flex-wrap gap-1">
            {#each body.bench as p, i (p.id)}
              <li>
                <button
                  class="flex items-center gap-1 min-h-11 px-2 rounded bg-bg3 text-sm"
                  onclick={() => onpick(p.id)}
                >
                  <span class="text-muted2 text-[11px]">{i + 1}</span>
                  <Crest code={p.team_code} short={p.team ?? ''} size={14} />
                  <span class="truncate">{label(p)}</span>
                </button>
              </li>
            {/each}
          </ol>
        {/if}
      </section>
    {/if}

    <!-- ── league implication ─────────────────────────────────────── -->
    {#if body.league_note}
      <section class="card p-3">
        <h3 class="font-bold text-sm mb-1">League implication</h3>
        <p class="text-sm text-muted">{body.league_note}</p>
        <button class="text-[11px] text-accent-light hover:underline mt-1"
                onclick={() => onnav('strategy')}>Full league strategy →</button>
      </section>
    {/if}

    <!-- ── evidence ───────────────────────────────────────────────── -->
    <section class="card p-3">
      <button
        class="text-xs font-bold text-muted min-h-11"
        aria-expanded={showEvidence}
        onclick={() => (showEvidence = !showEvidence)}
      >{showEvidence ? '▾' : '▸'} What this rests on</button>
      {#if showEvidence}
        <ul class="text-[11px] text-muted2 mt-2 list-disc pl-4 space-y-0.5">
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
  {/if}
</div>
