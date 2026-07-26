<script lang="ts">
  import type { Player, Pos } from '../lib/types'
  import FixtureStrip from '../components/FixtureStrip.svelte'
  import Crest from '../components/Crest.svelte'
  import { matches } from '../lib/search'

  let { players, onpick }: { players: Player[]; onpick: (id: number) => void } = $props()

  let query = $state('')
  let pos = $state<'ALL' | Pos>('ALL')
  // Cap tracks the actual most-expensive player so premiums (Haaland £15.5) are
  // never clamped out of the list — the old fixed max=15 hid him entirely.
  const priceCap = Math.max(15, ...players.map((p) => p.price))
  let maxPrice = $state(priceCap)
  let onlyStarters = $state(false)
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
      .filter((p) => p.price <= maxPrice)
      .filter((p) => !onlyStarters || p.p_start >= 0.6)
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
      max £{maxPrice.toFixed(1)}
      <input type="range" min="4" max={priceCap} step="0.5" bind:value={maxPrice} class="accent-brand" />
    </label>
    <label class="flex items-center gap-1.5 text-xs text-muted">
      <input type="checkbox" bind:checked={onlyStarters} class="accent-brand" /> likely starters
    </label>
    <span class="text-xs text-muted2 ml-auto">{filtered.length} of {players.length}{filtered.length > rows.length ? ` (top ${rows.length})` : ''}</span>
  </div>

  <div class="card overflow-x-auto">
    <table class="data">
      <thead>
        <tr>
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
          <tr><td colspan="11" class="!text-center text-muted py-6">No players match — try a different search or raise the price filter.</td></tr>
        {/if}
      </tbody>
    </table>
  </div>
</div>
