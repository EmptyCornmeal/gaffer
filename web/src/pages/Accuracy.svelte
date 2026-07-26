<script lang="ts">
  import type { Bundle } from '../lib/data'
  import Icon from '../components/Icon.svelte'
  let { bundle }: { bundle: Bundle } = $props()
  const bt = $derived(bundle.backtest)
  const gain = $derived(bt ? Math.round(((bt.rank_corr.ml - bt.rank_corr.gaffer) / bt.rank_corr.gaffer) * 100) : 0)
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
        </tbody>
      </table>
    </div>

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
