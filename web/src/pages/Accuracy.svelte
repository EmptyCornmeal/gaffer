<script lang="ts">
  import type { Bundle } from '../lib/data'
  let { bundle }: { bundle: Bundle } = $props()
  const bt = $derived(bundle.backtest)

  // bar width helper (rank corr 0..1)
  const pct = (v: number, max = 0.7) => Math.max(2, Math.min(100, (v / max) * 100))
</script>

<div class="rise max-w-4xl">
  <h2 class="font-bold text-lg mb-1">📊 Model Accuracy — backtested</h2>
  <p class="text-sm text-muted mb-4">
    We hold ourselves accountable. The projection was tested on real history — no
    tool should ask you to trust a number it can't stand behind.
  </p>

  {#if !bt}
    <div class="card p-6 text-center text-muted text-sm">Backtest not generated yet.</div>
  {:else}
    <div class="grid sm:grid-cols-3 gap-3 mb-4">
      <div class="card p-3 text-center"><div class="text-2xl font-black text-brand-light">{bt.n_predictions.toLocaleString()}</div><div class="text-xs text-muted">predictions tested</div></div>
      <div class="card p-3 text-center"><div class="text-2xl font-black">{bt.season}</div><div class="text-xs text-muted">{bt.gameweeks}</div></div>
      <div class="card p-3 text-center"><div class="text-2xl font-black text-accent-light">×{(bt.lift.gaffer.top / bt.lift.gaffer.bottom).toFixed(1)}</div><div class="text-xs text-muted">top vs bottom picks</div></div>
    </div>

    <!-- headline lift -->
    <div class="card p-4 mb-4 border-brand/40 bg-brand/8">
      <div class="text-xs font-bold uppercase tracking-wider text-brand-light mb-1">Does it separate good picks from bad?</div>
      <p class="text-[15px] text-text">
        Players Gaffer ranked in the <b class="text-brand-light">top 20%</b> averaged
        <b>{bt.lift.gaffer.top}</b> pts a gameweek; the <b class="text-red">bottom 20%</b>
        averaged just <b>{bt.lift.gaffer.bottom}</b>. So the model's best picks returned
        <b>{(bt.lift.gaffer.top / bt.lift.gaffer.bottom).toFixed(1)}×</b> the worst — genuine signal, not noise.
      </p>
    </div>

    <!-- comparison table -->
    <div class="card overflow-x-auto mb-4">
      <table class="data">
        <thead><tr><th>Metric</th><th>Gaffer</th><th>FPL's own xP</th><th>Naive (recent form)</th></tr></thead>
        <tbody>
          <tr>
            <td>Rank correlation <span class="text-muted2">(orders players; higher better)</span></td>
            <td class="font-bold text-brand-light">{bt.rank_corr.gaffer}</td>
            <td>{bt.rank_corr.fpl_xp}</td>
            <td class="text-muted">{bt.rank_corr.naive}</td>
          </tr>
          <tr>
            <td>Avg error / MAE <span class="text-muted2">(pts; lower better)</span></td>
            <td class="font-bold text-brand-light">{bt.mae.gaffer}</td>
            <td>{bt.mae.fpl_xp}</td>
            <td class="text-muted">{bt.mae.naive}</td>
          </tr>
          <tr>
            <td>Top-20% picks avg pts</td>
            <td class="font-bold text-brand-light">{bt.lift.gaffer.top}</td>
            <td>{bt.lift.fpl_xp.top}</td>
            <td class="text-muted2">—</td>
          </tr>
          <tr>
            <td>Bottom-20% picks avg pts</td>
            <td class="font-bold">{bt.lift.gaffer.bottom}</td>
            <td>{bt.lift.fpl_xp.bottom}</td>
            <td class="text-muted2">—</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="card p-4 text-sm text-muted leading-relaxed">
      <b class="text-text">The honest read:</b> Gaffer beats a naive recent-form
      baseline on point error, and clearly separates good picks from bad — but on
      ranking it trails FPL's own model ({bt.rank_corr.fpl_xp}). That gap is the whole
      point of this page: it's why the trained model (Phase 2) is the priority, and
      this page will track us closing it.
      <p class="mt-2 text-xs text-muted2">{bt.note}</p>
    </div>
  {/if}
</div>
