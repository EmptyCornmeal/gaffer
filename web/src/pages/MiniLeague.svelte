<script lang="ts">
  import { displayName, fpl, ProxyTimeoutError, type LeagueStanding } from '../lib/fpl'
  import { getLeagueIds, getEntryId } from '../lib/config'
  import Icon from '../components/Icon.svelte'
  import LineChart from '../components/LineChart.svelte'
  import LeagueStrategy from '../components/LeagueStrategy.svelte'
  import LeagueMeta from '../components/LeagueMeta.svelte'
  import type { Bundle } from '../lib/data'

  let { ongoSettings, bundle, onnav, onpick, now = Date.now() }: {
    ongoSettings: () => void
    bundle: Bundle
    onnav: (r: string) => void
    onpick: (id: number) => void
    now?: number
  } = $props()

  // Three views of one question: what is everyone else doing. Standings is
  // your own league; Strategy scores your decisions against it; Meta is the
  // same question asked of the whole game.
  let tab = $state<'standings' | 'strategy' | 'meta'>('standings')
  const TABS = [
    { key: 'standings', label: 'Standings' },
    { key: 'strategy', label: 'Strategy' },
    { key: 'meta', label: 'Meta' },
  ] as const

  let leagueIds = $state(getLeagueIds())
  let active = $state(0)
  let phase = $state<'idle' | 'loading' | 'ok' | 'error' | 'nosetup'>('idle')
  // A timeout and a wrong league id both land in 'error', but only one of them is
  // the user's settings and only one of them is worth retrying in place.
  let failure = $state<'timeout' | 'other'>('other')
  let retry = $state(0)
  let name = $state('')
  let rows = $state<LeagueStanding[]>([])
  let preseason = $state(false)
  let histories = $state<Map<number, GwRow[]>>(new Map())
  let chartMode = $state<'cumulative' | 'gw' | 'rank'>('cumulative')
  // Clicking a manager in the table isolates them in the chart, and clicking
  // again clears it — one piece of state drives both.
  let focusEntry = $state<number | null>(null)
  let sortKey = $state<keyof Stat>('total')
  let sortDir = $state<1 | -1>(-1)
  const myEntry = getEntryId()

  type GwRow = {
    event: number
    points: number
    total_points: number
    overall_rank: number
    event_transfers_cost: number
    points_on_bench: number
  }

  // "you" = emerald; rivals cycle a categorical palette (emerald reserved).
  const YOU = '#34d399'
  const PALETTE = ['#3987e5', '#d95926', '#c98500', '#d55181', '#9085e9', '#e66767', '#199e70', '#60a5fa', '#f59e0b', '#a78bfa']

  $effect(() => {
    void retry // read so the effect re-runs when the user asks for another go
    const ids = getLeagueIds()
    leagueIds = ids
    if (!ids.length || !fpl.configured()) {
      phase = 'nosetup'
      return
    }
    const id = ids[active]
    phase = 'loading'
    histories = new Map()
    loadLeague(id)
      .then(async ({ leagueName, results, pre }) => {
        name = leagueName
        rows = results
        preseason = pre
        if (!pre && results.length) {
          // fetch each member's season history (cap so a huge league can't hammer)
          const top = results.slice(0, 30)
          const pairs = await Promise.all(
            top.map((r) =>
              fpl
                .entryHistory(r.entry)
                .then((h): [number, GwRow[]] => [r.entry, (h?.current ?? []) as GwRow[]])
                .catch((): [number, GwRow[]] => [r.entry, []]),
            ),
          )
          histories = new Map(pairs)
        }
        phase = 'ok'
      })
      .catch((e) => {
        failure = e instanceof ProxyTimeoutError ? 'timeout' : 'other'
        phase = 'error'
      })
  })

  const MAX_PAGES = 20
  async function loadLeague(id: number) {
    let leagueName = `League ${id}`
    const results: LeagueStanding[] = []
    const newcomers: any[] = []
    for (let page = 1; page <= MAX_PAGES; page++) {
      const data = await fpl.league(id, page)
      leagueName = data?.league?.name ?? leagueName
      const chunk = ((data?.standings?.results ?? []) as LeagueStanding[]).map((r) => ({
        ...r,
        entry_name: displayName(r.entry_name),
        player_name: displayName(r.player_name),
      }))
      results.push(...chunk)
      newcomers.push(...(data?.new_entries?.results ?? []))
      const more = data?.standings?.has_next || data?.new_entries?.has_next
      if (!more || (chunk.length === 0 && (data?.new_entries?.results ?? []).length === 0)) break
    }
    if (results.length) return { leagueName, results, pre: false }
    const pre: LeagueStanding[] = newcomers.map((e, i) => ({
      entry: e.entry,
      entry_name: displayName(e.entry_name),
      player_name: displayName(`${e.player_first_name ?? ''} ${e.player_last_name ?? ''}`),
      rank: i + 1,
      last_rank: i + 1,
      total: 0,
      event_total: 0,
    }))
    return { leagueName, results: pre, pre: true }
  }

  // ---- derived analytics ------------------------------------------------
  const gwCount = $derived(Math.max(0, ...[...histories.values()].map((h) => h.length)))
  const gwLabels = $derived(Array.from({ length: gwCount }, (_, i) => `GW${i + 1}`))
  const hasHistory = $derived(gwCount > 0)

  function seriesFor(pick: (g: GwRow) => number, invert = false) {
    // top 10 by total + always include you, coloured consistently
    const ranked = [...rows].filter((r) => (histories.get(r.entry)?.length ?? 0) > 0)
    const shown = ranked.slice(0, 10)
    if (myEntry && !shown.some((r) => r.entry === myEntry)) {
      const me = ranked.find((r) => r.entry === myEntry)
      if (me) shown.push(me)
    }
    return shown.map((r, i) => {
      const h = histories.get(r.entry) ?? []
      const you = r.entry === myEntry
      return {
        key: r.entry,
        name: r.player_name,
        color: you ? YOU : PALETTE[i % PALETTE.length],
        you,
        values: h.map((g) => (invert ? -pick(g) : pick(g))),
      }
    })
  }
  const cumSeries = $derived(seriesFor((g) => g.total_points))
  const gwSeries = $derived(seriesFor((g) => g.points))
  // overall rank (invert so "up" on the chart = better rank)
  const rankSeries = $derived(seriesFor((g) => g.overall_rank, true))

  const chartSeries = $derived(
    chartMode === 'cumulative' ? cumSeries : chartMode === 'gw' ? gwSeries : rankSeries,
  )
  // The rank series is stored negated so that "up" means "better". Undo that for
  // the hover read-out, or the tooltip reports a negative overall rank.
  // Overall rank runs to seven digits, which fits neither the axis gutter nor a
  // tooltip row, so it is abbreviated rather than truncated.
  function compactRank(v: number): string {
    const r = -v
    if (!Number.isFinite(r)) return '—'
    if (r >= 1_000_000) return (r / 1_000_000).toFixed(r >= 10_000_000 ? 0 : 1) + 'm'
    if (r >= 1_000) return Math.round(r / 1_000) + 'k'
    return String(Math.round(r))
  }
  const chartFormat = $derived(
    chartMode === 'rank' ? compactRank : (v: number) => String(Math.round(v)),
  )

  // per-manager season stats
  type Stat = { entry: number; name: string; team: string; total: number; gw: number; best: number; form: number; hits: number; bench: number; wins: number }
  const stats = $derived.by<Stat[]>(() => {
    if (!hasHistory) return []
    // GW winners: highest points each GW
    const wins = new Map<number, number>()
    for (let gw = 0; gw < gwCount; gw++) {
      let bestEntry = -1
      let bestPts = -1
      for (const r of rows) {
        const g = histories.get(r.entry)?.[gw]
        if (g && g.points > bestPts) {
          bestPts = g.points
          bestEntry = r.entry
        }
      }
      if (bestEntry >= 0) wins.set(bestEntry, (wins.get(bestEntry) ?? 0) + 1)
    }
    return rows
      .map((r) => {
        const h = histories.get(r.entry) ?? []
        const pts = h.map((g) => g.points)
        return {
          entry: r.entry,
          name: r.player_name,
          team: r.entry_name,
          total: h.length ? h[h.length - 1].total_points : r.total,
          gw: h.length ? h[h.length - 1].points : 0,
          best: pts.length ? Math.max(...pts) : 0,
          form: pts.slice(-3).reduce((s, p) => s + p, 0),
          hits: h.reduce((s, g) => s + (g.event_transfers_cost ?? 0), 0),
          bench: h.reduce((s, g) => s + (g.points_on_bench ?? 0), 0),
          wins: wins.get(r.entry) ?? 0,
        }
      })
      .sort((a, b) => b.total - a.total)
  })

  const sortedStats = $derived.by<Stat[]>(() => {
    const out = [...stats]
    out.sort((a, b) => {
      const av = a[sortKey]
      const bv = b[sortKey]
      const d =
        typeof av === 'number' && typeof bv === 'number'
          ? av - bv
          : String(av).localeCompare(String(bv))
      return d * sortDir
    })
    return out
  })
  function sortBy(k: keyof Stat) {
    if (sortKey === k) sortDir = sortDir === 1 ? -1 : 1
    else {
      sortKey = k
      // Names read best A-Z; every number reads best biggest-first.
      sortDir = k === 'name' || k === 'team' ? 1 : -1
    }
  }
  const COLS: { key: keyof Stat; label: string; left?: boolean }[] = [
    { key: 'name', label: 'Manager', left: true },
    { key: 'team', label: 'Team', left: true },
    { key: 'total', label: 'Total', left: true },
    { key: 'gw', label: 'GW' },
    { key: 'best', label: 'Best' },
    { key: 'form', label: 'Form' },
    { key: 'hits', label: 'Hits' },
    { key: 'bench', label: 'Bench' },
  ]
  // The bar is a share of the leader, so it must track the highest total — not
  // whatever happens to be sorted into the first row.
  const topTotal = $derived(stats.length ? Math.max(...stats.map((s) => s.total)) : 1)

  const maxTotal = $derived(rows.length ? Math.max(...rows.map((r) => r.total)) : 1)
  const CHART_TABS: { key: 'cumulative' | 'gw' | 'rank'; label: string }[] = [
    { key: 'cumulative', label: 'Points race' },
    { key: 'gw', label: 'Per GW' },
    { key: 'rank', label: 'Overall rank' },
  ]
</script>

<div class="max-w-5xl mx-auto w-full">
  <div class="flex items-center gap-0.5 rounded-lg border border-line bg-bg2 p-0.5 w-fit mb-3">
    {#each TABS as t}
      <button
        onclick={() => (tab = t.key)}
        class="px-3 py-1 rounded-md text-xs font-bold transition {tab === t.key ? 'bg-brand text-[#05210f]' : 'text-muted hover:text-text'}"
      >{t.label}</button>
    {/each}
  </div>
</div>

{#if tab === 'strategy'}
  <LeagueStrategy {bundle} {onnav} {now} />
{:else if tab === 'meta'}
  <LeagueMeta {bundle} {onpick} />
{:else if phase === 'nosetup'}
  <div class="card p-8 text-center rise max-w-lg mx-auto">
    <div class="inline-flex items-center justify-center w-12 h-12 rounded-full bg-brand/12 text-brand-light mb-3"><Icon name="trophy" size={22} /></div>
    <h2 class="font-bold text-lg">Track your mini-leagues</h2>
    <p class="text-sm text-muted mt-2">Add <b>Classic League IDs</b> in Settings to see standings and momentum.</p>
    <button class="btn mt-4" onclick={ongoSettings}>Open settings</button>
  </div>
{:else}
  <div class="flex flex-col gap-3 rise max-w-5xl mx-auto w-full">
    {#if leagueIds.length > 1}
      <div class="flex gap-1">
        {#each leagueIds as id, i}
          <button onclick={() => (active = i)} class="px-3 py-1 rounded-lg text-sm font-semibold border {active === i ? 'bg-accent/15 text-accent-light border-accent/40' : 'bg-card border-line text-muted'}">League {i + 1}</button>
        {/each}
      </div>
    {/if}

    {#if phase === 'loading'}
      <div class="flex justify-center py-24 text-muted"><div class="w-8 h-8 rounded-full border-2 border-line border-t-brand animate-spin"></div></div>
    {:else if phase === 'error'}
      <div class="card p-6 text-center rise max-w-lg mx-auto">
        {#if failure === 'timeout'}
          <h2 class="font-bold">That took too long</h2>
          <p class="text-sm text-muted mt-2">The FPL proxy didn't answer in time. That's a slow connection or a busy matchday, not your settings.</p>
          <button class="btn mt-4" onclick={() => retry++}>Try again</button>
        {:else}
          <h2 class="font-bold">Couldn't load that league</h2>
          <p class="text-sm text-muted mt-2">Double-check your <b>Classic League ID</b> in Settings — it's the number in your league's URL on the FPL site.</p>
        {/if}
      </div>
    {:else if phase === 'ok'}
      <div class="flex items-center gap-2 flex-wrap">
        <h2 class="font-bold text-lg">{name}</h2>
        <span class="text-xs text-muted">{rows.length} member{rows.length === 1 ? '' : 's'}{hasHistory ? ` · ${gwCount} GW${gwCount === 1 ? '' : 's'}` : ''}</span>
      </div>

      {#if preseason || !hasHistory}
        <div class="text-xs chip-info rounded-lg px-3 py-2 flex items-center gap-2">
          <Icon name="hourglass" size={13} /> Charts &amp; stats light up once GW1 is played — here's who's in the league so far.
        </div>
      {/if}

      {#if hasHistory}
        <!-- charts -->
        <div class="card p-3">
          <div class="flex items-center justify-between mb-2 gap-2 flex-wrap">
            <h3 class="font-bold text-sm">League trends</h3>
            <div class="inline-flex items-center gap-0.5 rounded-lg border border-line bg-bg2 p-0.5">
              {#each CHART_TABS as t}
                <button onclick={() => (chartMode = t.key)} class="px-2.5 py-1 rounded-md text-xs font-bold transition {chartMode === t.key ? 'bg-brand text-[#05210f]' : 'text-muted hover:text-text'}">{t.label}</button>
              {/each}
            </div>
          </div>
          {#if focusEntry != null}
            <div class="mb-2 flex items-center gap-2 text-mini">
              <span class="chip chip-good">Isolated: {stats.find((s) => s.entry === focusEntry)?.name ?? '—'}</span>
              <button class="text-accent-light hover:underline" onclick={() => (focusEntry = null)}>show everyone</button>
            </div>
          {/if}
          <LineChart series={chartSeries} labels={gwLabels} height={260} yLabel={chartMode} format={chartFormat} focusKey={focusEntry} />
          <p class="text-mini text-muted2 mt-1">Hover or arrow-key the chart for a gameweek read-out · click a name below to mute it · click a row in the table to isolate a manager.</p>
          {#if chartMode === 'rank'}<p class="text-mini text-muted2 mt-1">Higher = better overall rank (millions, inverted).</p>{/if}
        </div>

        <!-- GW winners strip -->
        {#if stats.some((s) => s.wins > 0)}
          <div class="card p-3">
            <h3 class="font-bold text-sm mb-2">Gameweek wins</h3>
            <div class="flex flex-wrap gap-2">
              {#each [...stats].filter((s) => s.wins > 0).sort((a, b) => b.wins - a.wins) as s}
                <span class="chip {s.entry === myEntry ? 'chip-good' : 'chip-info'}">{s.name} · {s.wins}</span>
              {/each}
            </div>
          </div>
        {/if}
      {/if}

      <!-- standings / stats table -->
      <div class="card overflow-x-auto">
        <table class="data">
          <thead>
            {#if hasHistory}
              <tr>
                <th>#</th>
                {#each COLS as c}
                  <th class={c.left ? '!text-left' : ''}>
                    <button type="button" onclick={() => sortBy(c.key)} class="inline-flex items-center gap-1 transition hover:text-text {sortKey === c.key ? 'text-text' : ''}" title="Sort by {c.label}">
                      {c.label}{#if sortKey === c.key}<span class="text-[9px]">{sortDir === 1 ? '▲' : '▼'}</span>{/if}
                    </button>
                  </th>
                {/each}
              </tr>
            {:else}
              <tr><th>#</th><th>Manager</th><th>Team</th><th class="!text-left">Total</th><th>GW</th></tr>
            {/if}
          </thead>
          <tbody>
            {#if hasHistory}
              {#each sortedStats as s, i}
                <tr
                  onclick={() => (focusEntry = focusEntry === s.entry ? null : s.entry)}
                  title="Click to isolate {s.name} in the chart"
                  class="cursor-pointer transition hover:bg-line/20 {s.entry === myEntry ? 'bg-brand/10' : ''} {focusEntry === s.entry ? 'ring-1 ring-inset ring-brand/60' : ''}">
                  <td class="!text-left">{i + 1}</td>
                  <td class="!text-left">{s.name}{#if s.entry === myEntry}<span class="chip chip-good ml-1">you</span>{/if}</td>
                  <td class="!text-left text-muted">{s.team}</td>
                  <td class="!text-left">
                    <div class="flex items-center gap-2">
                      <div class="h-2 rounded-full bg-brand/70" style="width: {(s.total / (topTotal || 1)) * 110}px"></div>
                      <span class="font-bold tabular-nums">{s.total}</span>
                    </div>
                  </td>
                  <td class="text-muted">{s.gw}</td>
                  <td class="text-brand-light font-semibold">{s.best}</td>
                  <td class="text-muted">{s.form}</td>
                  <td class="{s.hits ? 'text-red' : 'text-muted2'}">{s.hits ? `-${s.hits}` : '0'}</td>
                  <td class="text-muted2">{s.bench}</td>
                </tr>
              {/each}
            {:else}
              {#each rows as r}
                <tr class="{r.entry === myEntry ? 'bg-brand/10' : ''}">
                  <td class="!text-left">{r.rank}</td>
                  <td class="!text-left">{r.player_name}{#if r.entry === myEntry}<span class="chip chip-good ml-1">you</span>{/if}</td>
                  <td class="!text-left text-muted">{r.entry_name}</td>
                  <td class="!text-left text-muted2">—</td>
                  <td class="text-muted2">—</td>
                </tr>
              {/each}
            {/if}
          </tbody>
        </table>
      </div>
    {/if}
  </div>
{/if}
