<script lang="ts">
  import type { Bundle } from '../lib/data'
  import {
    parseStrategy, pct, simError, epCost, departures,
    CHIP_LABELS, STANCE_LABELS, CLASS_LABELS, describeCoverage,
    type Coverage, type LeagueView,
  } from '../lib/strategy'
  import { classifyFreshness } from '../lib/freshness'
  import Icon from '../components/Icon.svelte'
  import Crest from '../components/Crest.svelte'

  let { bundle, onnav, now = Date.now() }: { bundle: Bundle; onnav: (r: string) => void; now?: number } =
    $props()

  const parsed = $derived(parseStrategy(bundle.strategy))
  const s = $derived(parsed.kind === 'ok' ? parsed.data : null)
  const fresh = $derived(s ? classifyFreshness(s.generated_at, now) : null)

  // Progressive disclosure: one league open at a time on a phone.
  let openLeague = $state<number | null>(null)
  let showDetail = $state(false)

  const chip = $derived(s?.chips ?? null)
  const chipLabel = $derived(chip ? (CHIP_LABELS[chip.recommendation] ?? chip.recommendation) : '')
  const conflicts = $derived(s?.resolution.conflicts ?? [])
  const diverging = $derived(s ? departures(s) : [])
  const err = $derived(s?.simulation ? simError(s.simulation.n_sims) : 0)

  function stanceClass(stance: string) {
    if (stance === 'protect') return 'chip-info'
    if (stance === 'chase') return 'chip-warn'
    if (stance === 'desperate') return 'chip-bad'
    return 'chip-good'
  }
  // Colour follows the level, not a percentage: 0/0 rivals is not a red failure,
  // and an inconsistent artifact must not look like healthy partial coverage.
  function coverageClass(c: Coverage) {
    if (c.level === 'full') return 'text-brand-light'
    if (c.level === 'partial') return 'text-yellow'
    if (c.level === 'no_rivals') return 'text-muted2'
    return 'text-red'
  }
  function lg(l: LeagueView) {
    return openLeague === l.league_id
  }
</script>

<div class="rise flex flex-col gap-4 max-w-4xl">
  <div>
    <h2 class="font-bold text-lg flex items-center gap-2"><Icon name="trophy" size={18} /> Strategy</h2>
    <p class="text-sm text-muted">
      What your leagues actually want, and where they disagree — from rival squads, not global ownership.
    </p>
  </div>

  {#if parsed.kind === 'missing'}
    <div class="card p-6 text-center">
      <h3 class="font-bold">No strategy data in this build</h3>
      <p class="text-sm text-muted mt-2">
        The pipeline ran with league analysis skipped, or no league IDs are configured.
        Add them to <code>gaffer.local.toml</code> and re-run.
      </p>
    </div>
  {:else if parsed.kind === 'failed'}
    <div class="card p-6">
      <h3 class="font-bold text-yellow">League analysis failed on the last run</h3>
      <p class="text-sm text-muted mt-2">
        The core recommendation is unaffected — this step is deliberately contained.
      </p>
      <pre class="text-[11px] text-muted2 mt-2 whitespace-pre-wrap">{parsed.detail}</pre>
    </div>
  {:else if parsed.kind === 'unsupported' || parsed.kind === 'malformed'}
    <div class="card p-6">
      <h3 class="font-bold text-red">This build can't render that strategy artifact</h3>
      <p class="text-sm text-muted mt-2">{parsed.detail}</p>
      <p class="text-[11px] text-muted2 mt-2">
        Showing nothing is deliberate: a probability from a simulator this build no longer
        contains would be a number with no meaning behind it.
      </p>
    </div>
  {:else if s}
    <!-- ── freshness ─────────────────────────────────────────────── -->
    {#if fresh && fresh.state !== 'fresh'}
      <div class="text-xs {fresh.state === 'critical' ? 'chip-bad' : 'chip-warn'} rounded-lg px-3 py-2 flex items-center gap-2">
        <Icon name="hourglass" size={13} /> {fresh.label} — rival squads and probabilities may be out of date.
      </div>
    {/if}

    <!-- ── the neutral recommendation ────────────────────────────── -->
    <div class="card p-4">
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <h3 class="font-bold text-sm">Neutral recommendation</h3>
        <span class="text-[11px] text-muted2">maximises expected points</span>
      </div>
      <div class="flex items-center gap-2 mt-2 flex-wrap">
        {#if s.squad.captain}
          <span class="chip chip-good">C: {s.squad.captain.name}</span>
        {/if}
        <span class="text-sm text-muted">GW{s.gameweek} · based on {s.basis}</span>
      </div>
      {#if diverging.length === 0}
        <p class="text-sm text-muted mt-2">
          {s.leagues.length
            ? 'No league argues for a different move this week — expected points is the right objective everywhere.'
            : 'No league data, so there is nothing for the leagues to disagree about.'}
        </p>
      {:else}
        <p class="text-sm mt-2">
          <b>{diverging.length}</b> of {s.leagues.length} league{s.leagues.length === 1 ? '' : 's'}
          argue{diverging.length === 1 ? 's' : ''} for a different posture — see below.
        </p>
      {/if}
    </div>

    <!-- ── chip ───────────────────────────────────────────────────── -->
    {#if chip}
      <div class="card p-4">
        <div class="flex items-baseline justify-between gap-2 flex-wrap">
          <h3 class="font-bold text-sm flex items-center gap-1.5"><Icon name="layers" size={15} /> Chips</h3>
          {#if chip.recommendation !== 'hold'}
            <span class="text-sm font-bold text-brand-light tabular-nums">
              +{chip.expected_gain.toFixed(1)} pts
            </span>
          {/if}
        </div>
        <div class="text-2xl font-black mt-1">
          {chipLabel}{#if chip.gameweek} <span class="text-lg text-muted font-bold">GW{chip.gameweek}</span>{/if}
        </div>
        <p class="text-sm text-muted mt-1">{chip.reason}</p>

        <div class="flex gap-1.5 flex-wrap mt-3">
          {#each chip.available as w}
            <span class="chip chip-info">{CHIP_LABELS[w.name] ?? w.name} · GW{w.start_event}–{w.stop_event}</span>
          {/each}
          {#each chip.used as u}
            <span class="chip chip-bad">{CHIP_LABELS[u] ?? u} · played</span>
          {/each}
          {#if !chip.available.length && !chip.used.length}
            <span class="text-[11px] text-muted2">No chip windows are open in GW{s.gameweek}.</span>
          {/if}
        </div>

        {#if chip.alternatives.length}
          <table class="data mt-3">
            <thead><tr><th class="!text-left">Chip</th><th>Gain</th><th>95% CI</th></tr></thead>
            <tbody>
              {#each chip.alternatives as a}
                <tr>
                  <td class="!text-left">{CHIP_LABELS[a.chip] ?? a.chip}</td>
                  <td class="tabular-nums font-semibold">{a.expected_gain >= 0 ? '+' : ''}{a.expected_gain.toFixed(1)}</td>
                  <td class="tabular-nums text-muted2">{a.ci95[0].toFixed(1)} to {a.ci95[1].toFixed(1)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
          <details class="mt-2">
            <summary class="text-[11px] text-muted2 cursor-pointer">What each number assumes</summary>
            <ul class="text-[11px] text-muted2 mt-1 list-disc pl-4 space-y-0.5">
              {#each chip.alternatives as a}
                {#each a.assumptions as x}<li>{CHIP_LABELS[a.chip] ?? a.chip}: {x}</li>{/each}
              {/each}
            </ul>
          </details>
        {/if}
      </div>
    {/if}

    <!-- ── per-league ─────────────────────────────────────────────── -->
    {#each s.leagues as l (l.league_id)}
      <!-- Coverage is stated from the counts every time, whether or not a placing
           probability came out, because it is what those probabilities are worth. -->
      {@const cov = describeCoverage(l.data_quality)}
      <div class="card p-4">
        <button class="w-full text-left" onclick={() => (openLeague = lg(l) ? null : l.league_id)}>
          <div class="flex items-center justify-between gap-2 flex-wrap">
            <div>
              <h3 class="font-bold text-sm">{l.name}</h3>
              <p class="text-[11px] text-muted2">{CLASS_LABELS[l.classification] ?? l.classification}</p>
            </div>
            <span class="chip {stanceClass(l.posture.stance)}">{STANCE_LABELS[l.posture.stance] ?? l.posture.stance}</span>
          </div>
        </button>

        <div class="grid grid-cols-3 gap-2 mt-3 text-center">
          <div class="rounded-lg bg-bg3 p-2">
            <div class="text-[10px] uppercase text-muted2 font-bold">Win it</div>
            <div class="text-xl font-black tabular-nums">{l.placing.available ? pct(l.placing.p_first) : '—'}</div>
          </div>
          <div class="rounded-lg bg-bg3 p-2">
            <div class="text-[10px] uppercase text-muted2 font-bold">Top {l.target_position}</div>
            <div class="text-xl font-black tabular-nums">{l.placing.available ? pct(l.placing.p_target) : '—'}</div>
          </div>
          <div class="rounded-lg bg-bg3 p-2">
            <div class="text-[10px] uppercase text-muted2 font-bold">Exp. place</div>
            <div class="text-xl font-black tabular-nums">
              {l.placing.available ? l.placing.expected_position.toFixed(1) : '—'}
            </div>
          </div>
        </div>

        {#if l.placing.available}
          <p class="text-[11px] text-muted2 mt-2">
            ±{(l.placing.ci95_halfwidth * 100).toFixed(1)}pp from {l.placing.simulations.toLocaleString()} shared scenarios
          </p>
        {:else}
          <p class="text-[11px] text-muted2 mt-2">
            No placing probability yet — {l.placing.caveats[0] ?? 'nothing to place against'}.
          </p>
        {/if}

        <p class="text-[11px] mt-1">
          <span class="font-semibold {coverageClass(cov)}">{cov.summary}</span>
          {#if cov.meaning}<span class="text-muted2"> — {cov.meaning}</span>{/if}
          {#if cov.notes.length}<span class="text-muted2"> · {cov.notes.join(' · ')}</span>{/if}
        </p>

        {#if l.differs_from_neutral}
          <p class="text-sm mt-2 chip-warn rounded-lg px-3 py-2">{l.difference_reason}</p>
        {/if}

        {#if lg(l)}
          <div class="mt-3 grid sm:grid-cols-2 gap-3">
            <div>
              <h4 class="text-xs font-bold uppercase text-muted mb-1">
                <Icon name="shield" size={12} /> Shields ({l.shields.length})
              </h4>
              {#if l.shields.length}
                <ul class="space-y-1">
                  {#each l.shields as row}
                    <li class="flex items-center gap-2 text-sm">
                      <Crest code={row.player?.team_code} short={row.player?.team ?? ''} size={14} />
                      <span class="flex-1 truncate">{row.player?.name ?? row.player_id}</span>
                      <span class="tabular-nums text-muted2 text-[11px]">EO {row.effective_ownership_pct}%</span>
                    </li>
                  {/each}
                </ul>
              {:else}
                <p class="text-[11px] text-muted2">Nothing you own is majority-owned here.</p>
              {/if}
            </div>
            <div>
              <h4 class="text-xs font-bold uppercase text-muted mb-1">
                <Icon name="flame" size={12} /> Differentials ({l.differentials.length})
              </h4>
              {#if l.differentials.length}
                <ul class="space-y-1">
                  {#each l.differentials as row}
                    <li class="flex items-center gap-2 text-sm">
                      <Crest code={row.player?.team_code} short={row.player?.team ?? ''} size={14} />
                      <span class="flex-1 truncate">{row.player?.name ?? row.player_id}</span>
                      <span class="tabular-nums text-muted2 text-[11px]">0 rivals</span>
                    </li>
                  {/each}
                </ul>
              {:else}
                <p class="text-[11px] text-muted2">No player of yours is unowned in this league.</p>
              {/if}
            </div>
          </div>
          <p class="text-[11px] text-muted2 mt-2">{l.posture.reason}.</p>
          {#if l.placing.caveats.length}
            <ul class="text-[11px] text-muted2 mt-1 list-disc pl-4 space-y-0.5">
              {#each l.placing.caveats as c}<li>{c}</li>{/each}
            </ul>
          {/if}
        {:else}
          <button class="text-[11px] text-accent-light hover:underline mt-2" onclick={() => (openLeague = l.league_id)}>
            Show shields &amp; differentials →
          </button>
        {/if}
      </div>
    {/each}

    {#if s.league_errors.length}
      <div class="card p-3">
        <h3 class="font-bold text-sm text-yellow">Some leagues couldn't be read</h3>
        <ul class="text-[11px] text-muted2 mt-1 list-disc pl-4">
          {#each s.league_errors as e}
            <li>{e.league_id ? `League ${e.league_id}: ` : ''}{e.error}</li>
          {/each}
        </ul>
      </div>
    {/if}

    <!-- ── conflicts ──────────────────────────────────────────────── -->
    {#if s.options.length}
      <div class="card p-4">
        <h3 class="font-bold text-sm">Captain, scored in every league at once</h3>
        <p class="text-[11px] text-muted2 mb-2">
          The armband is the week's biggest lever and the one place league ownership really
          changes the answer. The cost column is what you give up in expected points.
        </p>
        <div class="overflow-x-auto">
          <table class="data">
            <thead>
              <tr>
                <th class="!text-left">Option</th>
                <th>xP</th>
                <th>EP cost</th>
                {#each s.leagues as l}<th class="whitespace-nowrap">{l.name}</th>{/each}
              </tr>
            </thead>
            <tbody>
              {#each s.options as o}
                <tr class={s.resolution.default === o.key ? 'bg-brand/10' : ''}>
                  <td class="!text-left">{o.label}</td>
                  <td class="tabular-nums font-semibold">{o.expected_points.toFixed(1)}</td>
                  <td class="tabular-nums {epCost(s, o.key) > 0.001 ? 'text-yellow' : 'text-muted2'}">
                    {epCost(s, o.key) > 0.001 ? `−${epCost(s, o.key).toFixed(2)}` : '—'}
                  </td>
                  {#each s.leagues as l}
                    <td class="tabular-nums">{pct(o.p_target[String(l.league_id)], 1)}</td>
                  {/each}
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        <p class="text-sm mt-2">{s.resolution.reason}</p>
        {#if conflicts.length}
          <div class="mt-2 space-y-1">
            {#each conflicts as c}
              <div class="text-[11px] chip-warn rounded-lg px-3 py-2">
                <b>{c.option_a}</b> vs <b>{c.option_b}</b>:
                {#each c.per_league as d, i}{i > 0 ? ' · ' : ' '}{d.league} prefers {d.prefers}{/each}
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    <!-- ── provenance ─────────────────────────────────────────────── -->
    <div class="card p-3">
      <button class="text-xs font-bold text-muted" onclick={() => (showDetail = !showDetail)}>
        {showDetail ? '▾' : '▸'} How these numbers were produced
      </button>
      {#if showDetail}
        <ul class="text-[11px] text-muted2 mt-2 list-disc pl-4 space-y-0.5">
          <li>
            {s.simulation.n_sims.toLocaleString()} shared fixture scenarios
            (<code>{s.simulation.sim_version}</code>, seed {s.simulation.seed},
            model <code>{s.simulation.model_version}</code>) — simulation error ≈ ±{err.toFixed(1)}pp.
          </li>
          {#each s.simulation.assumptions ?? [] as a}<li>{a}</li>{/each}
          {#each s.limitations as x}<li>{x}</li>{/each}
          <li>
            Effective ownership is measured <b>inside each league</b>, never from global
            selected-by%. <button class="text-accent-light hover:underline" onclick={() => onnav('league')}>League standings →</button>
          </li>
        </ul>
      {/if}
    </div>
  {/if}
</div>
