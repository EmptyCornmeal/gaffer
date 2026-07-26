<script lang="ts">
  import type { Bundle } from '../lib/data'
  import Icon from '../components/Icon.svelte'
  let { bundle }: { bundle: Bundle } = $props()
  const bt = $derived(bundle.backtest)
  const gain = $derived(bt ? Math.round(((bt.rank_corr.ml - bt.rank_corr.gaffer) / bt.rank_corr.gaffer) * 100) : 0)

  // Calibration reliability chart geometry (predicted vs actual; diagonal = ideal).
  const cal = $derived(bt?.calibration?.gaffer ?? [])
  const calMax = $derived(Math.max(6, Math.ceil(Math.max(4, ...cal.flatMap((b) => [b.pred, b.actual])))))
  const CW = 320
  const CH = 220
  const CM = { t: 12, r: 12, b: 30, l: 34 }
  const cx = (v: number) => CM.l + (v / calMax) * (CW - CM.l - CM.r)
  const cy = (v: number) => CH - CM.b - (v / calMax) * (CH - CM.t - CM.b)
  const calTicks = $derived(
    Array.from({ length: Math.floor(calMax / 2) + 1 }, (_, i) => i * 2).filter((t) => t <= calMax),
  )
  // Haul-rate bars scale to a rounded axis (next multiple of 5), NOT the max bin —
  // so 8.9% reads as ~9%, not a full bar.
  const haulAxis = $derived(Math.max(5, Math.ceil(Math.max(1, ...cal.map((b) => b.haul_rate)) / 5) * 5))
</script>

<div class="rise max-w-4xl">
  <h2 class="font-bold text-lg mb-1 flex items-center gap-2"><Icon name="target" size={18} /> Model Accuracy — backtested</h2>
  <p class="text-sm text-muted mb-4">
    We hold ourselves accountable. Every method below was tested on a season it
    never trained on — no tool should ask you to trust a number it can't stand behind.
  </p>

  {#if !bt}
    <div class="card p-6 text-center text-muted text-sm">Backtest not generated yet.</div>
  {:else}
    <div class="grid sm:grid-cols-3 gap-3 mb-4">
      <div class="card p-3 text-center"><div class="text-2xl font-black text-brand-light">{bt.n_predictions.toLocaleString()}</div><div class="text-xs text-muted">predictions tested</div></div>
      <div class="card p-3 text-center"><div class="text-2xl font-black">{bt.season}</div><div class="text-xs text-muted">held-out · {bt.gameweeks}</div></div>
      <div class="card p-3 text-center"><div class="text-2xl font-black text-accent-light">+{gain}%</div><div class="text-xs text-muted">ML vs heuristic ranking</div></div>
    </div>

    <!-- the Phase-2 headline -->
    <div class="card p-4 mb-4 border-brand/40 bg-brand/8">
      <div class="text-xs font-bold uppercase tracking-wider text-brand-light mb-1 flex items-center gap-1.5"><Icon name="zap" size={13} /> The trained model (Phase 2) is here</div>
      <p class="text-[15px] text-text">
        A gradient-boosted model trained on <b>{bt.trained_on}</b> and tested on
        <b>{bt.season}</b> (never seen in training) beats the transparent heuristic on
        every measure — ordering players <b>{gain}% better</b> (rank {bt.rank_corr.ml} vs
        {bt.rank_corr.gaffer}), with lower error ({bt.mae.ml} vs {bt.mae.gaffer} pts).
        Its top-20% picks averaged <b>{bt.lift.ml.top}</b> pts vs <b>{bt.lift.ml.bottom}</b> for the bottom.
      </p>
    </div>

    <!-- comparison table -->
    <div class="card overflow-x-auto mb-4">
      <table class="data">
        <thead><tr><th>Metric</th><th class="text-brand-light">Gaffer ML</th><th>Heuristic</th><th>FPL's own xP</th><th>Naive</th></tr></thead>
        <tbody>
          <tr>
            <td>Rank correlation <span class="text-muted2">(orders players; higher better)</span></td>
            <td class="font-bold text-brand-light">{bt.rank_corr.ml}</td>
            <td>{bt.rank_corr.gaffer}</td>
            <td>{bt.rank_corr.fpl_xp}</td>
            <td class="text-muted2">{bt.rank_corr.naive}</td>
          </tr>
          <tr>
            <td>Avg error / MAE <span class="text-muted2">(pts; lower better)</span></td>
            <td class="font-bold text-brand-light">{bt.mae.ml}</td>
            <td>{bt.mae.gaffer}</td>
            <td>{bt.mae.fpl_xp}</td>
            <td class="text-muted2">{bt.mae.naive}</td>
          </tr>
          <tr>
            <td>Top-20% picks avg pts</td>
            <td class="font-bold text-brand-light">{bt.lift.ml.top}</td>
            <td>{bt.lift.gaffer.top}</td>
            <td>{bt.lift.fpl_xp.top}</td>
            <td class="text-muted2">—</td>
          </tr>
          <tr>
            <td>Bottom-20% picks avg pts</td>
            <td class="font-bold">{bt.lift.ml.bottom}</td>
            <td>{bt.lift.gaffer.bottom}</td>
            <td>{bt.lift.fpl_xp.bottom}</td>
            <td class="text-muted2">—</td>
          </tr>
          {#if bt.team_points}
            <tr>
              <td>Top-XI actual pts / GW <span class="text-muted2">(field this model's picks)</span></td>
              <td class="font-bold text-brand-light">{bt.team_points.ml}</td>
              <td>{bt.team_points.gaffer}</td>
              <td>{bt.team_points.fpl_xp}</td>
              <td class="text-muted2">{bt.team_points.naive}</td>
            </tr>
          {/if}
        </tbody>
      </table>
    </div>

    <!-- calibration: is the model honest about its numbers? -->
    {#if cal.length}
      <div class="card p-4 mb-4">
        <h3 class="font-bold mb-1">Calibration <span class="text-xs text-muted font-normal">— does a projection of N actually score ~N?</span></h3>
        <p class="text-[11px] text-muted2 mb-3">Players binned by projected points ({bt.season}, held out). On the diagonal = calibrated; bars show how often each bin actually returned a 10+ haul. The gap most tools never show you.</p>
        <div class="grid sm:grid-cols-2 gap-5">
          <!-- reliability curve -->
          <div>
            <svg viewBox="0 0 {CW} {CH}" class="w-full h-auto" role="img" aria-label="Predicted vs actual points calibration">
              <!-- gridlines + axis ticks -->
              {#each calTicks as t}
                <line x1={cx(t)} y1={CM.t} x2={cx(t)} y2={cy(0)} class="stroke-line/50" stroke-width="1" />
                <line x1={CM.l} y1={cy(t)} x2={cx(calMax)} y2={cy(t)} class="stroke-line/50" stroke-width="1" />
                <text x={cx(t)} y={CH - 18} text-anchor="middle" class="fill-muted2 text-[9px]">{t}</text>
                <text x={CM.l - 5} y={cy(t) + 3} text-anchor="end" class="fill-muted2 text-[9px]">{t}</text>
              {/each}
              <!-- ideal diagonal (perfect calibration) -->
              <line x1={cx(0)} y1={cy(0)} x2={cx(calMax)} y2={cy(calMax)} class="stroke-muted2" stroke-width="1.25" stroke-dasharray="4 3" />
              <!-- model curve -->
              <polyline points={cal.map((b) => `${cx(b.pred)},${cy(b.actual)}`).join(' ')} fill="none" class="stroke-brand" stroke-width="2" />
              {#each cal as b}
                <circle cx={cx(b.pred)} cy={cy(b.actual)} r="3.5" class="fill-brand-light"><title>predicted {b.pred} → actual {b.actual} pts (n={b.n})</title></circle>
              {/each}
              <text x={CM.l + (CW - CM.l - CM.r) / 2} y={CH - 4} text-anchor="middle" class="fill-muted text-[10px] font-semibold">predicted pts</text>
              <text x={9} y={CM.t + (cy(0) - CM.t) / 2} transform="rotate(-90 9 {CM.t + (cy(0) - CM.t) / 2})" text-anchor="middle" class="fill-muted text-[10px] font-semibold">actual pts</text>
            </svg>
            <div class="flex items-center justify-center gap-4 mt-1 text-[10px]">
              <span class="flex items-center gap-1.5"><span class="w-4 h-[2px] bg-brand inline-block"></span>Gaffer</span>
              <span class="flex items-center gap-1.5"><span class="w-4 border-t border-dashed border-muted2 inline-block"></span>perfect (predicted = actual)</span>
            </div>
          </div>
          <!-- haul rate by bin -->
          <div class="flex flex-col justify-center gap-1.5">
            <div class="text-[11px] text-muted mb-0.5">Actual 10+ haul rate by projection bin <span class="text-muted2">(scale 0–{haulAxis}%)</span></div>
            {#each cal as b}
              <div class="flex items-center gap-2">
                <span class="text-[10px] text-muted2 w-8 tabular-nums text-right">{b.pred.toFixed(1)}</span>
                <div class="flex-1 h-3.5 rounded bg-bg3 overflow-hidden"><div class="h-full bg-brand/80 rounded" style="width:{(b.haul_rate / haulAxis) * 100}%"></div></div>
                <span class="text-[10px] text-brand-light w-9 tabular-nums">{b.haul_rate}%</span>
              </div>
            {/each}
          </div>
        </div>
      </div>
    {/if}

    <div class="card p-4 text-sm text-muted leading-relaxed">
      <b class="text-text">The honest read:</b> the trained model is a clear step up on
      the heuristic and now sits between it and FPL's own model ({bt.rank_corr.fpl_xp}) —
      which has data we don't (their minutes model, ICT, set-piece and penalty info).
      Next: feed the model more of those signals to close the rest of the gap, and switch
      the live projections onto it once the season has enough gameweeks of form data.
      <p class="mt-2 text-xs text-muted2">{bt.note}</p>
    </div>
  {/if}
</div>
