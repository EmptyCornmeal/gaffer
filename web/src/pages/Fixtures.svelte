<script lang="ts">
  import type { Fixtures, TeamFixture } from '../lib/types'

  let { fixtures }: { fixtures: Fixtures } = $props()

  // Split fixture difficulty: overall, attack (ease of scoring — opponent defence)
  // or defence (ease of a clean sheet — opponent attack). Scoriness/Porosity.
  type Mode = 'difficulty' | 'att' | 'def'
  let mode = $state<Mode>('difficulty')
  const MODES: { key: Mode; label: string; hint: string }[] = [
    { key: 'difficulty', label: 'Overall', hint: 'blend of both' },
    { key: 'att', label: 'Attack', hint: 'ease of scoring' },
    { key: 'def', label: 'Defence', hint: 'ease of a clean sheet' },
  ]
  const diffOf = (f: TeamFixture): number => (f[mode] ?? f.difficulty) as number

  // Union of every GW any team plays in — so doubles/blanks don't shift the grid.
  const gws = $derived(
    [...new Set(Object.values(fixtures).flatMap((v) => v.fixtures.map((f) => f.gw)))].sort(
      (a, b) => a - b,
    ),
  )

  const rows = $derived(
    Object.entries(fixtures)
      .map(([short, v]) => {
        // one cell per header GW: 0 fixtures = blank, 2+ = double
        const byGw = new Map<number, TeamFixture[]>()
        for (const f of v.fixtures) {
          const list = byGw.get(f.gw) ?? []
          list.push(f)
          byGw.set(f.gw, list)
        }
        const cells = gws.map((gw) => byGw.get(gw) ?? [])
        const played = v.fixtures.length
        // ease = mean difficulty, with blanks penalised and doubles rewarded so
        // the sort still means "best run" once BGWs/DGWs appear.
        const sumDiff = v.fixtures.reduce((s, f) => s + diffOf(f), 0)
        const blanks = cells.filter((c) => c.length === 0).length
        const ease = (sumDiff + blanks * 6) / (gws.length || 1)
        return { short, cells, played, blanks, doubles: cells.filter((c) => c.length > 1).length, ease }
      })
      .sort((a, b) => a.ease - b.ease),
  )
</script>

<div class="rise">
  <div class="flex items-center justify-between mb-3 gap-2 flex-wrap">
    <h2 class="font-bold text-lg">Fixture ticker</h2>
    <div class="flex items-center gap-2">
      <div class="inline-flex items-center gap-0.5 rounded-lg border border-line bg-bg2 p-0.5">
        {#each MODES as m}
          <button
            onclick={() => (mode = m.key)}
            title={m.hint}
            class="px-2.5 py-1 rounded-md text-xs font-bold transition
              {mode === m.key ? 'bg-brand text-[#05210f]' : 'text-muted hover:text-text'}"
          >{m.label}</button>
        {/each}
      </div>
      <span class="text-xs text-muted hidden sm:inline">{MODES.find((m) => m.key === mode)?.hint} · easiest → hardest</span>
    </div>
  </div>

  <div class="card overflow-x-auto">
    <div class="grid text-[11px] text-muted bg-bg2 border-b border-line" style="grid-template-columns: 60px repeat({gws.length}, minmax(46px,1fr));">
      <div class="py-2 px-2 font-semibold">Team</div>
      {#each gws as gw}<div class="py-2 text-center font-semibold">GW{gw}</div>{/each}
    </div>
    {#each rows as r}
      <div class="grid items-stretch border-b border-line/50" style="grid-template-columns: 60px repeat({gws.length}, minmax(46px,1fr));">
        <div class="py-1.5 px-2 text-sm font-bold flex items-center gap-1">
          {r.short}
          {#if r.doubles}<span class="text-[8px] text-brand-light" title="Double gameweek">×2</span>{/if}
        </div>
        {#each r.cells as cell}
          {#if cell.length === 0}
            <!-- blank gameweek -->
            <div class="m-0.5 rounded text-center py-1.5 bg-bg3/60 text-muted2" title="Blank — no fixture">
              <div class="text-[11px] font-bold leading-none">–</div>
            </div>
          {:else}
            <div class="m-0.5 flex flex-col gap-0.5">
              {#each cell as f}
                <div class="fdr-{diffOf(f)} rounded text-center py-1.5" title="{f.opp} {f.home ? 'Home' : 'Away'} · difficulty {diffOf(f)}">
                  <div class="text-[11px] font-bold leading-none">{f.opp}</div>
                  <div class="text-[8px] opacity-80 leading-none mt-0.5">{f.home ? 'H' : 'A'}</div>
                </div>
              {/each}
            </div>
          {/if}
        {/each}
      </div>
    {/each}
  </div>

  <div class="flex items-center gap-3 mt-3 text-xs text-muted flex-wrap">
    <span>Difficulty:</span>
    {#each [1, 2, 3, 4, 5] as d}<span class="fdr-{d} px-2 py-0.5 rounded">{d}</span>{/each}
    <span class="ml-2">×2 = double</span>
    <span>– = blank</span>
  </div>
</div>
