<script lang="ts">
  import { fpl, type LeagueStanding } from '../lib/fpl'
  import { getLeagueIds, getEntryId } from '../lib/config'

  let { ongoSettings }: { ongoSettings: () => void } = $props()

  let leagueIds = $state(getLeagueIds())
  let active = $state(0)
  let phase = $state<'idle' | 'loading' | 'ok' | 'error' | 'nosetup'>('idle')
  let msg = $state('')
  let name = $state('')
  let rows = $state<LeagueStanding[]>([])
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
    fpl
      .league(id)
      .then((data) => {
        name = data?.league?.name ?? `League ${id}`
        rows = (data?.standings?.results ?? []) as LeagueStanding[]
        phase = 'ok'
      })
      .catch((e) => {
        phase = 'error'
        msg = String(e?.message ?? e)
      })
  })

  const maxTotal = $derived(rows.length ? Math.max(...rows.map((r) => r.total)) : 1)
</script>

{#if phase === 'nosetup'}
  <div class="card p-8 text-center rise max-w-lg mx-auto">
    <div class="text-4xl mb-2">🏆</div>
    <h2 class="font-bold text-lg">Track your mini-leagues</h2>
    <p class="text-sm text-muted mt-2">Add <b>Classic League IDs</b> and a <b>proxy</b> in Settings to see standings and momentum.</p>
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
      <h2 class="font-bold text-lg">{name}</h2>
      <div class="card overflow-x-auto">
        <table class="data">
          <thead><tr><th>#</th><th>Manager</th><th>Team</th><th class="!text-left">Total</th><th>GW</th></tr></thead>
          <tbody>
            {#each rows as r}
              <tr class="{r.entry === myEntry ? 'bg-brand/10' : ''}">
                <td class="!text-left">
                  {r.rank}
                  {#if r.rank < r.last_rank}<span class="text-brand">▲</span>{:else if r.rank > r.last_rank}<span class="text-red">▼</span>{/if}
                </td>
                <td class="!text-left">{r.player_name}{#if r.entry === myEntry}<span class="chip chip-good ml-1">you</span>{/if}</td>
                <td class="!text-left text-muted">{r.entry_name}</td>
                <td class="!text-left">
                  <div class="flex items-center gap-2">
                    <div class="h-2 rounded-full bg-brand/70" style="width: {(r.total / maxTotal) * 120}px"></div>
                    <span class="font-bold tabular-nums">{r.total}</span>
                  </div>
                </td>
                <td class="text-muted">{r.event_total}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="text-xs text-muted2">Cumulative &amp; per-GW momentum charts coming next.</p>
    {/if}
  </div>
{/if}
