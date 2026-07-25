<script lang="ts">
  import { BOTTOM_TABS } from '../lib/nav'

  let {
    route,
    onnav,
    onmore,
  }: {
    route: string
    onnav: (r: string) => void
    onmore: () => void
  } = $props()
</script>

<!-- Phone bottom tab bar (hidden ≥lg, where the topbar nav takes over). Primary
     destinations + a More button that opens the drawer for the rest. -->
<nav
  class="lg:hidden fixed bottom-0 inset-x-0 z-40 flex items-stretch
         border-t border-line bg-bg2/95 backdrop-blur"
  style="height: var(--gaffer-bottomnav); padding-bottom: env(safe-area-inset-bottom);"
  aria-label="Primary"
>
  {#each BOTTOM_TABS as t}
    <button
      onclick={() => onnav(t.key)}
      aria-current={route === t.key ? 'page' : undefined}
      class="flex-1 flex flex-col items-center justify-center gap-0.5 text-[10px] font-semibold
        {route === t.key ? 'text-accent-light' : 'text-muted'}"
    >
      <span class="text-lg leading-none" aria-hidden="true">{t.icon}</span>
      {t.label}
    </button>
  {/each}
  <button
    onclick={onmore}
    class="flex-1 flex flex-col items-center justify-center gap-0.5 text-[10px] font-semibold text-muted"
    aria-label="More pages and settings"
  >
    <span class="text-lg leading-none" aria-hidden="true">☰</span>
    More
  </button>
</nav>
