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
  // G20 — the same bins restricted to players who actually featured.
  const calPlayed = $derived(bt?.calibration?.appeared ?? [])
  const retracted = $derived(bt ? withdrawn(bt) : [])
  const consequence = $derived(bt ? withdrawalConsequence(bt) : null)
  // EVERY candidate, not just the losing one.
  const candidates = $derived(bt ? modelCandidates(bt) : [])
  const evidence = $derived(bt?.model_candidates ?? null)
  const heurXI = $derived(evidence?.heuristic_reference?.xi_points_per_gw ?? {})
  const shipped = $derived(bt?.shipped_projection ?? null)
  // GW1 alone. Rendered apart from the horizon table because it is a different
  // regime, not the first row of one.
  const pre = $derived(
    bt?.pre_season && 'n' in bt.pre_season ? bt.pre_season : null)

  // ── Colour ────────────────────────────────────────────────────────────────
  // Colour has exactly one job on this page: it marks the result of a
  // comparison.
  //   blue  — the better of two things measured against each other
  //   red   — failed the test it was given
  //   amber — cannot be read as a result either way (withdrawn, unmeasured,
  //           inconclusive, or qualified by the text beside it)
  // Nothing is coloured for being large, headline or ours. The brand green in
  // particular is never used on a number here: green reads as "Gaffer", so
  // marking the winning column in it made the naive baseline's wins look like
  // ours — on the one page that exists to say how wrong we have been. Blue is
  // deliberately nobody's colour.
  const WIN = 'font-bold text-accent-light'

  const decisionClass = (d: string) =>
    d === 'shipped' ? 'text-accent-light'
      : d === 'rejected' ? 'text-red'
        : d === 'inconclusive' || d === 'invalid_experiment' ? 'text-amber'
          : 'text-muted'
  const signed = (v: number) => (v > 0 ? '+' : '') + v.toFixed(2)

  const label = (m: string) => METHOD_LABELS[m] ?? m
  // METHOD_LABELS is prose ("Gaffer (component model)"), and as a column header
  // it pushed the baseline column clean off a 390px screen: the phone showed
  // Gaffer's numbers and no sign a comparison existed at all, which is the loss
  // hidden by layout rather than by wording. Short in the header, full name in
  // the caption underneath.
  const SHORT_LABELS: Record<string, string> = {
    gaffer: 'Gaffer',
    naive: 'Recent form',
  }
  const short = (m: string) => SHORT_LABELS[m] ?? label(m)
  const fmt = (v: number | null | undefined, dp = 3) =>
    v == null || Number.isNaN(v) ? '—' : v.toFixed(dp)

  // Lower is better for MAE and regret; higher is better for the rest.
  function best(vals: Record<string, number>, lower = false): string | null {
    const ks = Object.keys(vals ?? {})
    if (!ks.length) return null
    return ks.reduce((a, b) => ((lower ? vals[b] < vals[a] : vals[b] > vals[a]) ? b : a))
  }

  // Asserted in prose directly above the table, so it has to be true of this
  // artifact rather than of the artifact that existed when the sentence was
  // typed.
  const baselineSweeps = $derived(
    bt != null && hs.length > 0 && methods.length > 1
    && hs.every((h) => best(bt.per_horizon[h].rank_corr) !== 'gaffer'
      && best(bt.per_horizon[h].mae, true) !== 'gaffer'))

  // Winner per decision-table column. Derived rather than written out, because a
  // hand-typed "Gaffer wins captaincy" outlives the artifact that made it true.
  const decWin = $derived.by(() => {
    const cols: Record<string, Record<string, number>> = { xi: {}, reg: {}, cpts: {}, cacc: {} }
    for (const [m, d] of Object.entries(h1?.decisions ?? {})) {
      // Same guard the table body uses: an empty object is a method that was
      // named but never decided anything.
      if (!d || !('xi_points_per_gw' in d)) continue
      cols.xi[m] = d.xi_points_per_gw
      if (d.xi_regret_per_gw != null) cols.reg[m] = d.xi_regret_per_gw
      cols.cpts[m] = d.captain_points_per_gw
      cols.cacc[m] = d.captain_accuracy_pct
    }
    return {
      xi: best(cols.xi), reg: best(cols.reg, true),
      cpts: best(cols.cpts), cacc: best(cols.cacc),
    }
  })
  // A verdict in words, never a figure: a number in a collapsed summary would be
  // a number whose caveat is folded away underneath it.
  const decVerdict = $derived(
    !decWin.xi || !decWin.cacc
      ? 'Squad, XI and captain rebuilt from each projection and scored on what happened.'
      : decWin.xi === decWin.cacc
        ? `${short(decWin.xi)} wins on both XI points and captaincy.`
        : `${short(decWin.xi)} wins on XI points; ${short(decWin.cacc)} wins on captaincy.`)

  // ── Navigation ────────────────────────────────────────────────────────────
  // Nine sections and several thousand pixels of tables. With no index the only
  // route to the limitations was scrolling past every one of them, and nothing
  // on a phone hinted there was anything below the fold worth reaching. Buttons
  // rather than <a href="#id">, because the app is a hash router — an anchor
  // would rewrite location.hash and navigate off the page.
  const sections = $derived(
    [
      { id: 'acc-measured', label: 'What was measured', on: true },
      { id: 'acc-gw1', label: 'GW1 on its own', on: !!pre },
      { id: 'acc-horizon', label: 'Accuracy by horizon', on: true },
      { id: 'acc-withdrawn', label: 'Withdrawn numbers', on: retracted.length > 0 },
      { id: 'acc-decisions', label: 'Decision-level results', on: !!h1?.decisions },
      { id: 'acc-models', label: 'Trained models', on: candidates.length > 0 },
      { id: 'acc-baselines', label: 'Frozen baselines', on: !!bt?.baselines },
      { id: 'acc-calibration', label: 'Calibration', on: cal.length > 0 },
      { id: 'acc-limits', label: 'What this does not prove', on: true },
    ].filter((s) => s.on))
  /** 1-based position in the index. Derived so a missing section cannot desync it. */
  const num = (id: string) => sections.findIndex((s) => s.id === id) + 1

  function jump(id: string) {
    const el = document.getElementById(id)
    if (!el) return
    // Landing on a shut <details> is landing on nothing.
    if (el instanceof HTMLDetailsElement) el.open = true
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
    // Move the caret with the viewport, or a keyboard user scrolls the page and
    // then carries on tabbing from the index they just left.
    el.focus({ preventScroll: true })
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
    <!-- Provenance. Four different kinds of fact — a count, a season, an
         identifier and a check result — used to render identically at
         text-xl/font-black, so a version string read as a headline statistic and
         two of the four were tinted green for no stated reason. Label leads on
         the house `.tile` primitive; the only tile allowed colour is the check,
         in red, and only when it fails. -->
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
      <div class="tile flex flex-col justify-center">
        <div class="tile-label">Predictions scored</div>
        <div class="tile-value text-xl mt-1">{bt.coverage.rows_evaluated.toLocaleString()}</div>
      </div>
      <div class="tile flex flex-col justify-center">
        <div class="tile-label">Season held out</div>
        <div class="tile-value text-xl mt-1">{bt.season}</div>
        <div class="text-mini text-muted2">{bt.decision_gameweeks}</div>
      </div>
      <div class="tile flex flex-col justify-center">
        <div class="tile-label">Model version</div>
        <!-- An identifier, not a measurement: sized so it stops competing with
             the counts either side of it. -->
        <div class="tile-value text-base mt-1 break-all">{bt.model_version}</div>
      </div>
      <div class="tile flex flex-col justify-center">
        <div class="tile-label">Leakage check</div>
        <div class="tile-value text-xl mt-1 {leakageClean(bt) ? '' : 'text-red'}">
          {leakageClean(bt) ? 'clean' : 'FAILED'}
        </div>
      </div>
    </div>

    <!-- The one rule, stated. An unstated colour convention is how green came to
         mean "good", "big", "ours" and "the number I felt like emphasising" all
         at once. -->
    <p class="text-mini text-muted2 leading-relaxed">
      <b class="text-muted">Colour here marks the result of a comparison, and nothing else.</b>
      <span class="text-accent-light font-bold">Blue</span> — the better of two things
      measured against each other.
      <span class="text-red font-bold">Red</span> — failed the test it was given.
      <span class="text-amber font-bold">Amber</span> — cannot be read as a result
      either way. No figure is coloured for being large, headline or ours; Gaffer's
      own green is not used on a number on this page.
    </p>

    <!-- Contents -->
    <nav id="acc-contents" tabindex="-1" class="card p-3 scroll-mt-3" aria-label="Sections of this page">
      <h2 class="tile-label">On this page</h2>
      <div class="grid grid-cols-2 sm:grid-cols-3 gap-1.5 mt-2">
        {#each sections as s, i (s.id)}
          <button
            type="button"
            class="w-full min-h-11 flex items-center gap-2 text-left rounded-lg border border-line2
                   px-3 py-2 text-xs leading-tight cursor-pointer hover:bg-card2"
            onclick={() => jump(s.id)}
          >
            <span class="tabular-nums text-muted2 shrink-0">{i + 1}</span>
            <span>{s.label}</span>
          </button>
        {/each}
      </div>
    </nav>

    <!-- Coverage -->
    <section id="acc-measured" tabindex="-1" class="card p-3 scroll-mt-3">
      <h2 class="font-bold mb-2">{num('acc-measured')} · What was measured</h2>
      <p class="text-[13px] text-muted leading-relaxed">
        <b>{bt.coverage.zero_minute_rows_retained.toLocaleString()}</b> rows
        ({bt.coverage.zero_minute_share_pct}%) are players who ended up not playing.
        They are <b>kept</b> — excluding them would mean the model knew who featured
        before it picked a team, which is what the previous harness did.
      </p>
      {#if bt.coverage.excluded}
        <ul class="text-xs text-muted2 mt-2 space-y-0.5">
          {#each Object.entries(bt.coverage.excluded) as [k, v]}
            <li>· <span class="text-muted">{k.replace(/_/g, ' ')}:</span> {v}</li>
          {/each}
        </ul>
      {/if}
      <p class="text-mini text-muted2 mt-2">
        Leakage policy: {bt.leakage_check.policy}.
        {#if !leakageClean(bt)}
          <span class="text-red">
            Post-match fields found in features:
            {bt.leakage_check.post_match_fields_in_features.join(', ')}
          </span>
        {/if}
      </p>
    </section>

    <!-- The pre-season decision, on its own -->
    {#if pre}
      <section id="acc-gw1" tabindex="-1" class="card p-3 scroll-mt-3">
        <h2 class="font-bold">{num('acc-gw1')} · GW{pre.decision_gw} — the pre-season decision</h2>
        <p class="text-xs text-muted2 mb-2">
          The one evening a whole squad is picked from scratch, measured on its
          own. {pre.regime}. Averaged into the {bt.coverage.decision_gws ?? 38}
          gameweeks below it is invisible, and before schema 6 it was not
          measured at all.
        </p>
        <!-- Surface-2 rather than card-inside-card, which read as four floating
             boxes at the same lightness as their parent. None is coloured: there
             is no baseline at GW1, so there is no comparison for colour to
             report. -->
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <div class="rounded-xl bg-bg3 px-3 py-2.5">
            <div class="tile-label">Rank correlation</div>
            <div class="tile-value text-xl mt-1">{fmt(pre.rank_corr.gaffer)}</div>
          </div>
          <div class="rounded-xl bg-bg3 px-3 py-2.5">
            <div class="tile-label">MAE, points</div>
            <div class="tile-value text-xl mt-1">{fmt(pre.mae.gaffer)}</div>
          </div>
          <div class="rounded-xl bg-bg3 px-3 py-2.5">
            <div class="tile-label">Players scored</div>
            <div class="tile-value text-xl mt-1">{pre.n.toLocaleString()}</div>
          </div>
          <div class="rounded-xl bg-bg3 px-3 py-2.5">
            <div class="tile-label">Did not play</div>
            <div class="tile-value text-xl mt-1">{pre.zero_minute_share_pct ?? '—'}%</div>
          </div>
        </div>
        <p class="text-xs text-muted mt-2">
          <b>No baseline to beat.</b> {pre.naive_baseline}
        </p>
        {#if pre.decisions_caveat}
          <p class="text-mini text-muted2 mt-2">{pre.decisions_caveat}</p>
        {/if}
      </section>
    {/if}

    <!-- Player-level accuracy, per horizon -->
    <section id="acc-horizon" tabindex="-1" class="card p-3 scroll-mt-3">
      <h2 class="font-bold">{num('acc-horizon')} · Player-level accuracy by horizon</h2>
      <p class="text-xs text-muted2 mb-2">
        h=1 is the imminent gameweek; h=6 is six weeks out from the same decision
        point, using the same information. Rank correlation is ordering quality
        (higher better); MAE is points error (lower better).
        {#if baselineSweeps}
          <b>The naive baseline beats Gaffer on both, at every horizon.</b>
          Read the note under the table before reading that as Gaffer being bad
          at football.
        {/if}
      </p>
      <div class="overflow-x-auto">
        <table class="data w-full text-sm">
          <thead>
            <tr>
              <th class="text-left">Horizon</th>
              <th class="text-right">n</th>
              {#each methods as m}<th class="text-right">{short(m)}</th>{/each}
            </tr>
          </thead>
          <tbody>
            <tr class="text-mini text-muted2"><td colspan={2 + methods.length}>Rank correlation ↑</td></tr>
            {#each hs as h}
              {@const b = bt.per_horizon[h]}
              {@const win = best(b.rank_corr)}
              <tr>
                <td>h = {h}</td>
                <td class="text-right tabular-nums text-muted">{b.n.toLocaleString()}</td>
                {#each methods as m}
                  <td class="text-right tabular-nums {m === win ? WIN : ''}">
                    {b.rank_corr[m] != null ? fmt(b.rank_corr[m]) : '—'}
                  </td>
                {/each}
              </tr>
            {/each}
            <tr class="text-mini text-muted2"><td colspan={2 + methods.length}>MAE ↓</td></tr>
            {#each hs as h}
              {@const b = bt.per_horizon[h]}
              {@const win = best(b.mae, true)}
              <tr>
                <td>h = {h}</td>
                <td class="text-right tabular-nums text-muted">{b.n.toLocaleString()}</td>
                {#each methods as m}
                  <td class="text-right tabular-nums {m === win ? WIN : ''}">
                    {b.mae[m] != null ? fmt(b.mae[m]) : '—'}
                  </td>
                {/each}
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="text-mini text-muted2 mt-2">
        Columns: {#each methods as m, i}{i > 0 ? ' · ' : ''}<b>{short(m)}</b> — {label(m)}{/each}.
        The baseline is cumulative season-to-date points per game.
      </p>

      <!-- The most misreadable pair of numbers on the site. The baseline wins
           because most scored rows are non-appearances and a season-to-date
           average encodes appearance rate for free — it is not a verdict on the
           points model. Saying so is not softening the loss: the loss is printed
           above, unhedged, and the change that would erase it is exactly the
           leakage this harness exists to prevent. -->
      <div class="mt-3 rounded-xl bg-bg3 p-3">
        <h3 class="text-xs font-bold">Why the baseline wins, and what that does and does not mean</h3>
        <p class="text-xs text-muted mt-1 leading-relaxed">
          {bt.coverage.zero_minute_share_pct}% of the scored rows are players who
          did not appear, and they are kept on purpose. Ordering that set is
          mostly the question of who plays at all — and a season-to-date average
          answers it for free, because a player who keeps not playing keeps a low
          average. Gaffer projects points and then gates them on a start
          probability, and the gate is the weak part.
        </p>
        <p class="text-xs text-muted2 mt-2 leading-relaxed">
          So the honest reading is <b>worse at predicting who appears</b>, not
          <b>worse at predicting football</b>. The trained-model section below
          measures that appearance error directly and points the same way — but
          this table does not decompose it, so it stays a reading rather than a
          measurement. Either way the baseline wins here, and dropping the
          non-appearances to close the gap would mean scoring the model on a set
          it could only know after kick-off.
        </p>
      </div>

      {#if shipped?.next_gameweek}
        <p class="text-mini text-muted2 mt-2">
          These columns are the standalone component model. What ships for the
          <b>next</b> gameweek is {shipped.next_gameweek}.
          {#if shipped.next_gameweek_status}<b class="text-amber">{shipped.next_gameweek_status}</b>{/if}
          Beyond h=1 the shipped projection is exactly the Gaffer column.
        </p>
      {/if}
    </section>

    <!-- Retracted numbers. Deliberately above the fold and never collapsed: a
         withdrawn claim mentioned only in a footnote — or only behind a tap —
         has not really been withdrawn. -->
    {#if retracted.length}
      <section id="acc-withdrawn" tabindex="-1" class="card p-3 scroll-mt-3 border border-amber/40 bg-amber/5">
        <h2 class="font-bold text-amber">
          {num('acc-withdrawn')} · Withdrawn: two baselines this page used to show
        </h2>
        <div class="space-y-3 mt-2">
          {#each retracted as w}
            <div>
              <div class="text-sm font-bold">
                {w.label}
                <span class="text-mini font-normal text-muted2">
                  — previously
                  {#each Object.entries(w.entry.previously_reported) as [k, v], i}{i > 0 ? ', ' : ''}{k.replace(/_/g, ' ')} {v}{/each}
                </span>
              </div>
              <p class="text-xs text-muted mt-1">{w.entry.reason}</p>
            </div>
          {/each}
        </div>
        {#if consequence}
          <p class="text-xs text-muted2 mt-3 pt-3 border-t border-line">{consequence}</p>
        {/if}
      </section>
    {/if}

    <!-- Decision-level. Collapsed: a second view of the same h=1 result. The
         summary states the verdict in words, so nothing numeric is asserted with
         its caveat folded away underneath it. -->
    {#if h1?.decisions}
      <details id="acc-decisions" tabindex="-1" class="card scroll-mt-3">
        <summary class="p-3 cursor-pointer">
          <span class="font-bold">{num('acc-decisions')} · Decision-level results (h = 1)</span>
          <span class="block text-mini text-muted2 mt-0.5">{decVerdict}</span>
        </summary>
        <div class="px-3 pb-3">
          <p class="text-xs text-muted2 mb-2">
            A legal 15 under budget, quota and the three-per-club limit, then the best
            XI from it. Every figure is per gameweek. Regret is the gap to a
            perfect-hindsight legal team — it is large for everyone, and only the
            comparison between methods is meaningful.
          </p>
          <!-- Headers abbreviated so all five columns fit a 390px screen, and
               spelled out above: a column no phone can see is a column this page
               has not really published. -->
          <div class="overflow-x-auto">
            <table class="data w-full text-sm">
              <thead>
                <tr>
                  <th class="text-left">Method</th>
                  <th class="text-right">XI pts</th>
                  <th class="text-right">Regret</th>
                  <th class="text-right">Capt pts</th>
                  <th class="text-right">Capt %</th>
                </tr>
              </thead>
              <tbody>
                {#each Object.entries(h1.decisions) as [m, d]}
                  {#if d && 'xi_points_per_gw' in d}
                    <tr>
                      <td>{short(m)}</td>
                      <td class="text-right tabular-nums {m === decWin.xi ? WIN : ''}">{fmt(d.xi_points_per_gw, 1)}</td>
                      <td class="text-right tabular-nums {m === decWin.reg ? WIN : 'text-muted'}">{fmt(d.xi_regret_per_gw, 1)}</td>
                      <td class="text-right tabular-nums {m === decWin.cpts ? WIN : ''}">{fmt(d.captain_points_per_gw, 2)}</td>
                      <td class="text-right tabular-nums {m === decWin.cacc ? WIN : ''}">{fmt(d.captain_accuracy_pct, 1)}%</td>
                    </tr>
                  {/if}
                {/each}
              </tbody>
            </table>
          </div>
          {#if h1.transfers}
            <p class="text-mini text-muted2 mt-2">
              One free transfer per week vs holding the opening squad:
              {#each Object.entries(h1.transfers) as [m, t], i}
                {#if t && 'gain' in t}{i > 0 ? ' · ' : ' '}<b>{short(m)}</b> {t.gain > 0 ? '+' : ''}{t.gain} pts{/if}
              {/each}
            </p>
          {/if}
        </div>
      </details>
    {/if}

    <!-- Every trained candidate, with its own verdict -->
    {#if candidates.length}
      <section id="acc-models" tabindex="-1" class="card p-3 scroll-mt-3">
        <h2 class="font-bold">{num('acc-models')} · Trained models: tested, none shipped</h2>
        {#if evidence?.outcome}
          <p class="text-xs text-muted mt-1">Outcome: <b>{evidence.outcome}</b>.</p>
        {/if}
        {#if evidence?.protocol}
          <p class="text-mini text-muted2 mt-1">{evidence.protocol}</p>
        {/if}

        <!-- One <details> per candidate. The verdict word is the finding and
             stays visible; the numbers and the limitations that qualify them
             fold away together, so no figure is ever shown with its caveat
             hidden. -->
        <div class="space-y-2 mt-3">
          {#each candidates as c}
            <details class="rounded-xl bg-bg3">
              <summary class="p-3 cursor-pointer">
                <span class="font-bold">{c.label ?? c.candidate}</span>
                <span class="text-mini font-bold uppercase ml-2 {decisionClass(c.decision)}">
                  {DECISION_LABELS[c.decision] ?? c.decision}
                </span>
                {#if c.detail}<span class="block text-mini text-muted2 mt-0.5">{c.detail}</span>{/if}
              </summary>
              <div class="px-3 pb-3">
                <p class="text-xs text-muted">{c.reason}</p>

                {#if c.per_horizon && Object.keys(c.per_horizon).length}
                  <p class="text-mini text-muted2 mt-2">Legal-XI points per gameweek.</p>
                  <div class="overflow-x-auto mt-1">
                    <table class="data w-full text-sm">
                      <thead>
                        <tr>
                          <th class="text-left">Horizon</th>
                          <th class="text-right">Heuristic</th>
                          <th class="text-right">Model</th>
                          <th class="text-right">Diff</th>
                          <th class="text-right">95% CI</th>
                        </tr>
                      </thead>
                      <!-- Blue goes on whichever of the two XI figures is higher,
                           the same rule as every other table here. The difference
                           column already carries the sign, so it does not need a
                           second colour repeating it. -->
                      <tbody>
                        {#each Object.entries(c.per_horizon) as [h, r]}
                          <tr>
                            <td>h = {h}</td>
                            <td class="text-right tabular-nums {heurXI[h] != null && r.diff < 0 ? WIN : 'text-muted'}">{fmt(heurXI[h], 2)}</td>
                            <td class="text-right tabular-nums {r.diff > 0 ? WIN : ''}">{fmt(r.candidate_xi, 2)}</td>
                            <td class="text-right tabular-nums text-muted">{signed(r.diff)}</td>
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
                  <p class="text-mini text-muted2 mt-2">
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
                  <ul class="text-mini text-muted2 mt-2 list-disc pl-4 space-y-1">
                    {#each c.limitations as l}<li>{l}</li>{/each}
                  </ul>
                {/if}
              </div>
            </details>
          {/each}
        </div>

        {#if evidence?.not_ruled_out}
          <p class="text-mini text-muted2 mt-3 pt-3 border-t border-line">
            Not ruled out: {evidence.not_ruled_out}
          </p>
        {/if}
      </section>
    {/if}

    <!-- Frozen-baseline comparison -->
    {#if bt.baselines}
      <details id="acc-baselines" tabindex="-1" class="card scroll-mt-3">
        <summary class="p-3 cursor-pointer">
          <span class="font-bold">{num('acc-baselines')} · Against frozen baselines</span>
          <span class="block text-mini text-muted2 mt-0.5">
            Reference numbers pinned in the artifact so a later run can be compared
            against this one.
          </span>
        </summary>
        {#if calPlayed.length}
          <!-- G20. Without this the middle of the Actual column dips and the
               panel reads as the model being anti-correlated with itself. -->
          <p class="px-3 pb-2 text-mini text-muted leading-relaxed">
            <b>Actual</b> counts every player-gameweek, including the majority
            where the player did not feature — a player correctly projected at
            1.3 who is then left out scores 0. So the middle of that column
            measures whether we knew <i>who would play</i>, not what they would
            score, which is why it sags rather than climbing.
            <b class="text-brand-light">Actual, if played</b> restricts the same
            bins to players who got on the pitch, and is the like-for-like read
            of the points model.
          </p>
        {/if}
        <div class="px-3 pb-3 overflow-x-auto">
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
      </details>
    {/if}

    <!-- Calibration -->
    {#if cal.length}
      <details id="acc-calibration" tabindex="-1" class="card scroll-mt-3">
        <summary class="p-3 cursor-pointer">
          <span class="font-bold">{num('acc-calibration')} · Calibration (h = 1)</span>
          <span class="block text-mini text-muted2 mt-0.5">
            Players binned by prediction, {cal.length} bins. A calibrated model sits
            on the diagonal.
          </span>
        </summary>
        <div class="px-3 pb-3 overflow-x-auto">
          <table class="data w-full text-sm">
            <thead>
              <tr>
                <th class="text-right">Predicted</th>
                <th class="text-right">Actual</th>
                {#if calPlayed.length}
                  <th class="text-right">Actual, if played</th>
                {/if}
                <th class="text-right">Haul rate</th>
                <th class="text-right">n</th>
              </tr>
            </thead>
            <tbody>
              {#each cal as b, i}
                <tr>
                  <td class="text-right tabular-nums">{fmt(b.pred, 2)}</td>
                  <td class="text-right tabular-nums">{fmt(b.actual, 2)}</td>
                  {#if calPlayed.length}
                    <td class="text-right tabular-nums text-brand-light"
                      >{calPlayed[i] ? fmt(calPlayed[i].actual, 2) : '—'}</td>
                  {/if}
                  <td class="text-right tabular-nums text-muted">{fmt(b.haul_rate, 1)}%</td>
                  <td class="text-right tabular-nums text-muted2">{b.n}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </details>
    {/if}

    <!-- Limitations: prominent, never collapsed. Amber rather than yellow only
         so the source says which of the three colour meanings this is — the two
         tokens are the same hex. -->
    <section id="acc-limits" tabindex="-1" class="card p-3 scroll-mt-3 border border-amber/30 bg-amber/5">
      <h2 class="font-bold text-amber mb-2">{num('acc-limits')} · What these numbers do not prove</h2>
      <ul class="text-[13px] space-y-1.5 leading-relaxed">
        {#each bt.limitations as lim}
          <li class="flex gap-2"><span class="text-amber shrink-0">·</span><span>{lim}</span></li>
        {/each}
      </ul>
    </section>

    <div class="flex flex-wrap items-center justify-between gap-2">
      <p class="text-mini text-muted2">
        {bt.dataset} · generated {bt.generated_at} · schema v{bt.schema_version}
      </p>
      <button
        type="button"
        class="btn btn-ghost min-h-11"
        onclick={() => jump('acc-contents')}
      >↑ Back to contents</button>
    </div>
  {/if}
</div>
