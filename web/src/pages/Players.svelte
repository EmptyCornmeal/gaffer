<script lang="ts">
  import type { Bundle } from '../lib/data'
  import type { Player, Pos } from '../lib/types'
  import FixtureStrip from '../components/FixtureStrip.svelte'
  import Crest from '../components/Crest.svelte'
  import Compare from '../components/Compare.svelte'
  import { matches } from '../lib/search'
  import { badgeCaption } from '../lib/evidence'

  let { players, onpick, bundle }: {
    players: Player[]
    onpick: (id: number) => void
    bundle: Bundle
  } = $props()

  // News is player data. On its own page it had no search, no filter and no
  // way to narrow to your squad; here it inherits all three.
  let tab = $state<'players' | 'news'>('players')
  const TABS = [
    { key: 'players', label: 'Players' },
    { key: 'news', label: 'News' },
  ] as const

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
  // `at` is the width each stat has to earn. All nine at once is a good desktop
  // table and an unusable phone one: the fixture strip alone is ~150px of a
  // 366px viewport, which pushed xP — the number this product exists to produce
  // — off-screen right with no scroll affordance. Only xP and price survive at
  // 390px. Nothing dropped is lost: the PlayerDetail modal carries the rest, and
  // the small-screen control below keeps every column's sort reachable.
  const cols: { key: keyof Player | 'value'; label: string; at: string }[] = [
    { key: 'next_gw_xp', label: 'xP', at: '' },
    { key: 'xp_window', label: '6GW', at: 'hidden sm:table-cell' },
    { key: 'value', label: 'Val', at: 'hidden md:table-cell' },
    { key: 'price', label: '£', at: '' },
    { key: 'owned_by', label: 'Own%', at: 'hidden md:table-cell' },
    { key: 'form', label: 'Form', at: 'hidden lg:table-cell' },
    { key: 'ict', label: 'ICT', at: 'hidden lg:table-cell' },
    { key: 'xgi90', label: 'xGI90', at: 'hidden lg:table-cell' },
    { key: 'defcon90', label: 'DC90', at: 'hidden lg:table-cell' },
  ]
  // Pre-season every player's form is 0, so the column is 587 em-dashes taking
  // width on every screen. Keyed off the data rather than the calendar, so it
  // returns by itself the first time anyone has actually played.
  const hasForm = $derived(players.some((p) => p.form > 0))
  const visibleCols = $derived(cols.filter((c) => c.key !== 'form' || hasForm))
  // FPL's availability code. The bare red dot this replaces put the entire
  // signal in colour; `news` is the club's own sentence ("Knock - 75% chance of
  // playing"), so the badge never has to invent a reason it doesn't have.
  const FLAGS: Record<string, { short: string; label: string; kind: string }> = {
    d: { short: 'DOUBT', label: 'Doubtful', kind: 'warn' },
    i: { short: 'INJ', label: 'Injured', kind: 'bad' },
    s: { short: 'SUSP', label: 'Suspended', kind: 'bad' },
    u: { short: 'OUT', label: 'Unavailable', kind: 'bad' },
    n: { short: 'N/A', label: 'Not in the squad', kind: 'bad' },
  }
  function flag(status: string | null) {
    if (!status || status === 'a') return null
    return FLAGS[status] ?? { short: 'FLAG', label: 'Flagged', kind: 'bad' }
  }
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

<div class="flex items-center gap-0.5 rounded-lg border border-line bg-bg2 p-0.5 w-fit mb-3">
  {#each TABS as t}
    <button
      onclick={() => (tab = t.key)}
      class="px-3 py-1 rounded-md text-xs font-bold transition {tab === t.key ? 'bg-brand text-[#05210f]' : 'text-muted hover:text-text'}"
    >{t.label}</button>
  {/each}
</div>

{#if tab === 'news'}
  {#await import('../components/NewsView.svelte')}
    <div class="flex justify-center py-16 text-muted" role="status" aria-live="polite">
      <div class="w-6 h-6 rounded-full border-2 border-line border-t-brand animate-spin motion-reduce:animate-none"></div>
    </div>
  {:then M}
    <M.default {bundle} />
  {:catch}
    <p class="text-sm text-muted text-center py-16">That section failed to load. Reload the page.</p>
  {/await}
{:else}


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
    <!-- Below `lg` most stat columns are hidden and their header sort handles go
         with them, which would strand a phone on whatever xP happened to give
         it. Same `sort()`, same keys, just reachable without the columns. -->
    <div class="lg:hidden flex items-center gap-1.5">
      <label class="flex items-center gap-1.5 text-xs text-muted">
        Sort
        <select
          value={sortKey}
          onchange={(e) => sort(e.currentTarget.value as keyof Player | 'value')}
          class="rounded-lg bg-card border border-line px-2 min-h-11 text-xs focus:outline-none focus:border-accent"
        >
          <option value="name">Player</option>
          {#each visibleCols as c}<option value={c.key}>{c.label}</option>{/each}
        </select>
      </label>
      <button
        onclick={() => (sortDir = (sortDir * -1) as 1 | -1)}
        aria-label="Sort {sortDir === -1 ? 'ascending' : 'descending'}"
        class="rounded-lg bg-card border border-line px-2.5 text-xs text-muted"
      >{sortDir === -1 ? '▾' : '▴'}</button>
    </div>
    <!-- Two different ratios: matches against the whole pool, then rendered rows
         against the matches. Run together as "587 of 587 (top 200)" they read as
         one contradictory claim, so each gets its own clause — and the second only
         when the table is actually cut short. -->
    <span class="text-xs text-muted2 ml-auto">{filtered.length} of {players.length} players{filtered.length > rows.length ? ` · showing the first ${rows.length}` : ''}</span>
  </div>

  <div class="card overflow-x-auto">
    <table class="data">
      <thead>
        <tr>
          <th class="!text-center" title="Add to comparison (up to 3)">⇄</th>
          <!-- A sort handle has to be a real button: a bare `<th onclick>` is
               neither focusable nor announced as a control, and `aria-sort`
               belongs on the cell rather than on it. The cell's padding moves to
               the button so the 44px coarse-pointer floor doesn't double the
               height of a sticky header. -->
          <th class="!p-0" aria-sort={sortKey === 'name' ? (sortDir === -1 ? 'descending' : 'ascending') : 'none'}>
            <button class="w-full px-2.5 py-2 text-left" onclick={() => sort('name')}>Player{sortKey === 'name' ? (sortDir === -1 ? ' ▾' : ' ▴') : ''}</button>
          </th>
          <th class="!text-center hidden sm:table-cell">Fixtures</th>
          {#each visibleCols as c}
            <th class="!p-0 {c.at}" aria-sort={sortKey === c.key ? (sortDir === -1 ? 'descending' : 'ascending') : 'none'}>
              <button class="w-full px-2.5 py-2 text-right" onclick={() => sort(c.key)}>{c.label}{sortKey === c.key ? (sortDir === -1 ? ' ▾' : ' ▴') : ''}</button>
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each rows as p (p.id)}
          {@const f = flag(p.status)}
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
              <!-- The row keeps its click, but `<tr onclick>` is unreachable by
                   keyboard and announced as nothing. The name becomes a real
                   button firing the same `onpick` prop the row does. -->
              <button class="flex items-center gap-2 w-full text-left" onclick={(e) => { e.stopPropagation(); onpick(p.id) }}>
                <Crest code={p.team_code} short={p.team} size={22} />
                <span class="min-w-0">
                  <span class="font-semibold flex items-center gap-1">
                    {p.name}
                    <!-- 4.7: the badge carries its measured error. NAILED is a
                         one-word confidence statement and over-claims by five
                         points; the caption is how a reader finds that out
                         without leaving the row. -->
                    <span class="badge badge-{p.xmins_badge.kind}"
                          title={badgeCaption(bundle.meta, p.xmins_badge.label) ?? undefined}
                    >{p.xmins_badge.label}</span>
                    {#if f}<span class="badge badge-{f.kind}" title={p.news || f.label}>{f.short}<span class="sr-only"> — {f.label}{p.news ? `: ${p.news}` : ''}</span></span>{/if}
                  </span>
                  <span class="block text-micro text-muted">{p.pos} · {p.team}</span>
                </span>
              </button>
            </td>
            <td class="hidden sm:table-cell"><div class="flex justify-center"><FixtureStrip fixtures={p.fixtures} max={4} /></div></td>
            <td class="font-bold text-brand-light">{p.next_gw_xp.toFixed(1)}</td>
            <td class="text-accent-light hidden sm:table-cell">{p.xp_window.toFixed(0)}</td>
            <td class="hidden md:table-cell">{p.price ? (p.next_gw_xp / p.price).toFixed(2) : '—'}</td>
            <td>
              <span class="tabular-nums">{p.price.toFixed(1)}</span>
              {#if p.price_pred.dir === 'up'}<span class="text-brand ml-0.5" title="Price rising">▲</span>
              {:else if p.price_pred.dir === 'down'}<span class="text-red ml-0.5" title="Price falling">▼</span>{/if}
            </td>
            <td class="text-muted hidden md:table-cell">{p.owned_by}</td>
            {#if hasForm}<td class="text-muted hidden lg:table-cell">{p.form ? p.form.toFixed(1) : '—'}</td>{/if}
            <td class="text-muted hidden lg:table-cell">{p.ict.toFixed(0)}</td>
            <td class="text-muted hidden lg:table-cell">{p.xgi90.toFixed(2)}</td>
            <td class="hidden lg:table-cell {p.defcon && p.defcon.p_hit >= 0.5 ? 'text-brand-light font-semibold' : 'text-muted'}">
              {p.defcon90 ? p.defcon90.toFixed(1) : '—'}
              {#if p.defcon?.near_hit}<span class="text-yellow ml-0.5" title="Near-hit — one tick from a consistent +2">•</span>{/if}
            </td>
          </tr>
        {/each}
        {#if rows.length === 0}
          <tr><td colspan={visibleCols.length + 3} class="!text-center text-muted py-6">No players match — try a different search or raise the price filter.</td></tr>
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
{/if}
