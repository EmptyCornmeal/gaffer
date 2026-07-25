<script lang="ts">
  import type { Fixtures } from '../lib/types'

  let { fixtures }: { fixtures: Fixtures } = $props()

  const rows = $derived(
    Object.entries(fixtures)
      .map(([short, v]) => ({
        short,
        ...v,
        ease: v.fixtures.reduce((s, f) => s + f.difficulty, 0) / (v.fixtures.length || 1),
      }))
      .sort((a, b) => a.ease - b.ease),
  )
  const gws = $derived(rows[0]?.fixtures.map((f) => f.gw) ?? [])
</script>

<div class="rise">
  <div class="flex items-center justify-between mb-3">
    <h2 class="font-bold text-lg">Fixture ticker</h2>
    <span class="text-xs text-muted">xGC-based difficulty · easiest run → hardest</span>
  </div>

  <div class="card overflow-x-auto">
    <div class="grid text-[11px] text-muted bg-bg2 border-b border-line" style="grid-template-columns: 60px repeat({gws.length}, minmax(46px,1fr));">
      <div class="py-2 px-2 font-semibold">Team</div>
      {#each gws as gw}<div class="py-2 text-center font-semibold">GW{gw}</div>{/each}
    </div>
    {#each rows as r}
      <div class="grid items-center border-b border-line/50" style="grid-template-columns: 60px repeat({gws.length}, minmax(46px,1fr));">
        <div class="py-1.5 px-2 text-sm font-bold">{r.short}</div>
        {#each r.fixtures as f}
          <div class="fdr-{f.difficulty} m-0.5 rounded text-center py-1.5" title="{f.opp} {f.home ? 'Home' : 'Away'} · difficulty {f.difficulty}">
            <div class="text-[11px] font-bold leading-none">{f.opp}</div>
            <div class="text-[8px] opacity-80 leading-none mt-0.5">{f.home ? 'H' : 'A'}</div>
          </div>
        {/each}
      </div>
    {/each}
  </div>

  <div class="flex items-center gap-3 mt-3 text-xs text-muted">
    <span>Difficulty:</span>
    {#each [1, 2, 3, 4, 5] as d}<span class="fdr-{d} px-2 py-0.5 rounded">{d}</span>{/each}
  </div>
</div>
