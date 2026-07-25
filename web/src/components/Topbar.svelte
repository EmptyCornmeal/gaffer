<script lang="ts">
  import type { Meta, Player } from '../lib/types'
  import { countdown } from '../lib/data'
  import { getTheme, setTheme } from '../lib/config'
  import { matches } from '../lib/search'

  let {
    meta,
    players,
    route,
    now,
    onnav,
    onpick,
    onmenu,
  }: {
    meta: Meta | null
    players: Player[]
    route: string
    now: number
    onnav: (r: string) => void
    onpick: (id: number) => void
    onmenu: () => void
  } = $props()

  const tabs = [
    { key: 'overview', label: 'Overview' },
    { key: 'my-team', label: 'My Team' },
    { key: 'planner', label: 'Planner' },
    { key: 'players', label: 'Players' },
    { key: 'fixtures', label: 'Fixtures' },
    { key: 'league', label: 'League' },
    { key: 'news', label: 'News' },
    { key: 'help', label: 'Help' },
  ]

  let theme = $state(getTheme())
  function toggleTheme() {
    theme = theme === 'dark' ? 'light' : 'dark'
    setTheme(theme)
  }

  let q = $state('')
  const results = $derived(
    q.length >= 2 ? players.filter((p) => matches(p, q)).slice(0, 6) : [],
  )
  function choose(p: Player) {
    onpick(p.id)
    q = ''
  }
  const timeLeft = $derived(meta ? countdown(meta.deadline, now) : '')
</script>

<header
  class="sticky top-0 z-40 flex items-center gap-3 px-3 border-b border-line bg-bg2/95 backdrop-blur"
  style="height: var(--gaffer-topbar);"
>
  <button class="lg:hidden text-muted text-xl px-1" onclick={onmenu} aria-label="menu">☰</button>

  <div class="flex items-center gap-2 shrink-0">
    <span class="text-lg font-black tracking-tight"><span class="text-brand">G</span>affer</span>
    <span class="hidden sm:inline text-[10px] text-muted border border-line rounded px-1 py-0.5"
      >{meta?.season ?? '2026-27'}</span
    >
  </div>

  {#if meta}
    <div class="hidden md:flex flex-col items-center px-3 border-l border-r border-line leading-tight">
      <span class="text-[9px] uppercase text-muted">{meta.gw_name || 'GW' + meta.current_gw} deadline</span>
      <span class="text-sm font-bold text-accent-light tabular-nums">{timeLeft || '—'}</span>
    </div>
  {/if}

  <nav class="hidden lg:flex items-center gap-1">
    {#each tabs as t}
      <button
        onclick={() => onnav(t.key)}
        class="px-3 py-1.5 rounded-lg text-sm font-semibold transition
          {route === t.key ? 'bg-accent/15 text-accent-light' : 'text-muted hover:text-text'}"
      >{t.label}</button>
    {/each}
  </nav>

  <div class="relative ml-auto w-40 sm:w-56">
    <input
      bind:value={q}
      placeholder="Search players…"
      class="w-full rounded-lg bg-card border border-line px-3 py-1.5 text-sm placeholder:text-muted2 focus:outline-none focus:border-accent"
    />
    {#if results.length}
      <div class="absolute right-0 mt-1 w-64 card shadow-xl z-50 overflow-hidden">
        {#each results as p}
          <button
            onclick={() => choose(p)}
            class="w-full flex items-center justify-between px-3 py-2 hover:bg-card2 text-left text-sm"
          >
            <span><b>{p.name}</b> <span class="text-muted">{p.pos} · {p.team}</span></span>
            <span class="text-brand-light font-bold tabular-nums">{p.next_gw_xp.toFixed(1)}</span>
          </button>
        {/each}
      </div>
    {/if}
  </div>

  <button
    onclick={toggleTheme}
    class="shrink-0 text-muted hover:text-text text-lg px-1"
    title="Toggle theme"
    aria-label="toggle theme">{theme === 'dark' ? '🌙' : '☀️'}</button
  >
</header>
