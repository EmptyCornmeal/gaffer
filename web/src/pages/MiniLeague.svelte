<script lang="ts">
  import { fpl, type LeagueStanding } from '../lib/fpl'
  import { getLeagueIds, getEntryId } from '../lib/config'
  import Icon from '../components/Icon.svelte'

  let { ongoSettings }: { ongoSettings: () => void } = $props()

  let leagueIds = $state(getLeagueIds())
  let active = $state(0)
  let phase = $state<'idle' | 'loading' | 'ok' | 'error' | 'nosetup'>('idle')
  let msg = $state('')
  let name = $state('')
  let rows = $state<LeagueStanding[]>([])
  let preseason = $state(false)
  const myEntry = getEntryId()

  $effect(() => {
    const ids = getLeagueIds()
    leagueIds = ids
    if (!ids.length || !fpl.configured()) {
      phase = 'nosetup'
      return
    }
    const id = ids[active]
    phase = 'loading'
    loadLeague(id)
      .then(({ leagueName, results, pre }) => {
        name = leagueName
        rows = results
        preseason = pre
        phase = 'ok'
      })
      .catch((e) => {
        phase = 'error'
        msg = String(e?.message ?? e)
      })
  })

  // The classic-standings endpoint pages 50 at a time; walk `has_next` so leagues
  // bigger than 50 aren't silently truncated. Capped so a 10k-manager league
  // can't hammer the proxy — we only need enough to place the user + rivals.
  const MAX_PAGES = 20
  async function loadLeague(id: number) {
    let leagueName = `League ${id}`
    const results: LeagueStanding[] = []
    const newcomers: any[] = []
    for (let page = 1; page <= MAX_PAGES; page++) {
      const data = await fpl.league(id, page)
      leagueName = data?.league?.name ?? leagueName
      const chunk = (data?.standings?.results ?? []) as LeagueStanding[]
      results.push(...chunk)
      newcomers.push(...(data?.new_entries?.results ?? []))
      const more = data?.standings?.has_next || data?.new_entries?.has_next
      if (!more || (chunk.length === 0 && (data?.new_entries?.results ?? []).length === 0)) break
    }
    if (results.length) return { leagueName, results, pre: false }
    // Pre-season: standings are empty until GW1 — members sit in `new_entries`.
    // Show who's joined (no scores yet) rather than a blank table.
    const pre: LeagueStanding[] = newcomers.map((e, i) => ({
      entry: e.entry,
      entry_name: e.entry_name,
      player_name: `${e.player_first_name ?? ''} ${e.player_last_name ?? ''}`.trim(),
      rank: i + 1,
      last_rank: i + 1,
      total: 0,
      event_total: 0,
    }))
    return { leagueName, results: pre, pre: true }
  }

  const maxTotal = $derived(rows.length ? Math.max(...rows.map((r) => r.total)) : 1)
</script>

{#if phase === 'nosetup'}
  <div class="card p-8 text-center rise max-w-lg mx-auto">
    <div class="inline-flex items-center justify-center w-12 h-12 rounded-full bg-brand/12 text-brand-light mb-3"><Icon name="trophy" size={22} /></div>
    <h2 class="font-bold text-lg">Track your mini-leagues</h2>
    <p class="text-sm text-muted mt-2">Add <b>Classic League IDs</b> in Settings to see standings and momentum.</p>
    <button class="btn mt-4" onclick={ongoSettings}>Open settings</button>
  </div>
{:else}
  <div class="flex flex-col gap-3 rise">
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
      <div class="card p-6 text-center"><h2 class="font-bold">Couldn't load league</h2><p class="text-xs text-muted2 mt-1">{msg}</p></div>
    {:else if phase === 'ok'}
      <div class="flex items-center gap-2 flex-wrap">
        <h2 class="font-bold text-lg">{name}</h2>
        <span class="text-xs text-muted">{rows.length} member{rows.length === 1 ? '' : 's'}</span>
      </div>
      {#if preseason}
        <div class="text-xs chip-info rounded-lg px-3 py-2 flex items-center gap-2">
          <Icon name="hourglass" size={13} /> The season hasn't kicked off — live standings appear after the GW1 deadline. Here's who's joined so far.
        </div>
      {/if}
      <div class="card overflow-x-auto">
        <table class="data">
          <thead><tr><th>#</th><th>Manager</th><th>Team</th><th class="!text-left">Total</th><th>GW</th></tr></thead>
          <tbody>
            {#each rows as r}
              <tr class="{r.entry === myEntry ? 'bg-brand/10' : ''}">
                <td class="!text-left">
                  {r.rank}
                  {#if !preseason && r.rank < r.last_rank}<span class="text-brand">▲</span>{:else if !preseason && r.rank > r.last_rank}<span class="text-red">▼</span>{/if}
                </td>
                <td class="!text-left">{r.player_name}{#if r.entry === myEntry}<span class="chip chip-good ml-1">you</span>{/if}</td>
                <td class="!text-left text-muted">{r.entry_name}</td>
                {#if preseason}
                  <td class="!text-left text-muted2">—</td>
                  <td class="text-muted2">—</td>
                {:else}
                  <td class="!text-left">
                    <div class="flex items-center gap-2">
                      <div class="h-2 rounded-full bg-brand/70" style="width: {(r.total / maxTotal) * 120}px"></div>
                      <span class="font-bold tabular-nums">{r.total}</span>
                    </div>
                  </td>
                  <td class="text-muted">{r.event_total}</td>
                {/if}
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if !preseason}
        <p class="text-xs text-muted2">Cumulative &amp; per-GW momentum charts coming next.</p>
      {/if}
    {/if}
  </div>
{/if}
