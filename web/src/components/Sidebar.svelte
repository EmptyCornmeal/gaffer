<script lang="ts">
  import type { Meta } from '../lib/types'
  import {
    getEntryId, setEntryId, getLeagueIds, setLeagueIds, parseId,
    apiBase, setApiBase,
  } from '../lib/config'
  import { GLOSSARY } from '../lib/glossary'
  import { NAV_TABS } from '../lib/nav'

  let {
    meta,
    playerCount,
    open,
    route,
    onnav,
    onsaved,
    onclose,
  }: {
    meta: Meta | null
    playerCount: number
    open: boolean
    route: string
    onnav: (r: string) => void
    onsaved: () => void
    onclose: () => void
  } = $props()

  let entry = $state(getEntryId()?.toString() ?? '')
  let leagues = $state(getLeagueIds().join(', '))
  let api = $state(apiBase() ?? '')
  let saved = $state(false)

  function save() {
    setEntryId(parseId(entry))
    setLeagueIds(
      leagues.split(',').map((s) => parseId(s.trim())).filter((n): n is number => !!n),
    )
    if (api.trim()) setApiBase(api.trim())
    saved = true
    setTimeout(() => (saved = false), 1500)
    onsaved()
  }

  const glossaryEntries = Object.entries(GLOSSARY)
</script>

<!-- backdrop for mobile drawer -->
{#if open}
  <button class="fixed inset-0 bg-black/50 z-30 lg:hidden" aria-label="close" onclick={onclose}></button>
{/if}

<aside
  class="fixed lg:sticky top-0 lg:top-[var(--gaffer-topbar)] left-0 z-40 lg:z-0
         h-svh lg:h-[calc(100svh-var(--gaffer-topbar))] overflow-y-auto
         bg-bg2 border-r border-line p-4 shrink-0 transition-transform
         {open ? 'translate-x-0' : '-translate-x-full'} lg:translate-x-0"
  style="width: var(--gaffer-sidebar);"
>
  <div class="lg:hidden flex items-center justify-between mb-3">
    <h3 class="font-bold">Menu</h3>
    <button class="text-muted" onclick={onclose} aria-label="close">✕</button>
  </div>

  <!-- Page navigation — mobile only (the topbar carries it on ≥lg). Without this
       the drawer had zero nav links, trapping phone users on Overview. -->
  <nav class="lg:hidden mb-4 grid gap-0.5">
    {#each NAV_TABS as t}
      <button
        onclick={() => onnav(t.key)}
        class="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-semibold text-left transition
          {route === t.key ? 'bg-accent/15 text-accent-light' : 'text-muted hover:text-text hover:bg-card2'}"
      >
        <span aria-hidden="true">{t.icon}</span>{t.label}
      </button>
    {/each}
  </nav>

  <h3 class="font-bold text-sm text-muted mb-2">Settings</h3>

  <div class="block text-xs text-muted mb-1">FPL Entry ID</div>
  <input
    bind:value={entry}
    placeholder="e.g. 1234567 or paste team URL"
    class="w-full rounded-lg bg-card border border-line px-3 py-2 text-sm mb-3 focus:outline-none focus:border-accent"
  />

  <div class="block text-xs text-muted mb-1">Classic League IDs</div>
  <input
    bind:value={leagues}
    placeholder="e.g. 12345, 67890"
    class="w-full rounded-lg bg-card border border-line px-3 py-2 text-sm mb-3 focus:outline-none focus:border-accent"
  />

  <div class="block text-xs text-muted mb-1">Proxy API base (for live data)</div>
  <input
    bind:value={api}
    placeholder="https://gaffer-proxy.…workers.dev/api"
    class="w-full rounded-lg bg-card border border-line px-3 py-2 text-xs mb-3 focus:outline-none focus:border-accent"
  />

  <button class="btn w-full" onclick={save}>{saved ? 'Saved ✓' : 'Save'}</button>

  <div class="mt-4 grid grid-cols-3 gap-2 text-center text-xs">
    <div class="card py-2"><div class="font-bold">20</div><div class="text-muted">teams</div></div>
    <div class="card py-2"><div class="font-bold">{playerCount}</div><div class="text-muted">players</div></div>
    <div class="card py-2"><div class="font-bold">{meta?.last_finished_gw || '—'}</div><div class="text-muted">last GW</div></div>
  </div>

  <details class="mt-4">
    <summary class="cursor-pointer text-sm font-semibold text-muted hover:text-text">Glossary / Help</summary>
    <div class="mt-2 space-y-2 text-xs">
      {#each glossaryEntries as [term, def]}
        <div><span class="chip chip-info">{term}</span> <span class="text-muted">{def}</span></div>
      {/each}
    </div>
  </details>

  <p class="mt-4 text-[11px] text-muted2 leading-relaxed">
    Live-aware. Projections update each refresh; your team &amp; league data are
    fetched live once IDs + a proxy are set.
  </p>
</aside>
