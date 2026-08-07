<script lang="ts">
  import type { Player, Pos } from '../lib/types'
  import FixtureStrip from '../components/FixtureStrip.svelte'
  import Crest from '../components/Crest.svelte'
  import Compare from '../components/Compare.svelte'
  import { matches } from '../lib/search'

  let { players, onpick }: { players: Player[]; onpick: (id: number) => void } = $props()

  // Compare tray: pick up to 3 players (checkbox per row) → radar + percentile
  // comparison overlay. Stops the row-click (which opens the single-player modal).
  let compareIds = $state<number[]>([])
  let showCompare = $state(false)
  const compareSet = $derived(new Set(compareIds))
  function toggleCompare(id: number) {
    if (compareSet.has(id)) compareIds = compareIds.filter((x) => x !== id)
    else if (compareIds.length < 3) compareIds = [...compareIds, id]
  }
  const comparePlayers = $derived(
    compareIds.map((id) => players.find((p) => p.id === id)).filter((p): p is Player => !!p),
  )

  let query = $state('')
  let pos = $state<'ALL' | Pos>('ALL')
  // Cap tracks the actual most-expensive player so premiums (Haaland £15.5) are
  // never clamped out of the list — the old fixed max=15 hid him entirely.
  const priceCap = $derived(Math.max(15, ...players.map((p) => p.price)))
  // Starts unset so the filter tracks the real cap; `effectiveMax` below reads
  // the derived cap until the user actually moves the slider. Seeding a $state
  // from a prop froze it at the first render's value.
  let maxPrice = $state<number | null>(null)
  const effectiveMax = $derived(maxPrice ?? priceCap)
  let onlyStarters = $state(false)
  // Differentials finder: sub-10%-owned players with a real projection, so the
  // list surfaces punts the crowd hasn't found rather than 0-minute noise.
  let onlyDiff = $state(false)
  let sortKey = $state<keyof Player | 'value'>('next_gw_xp')
  let sortDir = $state<1 | -1>(-1)

  const positions: ('ALL' | Pos)[] = ['ALL', 'GKP', 'DEF', 'MID', 'FWD']
  const cols: { key: keyof Player | 'value'; label: string }[] = [
    { key: 'next_gw_xp', label: 'xP' },
    { key: 'xp_window', label: '6GW' },
    { key: 'value', label: 'Val' },
    { key: 'price', label: '£' },
    { key: 'owned_by', label: 'Own%' },
    { key: 'form', label: 'Form' },
    { key: 'ict', label: 'ICT' },
    { key: 'xgi90', label: 'xGI90' },
    { key: 'defcon90', label: 'DC90' },
  ]
  function val(p: Player, k: keyof Player | 'value'): number {
    if (k === 'value') return p.price ? p.next_gw_xp / p.price : 0
    return (p[k] as number) ?? 0
  }
  function sort(k: keyof Player | 'value') {
    if (sortKey === k) sortDir = (sortDir * -1) as 1 | -1
    else {
      sortKey = k
      sortDir = -1
    }
  }
  const filtered = $derived(
    players
      .filter((p) => pos === 'ALL' || p.pos === pos)
      .filter((p) => p.price <= effectiveMax)
      .filter((p) => !onlyStarters || p.p_start >= 0.6)
      .filter((p) => !onlyDiff || (p.owned_by < 10 && p.next_gw_xp >= 3 && p.p_start >= 0.5))
      .filter((p) => matches(p, query))
      .slice()
      .sort((a, b) =>
        sortKey === 'name'
          ? a.name.localeCompare(b.name) * -sortDir
          : (val(a, sortKey) - val(b, sortKey)) * sortDir,
      ),
  )
  const rows = $derived(filtered.slice(0, 200))
</script>

<div class="flex flex-col gap-3 rise">
  <div class="flex flex-wrap items-center gap-2">
    <input bind:value={query} placeholder="Search…" class="rounded-lg bg-card border border-line px-3 py-1.5 text-sm w-40 focus:outline-none focus:border-accent" />
    <div class="flex gap-1">
      {#each positions as p}
        <button onclick={() => (pos = p)} class="px-2.5 py-1 rounded-lg text-xs font-bold border transition {pos === p ? 'bg-accent/15 text-accent-light border-accent/40' : 'bg-card border-line text-muted'}">{p}</button>
      {/each}
    </div>
    <label class="flex items-center gap-2 text-xs text-muted">
      max £{effectiveMax.toFixed(1)}
      <input type="range" min="4" max={priceCap} step="0.5" value={effectiveMax} oninput={(e) => (maxPrice = +e.currentTarget.value)} class="accent-brand" aria-label="Maximum price" />
    </label>
    <label class="flex items-center gap-1.5 text-xs text-muted">
      <input type="checkbox" bind:checked={onlyStarters} class="accent-brand" /> likely starters
    </label>
    <label class="flex items-center gap-1.5 text-xs text-muted" title="Sub-10% owned, likely to start, with a real projection">
      <input type="checkbox" bind:checked={onlyDiff} class="accent-brand" /> differentials
    </label>
    <span class="text-xs text-muted2 ml-auto">{filtered.length} of {players.length}{filtered.length > rows.length ? ` (top ${rows.length})` : ''}</span>
  </div>

  <div class="card overflow-x-auto">
    <table class="data">
      <thead>
        <tr>
          <th class="!text-center" title="Add to comparison (up to 3)">⇄</th>
          <th onclick={() => sort('name')}>Player</th>
          <th class="!text-center">Fixtures</th>
          {#each cols as c}
            <th onclick={() => sort(c.key)}>{c.label}{sortKey === c.key ? (sortDir === -1 ? ' ▾' : ' ▴') : ''}</th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each rows as p (p.id)}
          <tr onclick={() => onpick(p.id)}>
            <td class="!text-center" onclick={(e) => e.stopPropagation()}>
              <input
                type="checkbox"
                class="accent-brand"
                checked={compareSet.has(p.id)}
                disabled={!compareSet.has(p.id) && compareIds.length >= 3}
                onchange={() => toggleCompare(p.id)}
                aria-label="Compare {p.name}"
              />
            </td>
            <td>
              <div class="flex items-center gap-2">
                <Crest code={p.team_code} short={p.team} size={22} />
                <div>
                  <div class="font-semibold flex items-center gap-1">
                    {p.name}
                    <span class="badge badge-{p.xmins_badge.kind}">{p.xmins_badge.label}</span>
                    {#if p.status && p.status !== 'a'}<span class="w-1.5 h-1.5 rounded-full bg-red inline-block"></span>{/if}
                  </div>
                  <div class="text-[10px] text-muted">{p.pos} · {p.team}</div>
                </div>
              </div>
            </td>
            <td><div class="flex justify-center"><FixtureStrip fixtures={p.fixtures} max={4} /></div></td>
            <td class="font-bold text-brand-light">{p.next_gw_xp.toFixed(1)}</td>
            <td class="text-accent-light">{p.xp_window.toFixed(0)}</td>
            <td>{p.price ? (p.next_gw_xp / p.price).toFixed(2) : '—'}</td>
            <td>
              <span class="tabular-nums">{p.price.toFixed(1)}</span>
              {#if p.price_pred.dir === 'up'}<span class="text-brand ml-0.5" title="Price rising">▲</span>
              {:else if p.price_pred.dir === 'down'}<span class="text-red ml-0.5" title="Price falling">▼</span>{/if}
            </td>
            <td class="text-muted">{p.owned_by}</td>
            <td class="text-muted">{p.form ? p.form.toFixed(1) : '—'}</td>
            <td class="text-muted">{p.ict.toFixed(0)}</td>
            <td class="text-muted">{p.xgi90.toFixed(2)}</td>
            <td class="{p.defcon && p.defcon.p_hit >= 0.5 ? 'text-brand-light font-semibold' : 'text-muted'}">
              {p.defcon90 ? p.defcon90.toFixed(1) : '—'}
              {#if p.defcon?.near_hit}<span class="text-yellow ml-0.5" title="Near-hit — one tick from a consistent +2">•</span>{/if}
            </td>
          </tr>
        {/each}
        {#if rows.length === 0}
          <tr><td colspan="12" class="!text-center text-muted py-6">No players match — try a different search or raise the price filter.</td></tr>
        {/if}
      </tbody>
    </table>
  </div>
</div>

{#if compareIds.length}
  <div class="fixed left-1/2 -translate-x-1/2 z-40 flex items-center gap-3 rounded-full border border-line bg-card2 shadow-lg px-4 py-2 text-sm"
    style="bottom: calc(var(--gaffer-bottomnav, 0px) + env(safe-area-inset-bottom) + 1rem);">
    <span class="text-muted">{compareIds.length} selected</span>
    <button onclick={() => (showCompare = true)} disabled={compareIds.length < 2} class="btn text-xs disabled:opacity-40">Compare</button>
    <button onclick={() => (compareIds = [])} class="text-xs text-muted hover:text-text">clear</button>
  </div>
{/if}

{#if showCompare && comparePlayers.length >= 2}
  <Compare
    players={comparePlayers}
    pool={players}
    onclose={() => (showCompare = false)}
    onremove={(id) => { compareIds = compareIds.filter((x) => x !== id); if (compareIds.length < 2) showCompare = false }}
    onpick={(id) => { showCompare = false; onpick(id) }}
  />
{/if}
