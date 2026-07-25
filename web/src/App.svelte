<script lang="ts">
  import { loadBundle, type Bundle } from './lib/data'
  import { getTheme, setTheme } from './lib/config'
  import type { Player } from './lib/types'
  import Topbar from './components/Topbar.svelte'
  import Sidebar from './components/Sidebar.svelte'
  import BottomNav from './components/BottomNav.svelte'
  import PlayerDetail from './components/PlayerDetail.svelte'
  import Overview from './pages/Overview.svelte'
  import MyTeam from './pages/MyTeam.svelte'
  import Planner from './pages/Planner.svelte'
  import Players from './pages/Players.svelte'
  import Fixtures from './pages/Fixtures.svelte'
  import MiniLeague from './pages/MiniLeague.svelte'
  import News from './pages/News.svelte'
  import Accuracy from './pages/Accuracy.svelte'
  import Chips from './pages/Chips.svelte'
  import { GLOSSARY } from './lib/glossary'

  let bundle = $state<Bundle | null>(null)
  let error = $state<string | null>(null)
  let route = $state(parseRoute())
  let now = $state(Date.now())
  let selectedId = $state<number | null>(null)
  let sidebarOpen = $state(false)
  let reloadKey = $state(0)

  function parseRoute(): string {
    const h = (typeof location !== 'undefined' ? location.hash : '').replace(/^#\/?/, '')
    return h || 'overview'
  }
  function nav(r: string) {
    location.hash = `#/${r}`
    sidebarOpen = false
  }

  const byId = $derived(new Map((bundle?.players ?? []).map((p) => [p.id, p])))
  const selected = $derived<Player | null>(selectedId != null ? byId.get(selectedId) ?? null : null)

  $effect(() => {
    setTheme(getTheme())
    loadBundle().then((b) => (bundle = b)).catch((e) => (error = String(e)))
    const onHash = () => (route = parseRoute())
    window.addEventListener('hashchange', onHash)
    const t = setInterval(() => (now = Date.now()), 30000)
    return () => {
      window.removeEventListener('hashchange', onHash)
      clearInterval(t)
    }
  })
</script>

<div class="min-h-svh flex flex-col">
  <Topbar
    meta={bundle?.meta ?? null}
    players={bundle?.players ?? []}
    {route}
    {now}
    onnav={nav}
    onpick={(id) => (selectedId = id)}
    onmenu={() => (sidebarOpen = true)}
  />

  <div class="flex flex-1 min-h-0">
    <Sidebar
      meta={bundle?.meta ?? null}
      playerCount={bundle?.players.length ?? 0}
      open={sidebarOpen}
      {route}
      onnav={nav}
      onclose={() => (sidebarOpen = false)}
      onsaved={() => (reloadKey += 1)}
    />

    <main
      class="flex-1 min-w-0 overflow-y-auto p-3 sm:p-5"
      style="padding-bottom: calc(var(--gaffer-bottomnav) + env(safe-area-inset-bottom) + 1rem);"
    >
      {#if error}
        <div class="card p-4 text-red text-sm max-w-lg mx-auto">Couldn't load data — run the pipeline so <code>data/*.json</code> is served.<div class="mt-1 text-muted2">{error}</div></div>
      {:else if !bundle}
        <div class="flex flex-col items-center justify-center py-32 text-muted gap-3">
          <div class="w-8 h-8 rounded-full border-2 border-line border-t-brand animate-spin"></div>
          Loading…
        </div>
      {:else if route === 'overview'}
        <Overview {bundle} onpick={(id) => (selectedId = id)} onnav={nav} />
      {:else if route === 'my-team'}
        {#key reloadKey}<MyTeam {bundle} onpick={(id) => (selectedId = id)} ongoSettings={() => (sidebarOpen = true)} />{/key}
      {:else if route === 'planner'}
        <Planner {bundle} onpick={(id) => (selectedId = id)} />
      {:else if route === 'players'}
        <Players players={bundle.players} onpick={(id) => (selectedId = id)} />
      {:else if route === 'fixtures'}
        <Fixtures fixtures={bundle.fixtures} />
      {:else if route === 'chips'}
        <Chips {bundle} onnav={nav} />
      {:else if route === 'league'}
        {#key reloadKey}<MiniLeague ongoSettings={() => (sidebarOpen = true)} />{/key}
      {:else if route === 'news'}
        <News {bundle} />
      {:else if route === 'accuracy'}
        <Accuracy {bundle} />
      {:else if route === 'help'}
        <div class="rise max-w-3xl">
          <h2 class="font-bold text-lg mb-1">How Gaffer works</h2>
          <p class="text-sm text-muted mb-4">Every projection is a transparent sum of parts — appearance, goals, assists, clean sheet, DEFCON and bonus — gated by projected minutes, and each pick comes with a plain-English reason. Glossary below.</p>
          <div class="grid sm:grid-cols-2 gap-3">
            {#each Object.entries(GLOSSARY) as [term, def]}
              <div class="card p-3"><span class="chip chip-info">{term}</span><p class="text-sm text-muted mt-1">{def}</p></div>
            {/each}
          </div>
        </div>
      {/if}
    </main>
  </div>
</div>

<BottomNav {route} onnav={nav} onmore={() => (sidebarOpen = true)} />

<PlayerDetail player={selected} onclose={() => (selectedId = null)} />
