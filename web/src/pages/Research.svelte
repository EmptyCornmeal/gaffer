<script lang="ts">
  /**
   * 6.3 -- Research: one destination for the two pages a reader uses to look
   * something up rather than to decide something.
   *
   * Players and Fixtures were separate top-level tabs competing with the five
   * destinations that answer a weekly question. They are not a weekly question;
   * they are the reference material behind one. Merging them is not a saving of
   * pixels -- it is what lets `Now`, `My Team`, `League` and `Model` be the
   * whole navigation rather than four of eight things.
   *
   * A thin wrapper on purpose. Both pages keep their own state, their own
   * search and their own tests; nothing about them is rewritten to live here.
   */
  import { untrack } from 'svelte'
  import type { Bundle } from '../lib/data'
  import Players from './Players.svelte'
  import Fixtures from './Fixtures.svelte'

  let { bundle, onpick, tab = 'players' }: {
    bundle: Bundle
    onpick: (id: number) => void
    /** Which sub-page to open. Carried in the hash, so a link can name one. */
    tab?: string
  } = $props()

  const TABS = [
    { key: 'players', label: 'Players' },
    { key: 'fixtures', label: 'Fixtures' },
  ] as const

  // The deep link decides the initial tab; after that the reader does. Read
  // once and untracked, so a link opens the right sub-page without the URL
  // then fighting every click the reader makes.
  let active = $state(untrack(() => (tab === 'fixtures' ? 'fixtures' : 'players')))
</script>

<div class="flex flex-col gap-4 w-full">
  <nav class="flex gap-1 border-b border-line" aria-label="Research sections">
    {#each TABS as t (t.key)}
      <button
        class="px-3 py-2 text-sm font-semibold border-b-2 -mb-px min-h-11
               {active === t.key ? 'border-brand text-text' : 'border-transparent text-muted hover:text-text'}"
        aria-current={active === t.key ? 'page' : undefined}
        onclick={() => (active = t.key)}
      >{t.label}</button>
    {/each}
  </nav>

  {#if active === 'fixtures'}
    <Fixtures fixtures={bundle.fixtures} />
  {:else}
    <Players players={bundle.players} {onpick} {bundle} />
  {/if}
</div>
