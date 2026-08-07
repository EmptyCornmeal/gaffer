<script lang="ts">
  import type { Meta, Player } from '../lib/types'
  import { countdown } from '../lib/data'
  import { getTheme, setTheme } from '../lib/config'
  import { classifyFreshness } from '../lib/freshness'
  import { matches } from '../lib/search'
  import { MORE_TABS, PRIMARY_TABS } from '../lib/nav'
  import Icon from './Icon.svelte'

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

  // Six primary destinations in the bar; the rest behind More. Both derived
  // from NAV_TABS, so a new route lands in exactly one of them.
  const primary = PRIMARY_TABS
  const more = MORE_TABS
  const moreActive = $derived(more.some((t) => t.key === route))
  const activeMoreLabel = $derived(more.find((t) => t.key === route)?.label ?? null)

  let moreOpen = $state(false)
  let moreButton = $state<HTMLButtonElement | null>(null)
  let moreMenu = $state<HTMLDivElement | null>(null)

  function openMore(focusFirst = false) {
    moreOpen = true
    if (focusFirst) {
      // Wait a tick so the menu exists before reaching into it.
      queueMicrotask(() => moreMenu?.querySelector('button')?.focus())
    }
  }

  function closeMore(returnFocus = true) {
    if (!moreOpen) return
    moreOpen = false
    if (returnFocus) moreButton?.focus()
  }

  function onMoreKeydown(e: KeyboardEvent) {
    if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      openMore(e.key === 'ArrowDown')
    }
  }

  function onMenuKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.stopPropagation()
      closeMore()
    }
  }

  // Outside click and Escape anywhere close it. Registered on the window so a
  // click on the page body counts, not only one inside the header.
  function onWindowPointerDown(e: MouseEvent) {
    if (!moreOpen) return
    const t = e.target as Node
    if (moreMenu?.contains(t) || moreButton?.contains(t)) return
    closeMore(false)
  }

  function onWindowKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') closeMore()
  }

  function goMore(key: string) {
    onnav(key)
    closeMore()
  }

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

  // Data age. The deadline countdown ticks regardless of how old the underlying
  // projections are, so freshness gets its own always-visible state.
  const freshness = $derived(classifyFreshness(meta?.generated_at, now))
  const freshTone = $derived(
    {
      fresh: 'text-muted border-line',
      stale: 'text-yellow border-yellow/40 bg-yellow/10',
      critical: 'text-red border-red/40 bg-red/10',
      unknown: 'text-red border-red/40 bg-red/10',
    }[freshness.state],
  )
</script>

<svelte:window onpointerdown={onWindowPointerDown} onkeydown={onWindowKeydown} />

<header
  class="glass sticky top-0 z-40 flex items-center gap-3 px-3"
  style="height: var(--gaffer-topbar);"
>
  <button class="lg:hidden text-muted hover:text-text px-1" onclick={onmenu} aria-label="menu"><Icon name="menu" size={20} /></button>

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

    <span
      class="shrink-0 flex items-center gap-1 text-[10px] font-semibold rounded-full border px-2 py-0.5 {freshTone}"
      title={freshness.title}
      aria-label={freshness.title}
    >
      {#if freshness.state !== 'fresh'}
        <Icon name="hourglass" size={11} />
      {/if}
      <span class="hidden sm:inline">{freshness.label}</span>
      <span class="sm:hidden">{freshness.label.replace('Updated ', '')}</span>
    </span>
  {/if}

  <nav class="hidden lg:flex items-center gap-0.5 min-w-0" aria-label="Primary">
    {#each primary as t}
      <button
        onclick={() => onnav(t.key)}
        aria-current={route === t.key ? 'page' : undefined}
        class="group relative flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm font-semibold transition whitespace-nowrap
          {route === t.key ? 'text-text' : 'text-muted hover:text-text'}"
      >
        <Icon name={t.icon} size={15} class={route === t.key ? 'text-brand-light' : 'text-muted2 group-hover:text-muted'} />
        {t.label}
        {#if route === t.key}
          <span class="absolute -bottom-[7px] left-2 right-2 h-0.5 rounded-full bg-brand"></span>
        {/if}
      </button>
    {/each}

    <!-- The remaining nine routes. A menu rather than nine more buttons: at
         1024-1440px all fifteen pushed the document to 1667px and squeezed the
         search to 26px. -->
    <div class="relative">
      <button
        bind:this={moreButton}
        onclick={() => (moreOpen ? closeMore(false) : openMore())}
        onkeydown={onMoreKeydown}
        aria-expanded={moreOpen}
        aria-controls="topbar-more-menu"
        aria-haspopup="menu"
        aria-current={moreActive ? 'page' : undefined}
        title={activeMoreLabel ? `More — currently ${activeMoreLabel}` : 'More pages'}
        class="group relative flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm font-semibold transition whitespace-nowrap
          {moreActive ? 'text-text' : 'text-muted hover:text-text'}"
      >
        <Icon name="menu" size={15} class={moreActive ? 'text-brand-light' : 'text-muted2 group-hover:text-muted'} />
        {activeMoreLabel ?? 'More'}
        <Icon name="chevron-down" size={13} class="text-muted2" />
        {#if moreActive}
          <span class="absolute -bottom-[7px] left-2 right-2 h-0.5 rounded-full bg-brand"></span>
        {/if}
      </button>

      {#if moreOpen}
        <div
          bind:this={moreMenu}
          id="topbar-more-menu"
          role="menu"
          aria-label="More pages"
          tabindex="-1"
          onkeydown={onMenuKeydown}
          class="absolute right-0 mt-2 w-52 card shadow-xl z-50 overflow-hidden py-1"
        >
          {#each more as t}
            <button
              role="menuitem"
              onclick={() => goMore(t.key)}
              aria-current={route === t.key ? 'page' : undefined}
              class="w-full flex items-center gap-2 px-3 py-2 text-left text-sm font-semibold transition
                {route === t.key ? 'text-text bg-card2' : 'text-muted hover:text-text hover:bg-card2'}"
            >
              <Icon name={t.icon} size={15} class={route === t.key ? 'text-brand-light' : 'text-muted2'} />
              {t.label}
              {#if route === t.key}<span class="ml-auto w-1.5 h-1.5 rounded-full bg-brand"></span>{/if}
            </button>
          {/each}
        </div>
      {/if}
    </div>
  </nav>

  <div class="relative ml-auto w-40 sm:w-56 shrink-0 min-w-[9rem]">
    <input
      bind:value={q}
      placeholder="Search players…"
      aria-label="Search players"
      class="w-full rounded-lg bg-card border border-line px-3 py-1.5 text-sm placeholder:text-muted2 focus:outline-none focus:border-accent"
    />
    {#if q.length >= 2}
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
        {#if results.length === 0}
          <div class="px-3 py-2 text-sm text-muted2">No players found</div>
        {/if}
      </div>
    {/if}
  </div>

  <button
    onclick={toggleTheme}
    class="shrink-0 text-muted hover:text-text px-1"
    title="Toggle theme"
    aria-label="toggle theme"><Icon name={theme === 'dark' ? 'moon' : 'sun'} size={18} /></button
  >
</header>
