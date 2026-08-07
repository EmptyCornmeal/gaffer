<script lang="ts">
  import type { Bundle } from '../lib/data'
  import {
    DECISION_LABELS, horizonKeys, leakageClean, METHOD_LABELS, methodsIn,
    modelCandidates, parseBacktest, withdrawalConsequence, withdrawn,
  } from '../lib/backtest'

  let { bundle }: { bundle: Bundle } = $props()

  const state = $derived(parseBacktest(bundle.backtest as unknown))
  const bt = $derived(state.kind === 'ok' ? state.data : null)
  const hs = $derived(bt ? horizonKeys(bt) : [])
  const methods = $derived(bt ? methodsIn(bt) : [])
  const h1 = $derived(bt ? bt.per_horizon['1'] : null)
  const cal = $derived(bt?.calibration?.overall ?? [])
  const retracted = $derived(bt ? withdrawn(bt) : [])
  const consequence = $derived(bt ? withdrawalConsequence(bt) : null)
  // EVERY candidate, not just the losing one.
  const candidates = $derived(bt ? modelCandidates(bt) : [])
  const evidence = $derived(bt?.model_candidates ?? null)
  const heurXI = $derived(evidence?.heuristic_reference?.xi_points_per_gw ?? {})
  const shipped = $derived(bt?.shipped_projection ?? null)

  const decisionClass = (d: string) =>
    d === 'rejected' ? 'text-red'
      : d === 'inconclusive' ? 'text-amber'
        : d === 'shipped' ? 'text-green' : 'text-muted'
  const signed = (v: number) => (v > 0 ? '+' : '') + v.toFixed(2)

  const label = (m: string) => METHOD_LABELS[m] ?? m
  const fmt = (v: number | null | undefined, dp = 3) =>
    v == null || Number.isNaN(v) ? '—' : v.toFixed(dp)

  // Lower is better for MAE and regret; higher is better for the rest.
  function best(vals: Record<string, number>, lower = false): string | null {
    const ks = Object.keys(vals ?? {})
    if (!ks.length) return null
    return ks.reduce((a, b) => ((lower ? vals[b] < vals[a] : vals[b] > vals[a]) ? b : a))
  }
</script>

<div class="max-w-4xl mx-auto space-y-4">
  <div>
    <h1 class="text-xl font-black">Model accuracy</h1>
    <p class="text-sm text-muted mt-1">
      What the projection actually did on a season it never saw, measured with the
      same code the live pipeline runs.
    </p>
  </div>

  {#if state.kind === 'missing'}
    <div class="card p-4">
      <h2 class="font-bold">No backtest published</h2>
      <p class="text-sm text-muted mt-1">
        Run <code>python -m gaffer.backtest --write</code> to generate one.
      </p>
    </div>

  {:else if state.kind === 'unsupported'}
    <div class="card p-4 border border-red/40 bg-red/5">
      <h2 class="font-bold text-red">Backtest not shown</h2>
      <p class="text-sm mt-1">{state.detail}</p>
      <p class="text-xs text-muted2 mt-2">
        Rather than display numbers that may describe a different model, this page
        shows nothing. Regenerate with <code>python -m gaffer.backtest --write</code>.
      </p>
    </div>

  {:else if state.kind === 'malformed'}
    <div class="card p-4 border border-red/40 bg-red/5">
      <h2 class="font-bold text-red">Backtest artifact is malformed</h2>
      <p class="text-sm mt-1">{state.detail}</p>
    </div>

  {:else if bt}
    <!-- Provenance -->
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 text-center">
      <div class="card p-3">
        <div class="text-xl font-black text-brand-light">{bt.coverage.rows_evaluated.toLocaleString()}</div>
        <div class="text-[11px] text-muted">predictions scored</div>
      </div>
      <div class="card p-3">
        <div class="text-xl font-black">{bt.season}</div>
        <div class="text-[11px] text-muted">held out · {bt.decision_gameweeks}</div>
      </div>
      <div class="card p-3">
        <div class="text-xl font-black">{bt.model_version}</div>
        <div class="text-[11px] text-muted">model version</div>
      </div>
      <div class="card p-3">
        <div class="text-xl font-black {leakageClean(bt) ? 'text-brand-light' : 'text-red'}">
          {leakageClean(bt) ? 'clean' : 'FAILED'}
        </div>
        <div class="text-[11px] text-muted">leakage check</div>
      </div>
    </div>

    <!-- Coverage -->
    <section class="card p-3">
      <h2 class="font-bold mb-2">Coverage</h2>
      <p class="text-[13px] text-muted leading-relaxed">
        <b>{bt.coverage.zero_minute_rows_retained.toLocaleString()}</b> rows
        ({bt.coverage.zero_minute_share_pct}%) are players who ended up not playing.
        They are <b>kept</b> — excluding them would mean the model knew who featured
        before it picked a team, which is what the previous harness did.
      </p>
      {#if bt.coverage.excluded}
        <ul class="text-[12px] text-muted2 mt-2 space-y-0.5">
          {#each Object.entries(bt.coverage.excluded) as [k, v]}
            <li>· <span class="text-muted">{k.replace(/_/g, ' ')}:</span> {v}</li>
          {/each}
        </ul>
      {/if}
      <p class="text-[11px] text-muted2 mt-2">
        Leakage policy: {bt.leakage_check.policy}.
        {#if !leakageClean(bt)}
          <span class="text-red">
            Post-match fields found in features:
            {bt.leakage_check.post_match_fields_in_features.join(', ')}
          </span>
        {/if}
      </p>
    </section>

    <!-- Player-level accuracy, per horizon -->
    <section class="card p-3">
      <h2 class="font-bold">Player-level accuracy by horizon</h2>
      <p class="text-[12px] text-muted2 mb-2">
        h=1 is the imminent gameweek; h=6 is six weeks out from the same decision
        point, using the same information. Rank correlation is ordering quality
        (higher better); MAE is points error (lower better).
      </p>
      <div class="overflow-x-auto">
        <table class="data w-full text-sm">
          <thead>
            <tr>
              <th class="text-left">Horizon</th>
              <th class="text-right">n</th>
              {#each methods as m}<th class="text-right">{label(m)}</th>{/each}
            </tr>
          </thead>
          <tbody>
            <tr class="text-[11px] text-muted2"><td colspan={2 + methods.length}>Rank correlation ↑</td></tr>
            {#each hs as h}
              {@const b = bt.per_horizon[h]}
              {@const win = best(b.rank_corr)}
              <tr>
                <td>h = {h}</td>
                <td class="text-right tabular-nums text-muted">{b.n.toLocaleString()}</td>
                {#each methods as m}
                  <td class="text-right tabular-nums {m === win ? 'font-bold text-brand-light' : ''}">
                    {b.rank_corr[m] != null ? fmt(b.rank_corr[m]) : '—'}
                  </td>
                {/each}
              </tr>
            {/each}
            <tr class="text-[11px] text-muted2"><td colspan={2 + methods.length}>MAE ↓</td></tr>
            {#each hs as h}
              {@const b = bt.per_horizon[h]}
              {@const win = best(b.mae, true)}
              <tr>
                <td>h = {h}</td>
                <td class="text-right tabular-nums text-muted">{b.n.toLocaleString()}</td>
                {#each methods as m}
                  <td class="text-right tabular-nums {m === win ? 'font-bold text-brand-light' : ''}">
                    {b.mae[m] != null ? fmt(b.mae[m]) : '—'}
                  </td>
                {/each}
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if shipped?.next_gameweek}
        <p class="text-[11px] text-muted2 mt-2">
          These columns are the standalone component model. What ships for the
          <b>next</b> gameweek is {shipped.next_gameweek}.
          {#if shipped.next_gameweek_status}<b class="text-amber">{shipped.next_gameweek_status}</b>{/if}
          Beyond h=1 the shipped projection is exactly the Gaffer column.
        </p>
      {/if}
    </section>

    <!-- Retracted numbers. Deliberately above the fold: a withdrawn claim that
         is only mentioned in a footnote has not really been withdrawn. -->
    {#if retracted.length}
      <section class="card p-3 border border-amber/40 bg-amber/5">
        <h2 class="font-bold text-amber">Withdrawn: two baselines this page used to show</h2>
        <div class="space-y-3 mt-2">
          {#each retracted as w}
            <div>
              <div class="text-sm font-bold">
                {w.label}
                <span class="text-[11px] font-normal text-muted2">
                  — previously
                  {#each Object.entries(w.entry.previously_reported) as [k, v], i}{i > 0 ? ', ' : ''}{k.replace(/_/g, ' ')} {v}{/each}
                </span>
              </div>
              <p class="text-[12px] text-muted mt-1">{w.entry.reason}</p>
            </div>
          {/each}
        </div>
        {#if consequence}
          <p class="text-[12px] text-muted2 mt-3 pt-3 border-t border-line">{consequence}</p>
        {/if}
      </section>
    {/if}

    <!-- Decision-level -->
    {#if h1?.decisions}
      <section class="card p-3">
        <h2 class="font-bold">Decision-level results (h = 1)</h2>
        <p class="text-[12px] text-muted2 mb-2">
          A legal 15 under budget, quota and the three-per-club limit, then the best
          XI from it. Regret is the gap to a perfect-hindsight legal team — it is
          large for everyone, and only the comparison between methods is meaningful.
        </p>
        <div class="overflow-x-auto">
          <table class="data w-full text-sm">
            <thead>
              <tr>
                <th class="text-left">Method</th>
                <th class="text-right">XI pts/GW</th>
                <th class="text-right">XI regret</th>
                <th class="text-right">Captain pts/GW</th>
                <th class="text-right">Captain accuracy</th>
              </tr>
            </thead>
            <tbody>
              {#each Object.entries(h1.decisions) as [m, d]}
                {#if d && 'xi_points_per_gw' in d}
                  <tr>
                    <td>{label(m)}</td>
                    <td class="text-right tabular-nums">{fmt(d.xi_points_per_gw, 1)}</td>
                    <td class="text-right tabular-nums text-muted">{fmt(d.xi_regret_per_gw, 1)}</td>
                    <td class="text-right tabular-nums">{fmt(d.captain_points_per_gw, 2)}</td>
                    <td class="text-right tabular-nums">{fmt(d.captain_accuracy_pct, 1)}%</td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
        </div>
        {#if h1.transfers}
          <p class="text-[11px] text-muted2 mt-2">
            One free transfer per week vs holding the opening squad:
            {#each Object.entries(h1.transfers) as [m, t], i}
              {#if t && 'gain' in t}{i > 0 ? ' · ' : ' '}<b>{label(m)}</b> {t.gain > 0 ? '+' : ''}{t.gain} pts{/if}
            {/each}
          </p>
        {/if}
      </section>
    {/if}

    <!-- Every trained candidate, with its own verdict -->
    {#if candidates.length}
      <section class="card p-3">
        <h2 class="font-bold">Trained models: tested, none shipped</h2>
        {#if evidence?.protocol}
          <p class="text-[11px] text-muted2 mt-1">{evidence.protocol}</p>
        {/if}

        <div class="space-y-4 mt-3">
          {#each candidates as c}
            <div class="rounded-lg bg-bg3 p-3">
              <div class="flex flex-wrap items-baseline gap-2">
                <span class="font-bold">{c.label ?? c.candidate}</span>
                <span class="text-[11px] font-bold uppercase {decisionClass(c.decision)}">
                  {DECISION_LABELS[c.decision] ?? c.decision}
                </span>
                {#if c.detail}<span class="text-[11px] text-muted2">{c.detail}</span>{/if}
              </div>
              <p class="text-[12px] text-muted mt-1">{c.reason}</p>

              {#if c.per_horizon && Object.keys(c.per_horizon).length}
                <div class="overflow-x-auto mt-2">
                  <table class="data w-full text-sm">
                    <thead>
                      <tr>
                        <th class="text-left">Horizon</th>
                        <th class="text-right">Heuristic XI</th>
                        <th class="text-right">This model</th>
                        <th class="text-right">Difference</th>
                        <th class="text-right">95% interval</th>
                      </tr>
                    </thead>
                    <tbody>
                      {#each Object.entries(c.per_horizon) as [h, r]}
                        <tr>
                          <td>h = {h}</td>
                          <td class="text-right tabular-nums text-muted">{fmt(heurXI[h], 2)}</td>
                          <td class="text-right tabular-nums">{fmt(r.candidate_xi, 2)}</td>
                          <td class="text-right tabular-nums {r.diff > 0 ? 'text-green' : 'text-red'}">
                            {signed(r.diff)}
                          </td>
                          <td class="text-right tabular-nums text-muted2">
                            [{fmt(r.ci95?.[0], 2)}, {fmt(r.ci95?.[1], 2)}]
                          </td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                </div>
              {/if}

              {#if c.captain_accuracy_pct_h1 != null}
                <p class="text-[11px] text-muted2 mt-2">
                  Captaincy at h=1: {fmt(c.captain_accuracy_pct_h1, 1)}% accurate
                  {#if evidence?.heuristic_reference?.captain_accuracy_pct_h1}
                    (heuristic {fmt(evidence.heuristic_reference.captain_accuracy_pct_h1, 1)}%)
                  {/if}
                  {#if c.captain_regret_per_gw_h1 != null}
                    · regret {fmt(c.captain_regret_per_gw_h1, 2)}
                  {/if}
                </p>
              {/if}

              {#if c.limitations?.length}
                <ul class="text-[11px] text-muted2 mt-2 list-disc pl-4 space-y-1">
                  {#each c.limitations as l}<li>{l}</li>{/each}
                </ul>
              {/if}
            </div>
          {/each}
        </div>

        {#if evidence?.not_ruled_out}
          <p class="text-[11px] text-muted2 mt-3 pt-3 border-t border-line">
            Not ruled out: {evidence.not_ruled_out}
          </p>
        {/if}
      </section>
    {/if}

    <!-- Frozen-baseline comparison -->
    {#if bt.baselines}
      <section class="card p-3">
        <h2 class="font-bold mb-2">Against frozen baselines</h2>
        <div class="overflow-x-auto">
          <table class="data w-full text-sm">
            <thead>
              <tr>
                <th class="text-left">Baseline</th>
                {#each Object.keys(Object.values(bt.baselines)[0] ?? {}) as k}
                  <th class="text-right">{k.replace(/_/g, ' ')}</th>
                {/each}
              </tr>
            </thead>
            <tbody>
              {#each Object.entries(bt.baselines) as [name, vals]}
                <tr>
                  <td>{name}</td>
                  {#each Object.values(vals) as v}
                    <td class="text-right tabular-nums">{typeof v === 'number' ? fmt(v) : v}</td>
                  {/each}
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    {/if}

    <!-- Calibration -->
    {#if cal.length}
      <section class="card p-3">
        <h2 class="font-bold mb-2">Calibration (h = 1)</h2>
        <p class="text-[12px] text-muted2 mb-2">
          Players binned by prediction. A calibrated model sits on the diagonal.
        </p>
        <div class="overflow-x-auto">
          <table class="data w-full text-sm">
            <thead>
              <tr>
                <th class="text-right">Predicted</th>
                <th class="text-right">Actual</th>
                <th class="text-right">Haul rate</th>
                <th class="text-right">n</th>
              </tr>
            </thead>
            <tbody>
              {#each cal as b}
                <tr>
                  <td class="text-right tabular-nums">{fmt(b.pred, 2)}</td>
                  <td class="text-right tabular-nums">{fmt(b.actual, 2)}</td>
                  <td class="text-right tabular-nums text-muted">{fmt(b.haul_rate, 1)}%</td>
                  <td class="text-right tabular-nums text-muted2">{b.n}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    {/if}

    <!-- Limitations: prominent, not a footnote -->
    <section class="card p-3 border border-yellow/30 bg-yellow/5">
      <h2 class="font-bold text-yellow mb-2">What these numbers do not prove</h2>
      <ul class="text-[13px] space-y-1.5 leading-relaxed">
        {#each bt.limitations as lim}
          <li class="flex gap-2"><span class="text-yellow shrink-0">·</span><span>{lim}</span></li>
        {/each}
      </ul>
    </section>

    <p class="text-[11px] text-muted2">
      {bt.dataset} · generated {bt.generated_at} · schema v{bt.schema_version}
    </p>
  {/if}
</div>
