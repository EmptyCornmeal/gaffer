<script lang="ts">
  import type { TeamFixture } from '../lib/types'
  import type { FixtureMode, FixturesArtifact } from '../lib/fixtures'
  import {
    buildRotation, buildRows, difficultyOf, gameweeksIn, parseFixtures,
  } from '../lib/fixtures'

  let { fixtures }: { fixtures: FixturesArtifact } = $props()

  // One guard between the artifact and this page. The artifact carries team
  // records *and* metadata (`season`), and treating every value as a team is
  // what took the whole route down with a TypeError.
  const parsed = $derived(parseFixtures(fixtures))
  const teams = $derived(parsed.kind === 'ok' ? parsed.teams : [])


  // Split fixture difficulty: overall, attack (ease of scoring — opponent defence)
  // or defence (ease of a clean sheet — opponent attack). Scoriness/Porosity.
  let mode = $state<FixtureMode>('difficulty')
  const MODES: { key: FixtureMode; label: string; hint: string }[] = [
    { key: 'difficulty', label: 'Overall', hint: 'blend of both' },
    { key: 'att', label: 'Attack', hint: 'ease of scoring' },
    { key: 'def', label: 'Defence', hint: 'ease of a clean sheet' },
  ]
  const diffOf = (f: TeamFixture): number => difficultyOf(f, mode)

  // Rotation-pair planner: pin 2–3 teams and see the *best-of* their fixtures each
  // GW — i.e. the effective difficulty if you rotated them through one squad slot.
  let pinned = $state<string[]>([])
  function togglePin(short: string) {
    if (pinned.includes(short)) pinned = pinned.filter((s) => s !== short)
    else if (pinned.length < 3) pinned = [...pinned, short]
  }

  // Union of every GW any team plays in — so doubles/blanks don't shift the grid.
  const gws = $derived(gameweeksIn(teams))

  const rows = $derived(buildRows(teams, gws, mode))

  // For each header GW, the easiest of the pinned teams' fixtures (a blank counts
  // as difficulty 6). The row shows which team you'd field and how the rotation
  // smooths the run vs owning either team alone.
  const rotation = $derived(buildRotation(teams, gws, pinned, mode))
</script>

{#if parsed.kind === 'unavailable'}
  <!-- Fail visibly. Silently rendering an empty grid — or leaving the previous
       route on screen — is how a broken artifact reads as "no fixtures". -->
  <div class="rise max-w-xl mx-auto">
    <h2 class="font-bold text-lg mb-2">Fixture ticker</h2>
    <div class="card p-4 border border-red/40 bg-red/5">
      <div class="font-bold text-red">Fixtures unavailable</div>
      <p class="text-sm text-muted mt-1">{parsed.reason}</p>
      <p class="text-[11px] text-muted2 mt-2">
        This is a problem with the published <code>fixtures.json</code>, not with
        your team. Everything else on the site is unaffected.
      </p>
    </div>
  </div>
{:else}
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
      <span class="text-xs text-muted hidden sm:inline">{MODES.find((m) => m.key === mode)?.hint}</span>
    </div>
  </div>

  <!-- legend + hints up top, where they're needed (not buried under 20 rows) -->
  <div class="flex items-center gap-x-4 gap-y-1.5 mb-3 text-[11px] text-muted flex-wrap">
    <span class="flex items-center gap-1">Difficulty {#each [1, 2, 3, 4, 5] as d}<span class="fdr-{d} w-5 h-5 rounded flex items-center justify-center font-bold">{d}</span>{/each}</span>
    <span class="text-muted2">×2 = double · – = blank</span>
    <span class="text-muted2">Rows sorted best fixture-run → worst · tap <span class="text-muted">+</span> to plan a rotation</span>
  </div>

  {#if rotation}
    <div class="card p-3 mb-3">
      <div class="flex items-center justify-between mb-2 gap-2 flex-wrap">
        <div class="text-sm font-bold flex items-center gap-2">
          Rotation: {pinned.join(' / ')}
          <span class="text-xs font-normal text-muted">best-of each GW · avg {rotation.ease}</span>
        </div>
        <button onclick={() => (pinned = [])} class="text-xs text-muted hover:text-text">clear</button>
      </div>
      <div class="overflow-x-auto">
        <div class="grid" style="grid-template-columns: repeat({gws.length}, minmax(46px,1fr));">
          {#each rotation.cells as c, i}
            <div class="m-0.5">
              <div class="text-[10px] text-muted text-center leading-none mb-0.5">GW{gws[i]}</div>
              {#if c}
                <div class="fdr-{diffOf(c.f)} rounded text-center py-1.5" title="Field {c.short}: {c.f.opp} {c.f.home ? 'Home' : 'Away'} · difficulty {diffOf(c.f)}">
                  <div class="text-[10px] font-bold leading-none">{c.short}</div>
                  <div class="text-[8px] opacity-80 leading-none mt-0.5">v {c.f.opp} · {diffOf(c.f)}</div>
                </div>
              {:else}
                <div class="rounded text-center py-1.5 bg-bg3/60 text-muted2" title="Both blank"><div class="text-[11px] font-bold leading-none">–</div></div>
              {/if}
            </div>
          {/each}
        </div>
      </div>
      <div class="text-[11px] text-muted mt-1.5">Pin up to 3 teams (📌) to plan a rotation slot — each GW shows the easier fixture you'd field.</div>
    </div>
  {/if}

  <div class="card overflow-x-auto">
    <div class="grid text-[11px] text-muted bg-bg2 border-b border-line" style="grid-template-columns: 60px repeat({gws.length}, minmax(46px,1fr));">
      <div class="py-2 px-2 font-semibold">Team</div>
      {#each gws as gw}<div class="py-2 text-center font-semibold">GW{gw}</div>{/each}
    </div>
    {#each rows as r}
      <div class="grid items-stretch border-b border-line/50" style="grid-template-columns: 60px repeat({gws.length}, minmax(46px,1fr));">
        <div class="py-1.5 px-2 text-sm font-bold flex items-center gap-1">
          <button
            onclick={() => togglePin(r.short)}
            title={pinned.includes(r.short) ? 'Remove from rotation' : 'Pin to rotation (max 3)'}
            class="text-[10px] leading-none {pinned.includes(r.short) ? 'text-brand' : 'text-muted2 hover:text-muted'}"
          >{pinned.includes(r.short) ? '📌' : '+'}</button>
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
                <div class="fdr-{diffOf(f)} rounded relative text-center py-1.5" title="{f.opp} {f.home ? 'Home' : 'Away'} · difficulty {diffOf(f)}">
                  <!-- difficulty number in the corner so it's legible without relying on hue (colour-blind a11y) -->
                  <div class="absolute top-0.5 right-1 text-[9px] font-black leading-none opacity-90">{diffOf(f)}</div>
                  <div class="text-[11px] font-bold leading-none">{f.opp}</div>
                  <div class="text-[8px] opacity-75 leading-none mt-0.5">{f.home ? 'Home' : 'Away'}</div>
                </div>
              {/each}
            </div>
          {/if}
        {/each}
      </div>
    {/each}
  </div>
</div>
{/if}
