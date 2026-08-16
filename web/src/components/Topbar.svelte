<script lang="ts">
  import type { Meta, Player } from '../lib/types'
  import { deadlineState } from '../lib/data'
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

  // Outside click and Escape anywhere close them. Registered on the window so a
  // click on the page body counts, not only one inside the header.
  function onWindowPointerDown(e: MouseEvent) {
    const t = e.target as Node
    if (moreOpen && !moreMenu?.contains(t) && !moreButton?.contains(t)) closeMore(false)
    if (searchOpen && !searchBar?.contains(t) && !searchButton?.contains(t)) closeSearch(false)
  }

  function onWindowKeydown(e: KeyboardEvent) {
    if (e.key !== 'Escape') return
    closeMore()
    closeSearch()
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
    searchOpen = false
  }
  // Enter takes the top hit. This field jumps somewhere; it does not filter a
  // list in place, and behaving like a launcher is half of saying so.
  function onSearchKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' && results.length) {
      e.preventDefault()
      choose(results[0])
    }
  }

  // On a 390px bar the field costs 144px it cannot shrink out of, which is what
  // pushed the deadline countdown to `md` and above. Below `sm` it collapses to
  // its icon and expands over the bar — which also stops it standing 40px above
  // the Players filter looking like the same control.
  let searchOpen = $state(false)
  let searchBar = $state<HTMLDivElement | null>(null)
  let searchButton = $state<HTMLButtonElement | null>(null)
  let searchInput = $state<HTMLInputElement | null>(null)

  function closeSearch(returnFocus = true) {
    if (!searchOpen) return
    searchOpen = false
    q = ''
    if (returnFocus) searchButton?.focus()
  }
  $effect(() => {
    if (searchOpen) searchInput?.focus()
  })

  // The heading below already says "<gameweek> deadline", so this slot holds
  // only the value. The old string form returned the whole clause "deadline
  // passed", which read as "GW1 DEADLINE / deadline passed".
  const dl = $derived(meta?.deadline ? deadlineState(meta.deadline, now) : null)
  const timeLeft = $derived(
    dl?.state === 'until' ? dl.remaining : dl?.state === 'passed' ? 'Passed' : '',
  )

  // Data age. The deadline countdown ticks regardless of how old the underlying
  // projections are, so freshness gets its own always-visible state.
  // The deadline is passed in on purpose: near one, and after one, plain age is
  // the wrong question. See lib/freshness.ts.
  const freshness = $derived(classifyFreshness(meta?.generated_at, now, meta?.deadline))
  const freshTone = $derived(
    {
      fresh: 'text-muted border-line',
      stale: 'text-yellow border-yellow/40 bg-yellow/10',
      critical: 'text-red border-red/40 bg-red/10',
      expired: 'text-red border-red/40 bg-red/10',
      unknown: 'text-red border-red/40 bg-red/10',
    }[freshness.state],
  )

  const SEARCH_FIELD =
    'w-full rounded-full bg-bg3 border border-line2 pl-8 pr-3 py-1.5 text-sm ' +
    'placeholder:text-muted2 focus:outline-none focus:border-accent'
</script>

<svelte:window onpointerdown={onWindowPointerDown} onkeydown={onWindowKeydown} />

<!-- The results list is identical in both places the field appears, so it is
     written once. Its own header states what picking a row does — the whole
     reason this control is mistakable for the Players filter below it. -->
{#snippet searchResults()}
  {#if q.length >= 2}
    <div class="absolute left-0 right-0 sm:left-auto sm:w-64 mt-1 card shadow-xl z-50 overflow-hidden">
      <div class="px-3 py-1.5 text-[10px] uppercase font-bold text-muted2 border-b border-line">
        Opens the player's card
      </div>
      {#each results as p}
        <button
          onclick={() => choose(p)}
          class="w-full flex items-center justify-between gap-2 px-3 py-2 hover:bg-card2 text-left text-sm"
        >
          <span class="truncate"><b>{p.name}</b> <span class="text-muted">{p.pos} · {p.team}</span></span>
          <span class="shrink-0 text-brand-light font-bold tabular-nums">{p.next_gw_xp.toFixed(1)}</span>
        </button>
      {/each}
      {#if results.length === 0}
        <div class="px-3 py-2 text-sm text-muted2">No players found</div>
      {/if}
    </div>
  {/if}
{/snippet}

<header
  class="glass sticky top-0 z-40 flex items-center gap-2 sm:gap-3 px-3"
  style="height: var(--gaffer-topbar);"
>
  <button class="lg:hidden text-muted hover:text-text px-1" onclick={onmenu} aria-label="menu"><Icon name="menu" size={20} /></button>

  <!-- The season pill that used to sit here has moved to the sidebar's "built
       for" block. It never changed mid-season and it was holding ~52px that the
       countdown below needs on a phone. -->
  <div class="flex items-center gap-2 shrink-0">
    <span class="text-lg font-black tracking-tight"><span class="text-brand">G</span>affer</span>
  </div>

  {#if meta}
    <!-- The countdown is the reason anyone opens this app twenty minutes before
         a deadline, and it used to exist on a phone only on Home — not on
         Players or Planner, where the move is actually being weighed. Compact
         inline form below `md`; the bordered column above it. -->
    <div
      class="shrink-0 flex items-baseline gap-1.5 leading-tight
             md:flex-col md:items-center md:gap-0 md:px-3 md:border-l md:border-r md:border-line"
    >
      <span class="text-[9px] uppercase text-muted whitespace-nowrap">
        <span class="md:hidden">GW{meta.current_gw}</span>
        <span class="hidden md:inline">{meta.gw_name || 'GW' + meta.current_gw} deadline</span>
      </span>
      <span class="text-xs md:text-sm font-bold text-accent-light tabular-nums whitespace-nowrap"
        >{timeLeft || '—'}</span
      >
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

  <!-- Phone: the icon only. Tapping it takes the whole bar. -->
  <button
    bind:this={searchButton}
    class="sm:hidden ml-auto shrink-0 text-muted hover:text-text px-1"
    aria-label="Search every player"
    aria-expanded={searchOpen}
    onclick={() => (searchOpen ? closeSearch() : (searchOpen = true))}
  ><Icon name="search" size={18} /></button>

  <!-- ≥sm: a pill with the magnifier inside it, deliberately unlike the square
       filter box the Players page puts 40px below. Different shape, different
       placeholder, and the dropdown says where a row takes you. -->
  <div class="relative hidden sm:block ml-auto w-48 md:w-56 shrink-0">
    <Icon name="search" size={14} class="absolute left-3 top-1/2 -translate-y-1/2 text-muted2 pointer-events-none" />
    <input
      bind:value={q}
      onkeydown={onSearchKeydown}
      placeholder="Jump to any player…"
      aria-label="Jump to any player — opens their card"
      class={SEARCH_FIELD}
    />
    {@render searchResults()}
  </div>

  <button
    onclick={toggleTheme}
    class="shrink-0 text-muted hover:text-text px-1"
    title="Toggle theme"
    aria-label="toggle theme"><Icon name={theme === 'dark' ? 'moon' : 'sun'} size={18} /></button
  >

  {#if searchOpen}
    <!-- Covers the bar rather than squeezing into it: on a phone there is no
         width for both a usable field and the countdown, and the countdown is
         the one that has to be there without being asked for. -->
    <div
      bind:this={searchBar}
      class="sm:hidden absolute inset-0 z-50 flex items-center gap-2 px-3 bg-bg2"
    >
      <div class="relative flex-1 min-w-0">
        <Icon name="search" size={14} class="absolute left-3 top-1/2 -translate-y-1/2 text-muted2 pointer-events-none" />
        <input
          bind:this={searchInput}
          bind:value={q}
          onkeydown={onSearchKeydown}
          placeholder="Jump to any player…"
          aria-label="Jump to any player — opens their card"
          class={SEARCH_FIELD}
        />
        {@render searchResults()}
      </div>
      <button
        class="shrink-0 text-muted hover:text-text px-1"
        aria-label="Close search"
        onclick={() => closeSearch()}
      ><Icon name="x" size={18} /></button>
    </div>
  {/if}
</header>
