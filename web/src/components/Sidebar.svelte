<script lang="ts">
  import type { Meta } from '../lib/types'
  import {
    getEntryId, setEntryId, getLeagueIds, setLeagueIds, parseId,
  } from '../lib/config'
  import { NAV_TABS } from '../lib/nav'
  import Icon from './Icon.svelte'

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

  let aside = $state<HTMLElement | null>(null)
  // Tailwind's `lg` breakpoint. Tracked rather than assumed so the drawer is
  // only made inert on the layout where it is genuinely hidden.
  let narrow = $state(true)
  $effect(() => {
    if (typeof window === 'undefined') return
    const mq = window.matchMedia('(max-width: 1023px)')
    const sync = () => (narrow = mq.matches)
    sync()
    mq.addEventListener('change', sync)
    return () => mq.removeEventListener('change', sync)
  })
  // Opening the drawer moves focus into it, so a keyboard user is not left
  // behind on the button that opened it.
  $effect(() => {
    if (open && narrow) aside?.querySelector<HTMLElement>('button')?.focus()
  })

  let entry = $state(getEntryId()?.toString() ?? '')
  let leagues = $state(getLeagueIds().join(', '))
  let saved = $state(false)

  // Two fields touched roughly once ever held the top-left corner of all fifteen
  // pages. They fold away — but only once an id exists, because with no id
  // configured, setting one *is* the job and it should be the first thing here.
  let settingsOpen = $state(!getEntryId())
  let entryEl = $state<HTMLInputElement | null>(null)
  let wantsEntryFocus = $state(false)

  function revealSettings() {
    settingsOpen = true
    wantsEntryFocus = true
  }
  // Waits for the field to exist rather than guessing at a tick: `entryEl` is
  // reactive, so this re-runs the moment the panel unfolds.
  $effect(() => {
    if (wantsEntryFocus && entryEl) {
      entryEl.focus()
      wantsEntryFocus = false
    }
  })

  // `open` is how "Go to settings" on My Team and League reaches this panel. On
  // a phone that raises the drawer, where the fields were always visible and
  // still are. On a desktop the rail is already on screen, so that button used
  // to do nothing observable whatsoever; now it unfolds the panel it named.
  $effect(() => {
    if (!open) return
    settingsOpen = true
    // Focus is only stolen where the panel was already in view. On a phone the
    // drawer itself is the new thing and focus belongs at its top, not in a
    // numeric field that summons the keyboard.
    if (!narrow) wantsEntryFocus = true
  })

  function save() {
    setEntryId(parseId(entry))
    setLeagueIds(
      leagues.split(',').map((s) => parseId(s.trim())).filter((n): n is number => !!n),
    )
    saved = true
    setTimeout(() => (saved = false), 1500)
    onsaved()
  }
</script>

<svelte:window onkeydown={(e) => open && e.key === 'Escape' && onclose()} />

<!-- backdrop for mobile drawer -->
{#if open}
  <button class="fixed inset-0 bg-black/50 z-30 lg:hidden" aria-label="close" onclick={onclose}></button>
{/if}

<!-- `inert` when closed on phones: a drawer that is merely translated off-screen
     still holds keyboard focus and is still announced by a screen reader, so a
     Tab press from the topbar disappears into an invisible menu. `lg:` layouts
     show it permanently, hence the width check rather than a blanket flag. -->
<aside
  bind:this={aside}
  inert={!open && narrow ? true : undefined}
  aria-hidden={!open && narrow ? 'true' : undefined}
  class="fixed lg:sticky top-0 lg:top-[var(--gaffer-topbar)] left-0 z-40 lg:z-0
         h-svh lg:h-[calc(100svh-var(--gaffer-topbar))] overflow-y-auto
         bg-bg2 border-r border-line p-4 shrink-0 transition-transform motion-reduce:transition-none
         {open ? 'translate-x-0' : '-translate-x-full'} lg:translate-x-0"
  style="width: var(--gaffer-sidebar);"
>
  <div class="lg:hidden flex items-center justify-between mb-3">
    <h3 class="font-bold">Menu</h3>
    <button class="text-muted hover:text-text" onclick={onclose} aria-label="close"><Icon name="x" size={18} /></button>
  </div>

  <!-- Page navigation — mobile only (the topbar carries it on ≥lg). Without this
       the drawer had zero nav links, trapping phone users on Overview. -->
  <nav class="lg:hidden mb-4 grid gap-0.5">
    {#each NAV_TABS as t}
      <button
        onclick={() => onnav(t.key)}
        class="flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm font-semibold text-left transition
          {route === t.key ? 'bg-brand/12 text-text' : 'text-muted hover:text-text hover:bg-card2'}"
      >
        <Icon name={t.icon} size={16} class={route === t.key ? 'text-brand-light' : ''} />{t.label}
      </button>
    {/each}
  </nav>

  <!-- Whose numbers these are.
       This is the one question the rail is better placed to answer than any
       page, and it belongs at the top of every screen: an entry id typed once
       decides whether the advice above is about your squad or a stranger's.
       The form that used to hold this corner is two fields below. -->
  {#if meta}
    <button
      onclick={() => onnav('my-team')}
      aria-current={route === 'my-team' ? 'page' : undefined}
      class="card card-hover w-full text-left p-3"
    >
      <div class="text-micro uppercase font-bold text-muted2 tracking-wide">Built for</div>
      <div class="font-bold truncate">{meta.entry_name ?? 'No team linked'}</div>
      <div class="text-mini text-muted truncate">
        {meta.manager_name ?? 'unknown manager'} · {meta.season}
      </div>
    </button>
    {#if meta.build_mode === 'generic'}
      <!-- The one state where the settings are urgent, so it opens them. -->
      <button class="btn btn-ghost w-full mt-2 text-xs" onclick={revealSettings}>
        Generic build — set your Entry ID
      </button>
    {/if}
  {/if}

  <!-- Reachable in one click; not permanently unfurled. -->
  <div class="mt-4 border-t border-line pt-2">
    <button
      class="w-full flex items-center gap-2 text-sm font-semibold text-muted hover:text-text"
      aria-expanded={settingsOpen}
      onclick={() => (settingsOpen = !settingsOpen)}
    >
      <Icon name="sliders" size={15} />
      Settings
      <Icon
        name="chevron-down"
        size={13}
        class="ml-auto transition-transform motion-reduce:transition-none {settingsOpen ? 'rotate-180' : ''}"
      />
    </button>

    {#if settingsOpen}
      <div class="mt-1">
        <label for="fpl-entry-id" class="block text-xs text-muted mb-1">FPL Entry ID</label>
        <input
          id="fpl-entry-id"
          bind:this={entryEl}
          bind:value={entry}
          inputmode="numeric"
          placeholder="e.g. 1234567 or paste team URL"
          class="w-full rounded-lg bg-card border border-line px-3 py-2 text-sm mb-3 focus:outline-none focus:border-accent"
        />

        <label for="fpl-league-ids" class="block text-xs text-muted mb-1">Classic League IDs</label>
        <input
          id="fpl-league-ids"
          bind:value={leagues}
          placeholder="e.g. 12345, 67890"
          class="w-full rounded-lg bg-card border border-line px-3 py-2 text-sm mb-3 focus:outline-none focus:border-accent"
        />

        <button class="btn w-full" onclick={save}>{saved ? 'Saved ✓' : 'Save'}</button>

        <p class="mt-3 text-mini text-muted2 leading-relaxed">
          Your team &amp; league tables are fetched live from these IDs. They do not
          change the projections or the recommended squad.
        </p>
      </div>
    {/if}
  </div>

  <button
    onclick={() => onnav('help')}
    class="mt-3 w-full text-left text-sm font-semibold text-muted hover:text-text flex items-center gap-2"
  >
    <Icon name="book" size={15} /> Glossary &amp; how it works →
  </button>

  <div class="mt-4 grid gap-2 text-center text-xs {meta?.last_finished_gw ? 'grid-cols-3' : 'grid-cols-2'}">
    <div class="card py-2"><div class="font-bold">20</div><div class="text-muted">teams</div></div>
    <div class="card py-2"><div class="font-bold">{playerCount}</div><div class="text-muted">players</div></div>
    <!-- Pre-season nothing has finished, and an em dash in a stat tile reads as a
         failed lookup rather than "not yet". -->
    {#if meta?.last_finished_gw}
      <div class="card py-2"><div class="font-bold">{meta.last_finished_gw}</div><div class="text-muted">last GW</div></div>
    {/if}
  </div>

  <p class="mt-4 text-mini text-muted2 leading-relaxed">
    Projections are a snapshot from the last pipeline run — see the data-age chip
    in the header.
  </p>
</aside>
