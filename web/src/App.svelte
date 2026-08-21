<script lang="ts">
  import { loadBundle, loadShell, type Bundle, type Shell } from './lib/data'
  import { getTheme, setTheme } from './lib/config'
  import { normaliseRoute } from './lib/nav'
  import type { Player } from './lib/types'
  import Topbar from './components/Topbar.svelte'
  import Sidebar from './components/Sidebar.svelte'
  import BottomNav from './components/BottomNav.svelte'
  import PlayerDetail from './components/PlayerDetail.svelte'
  // Eagerly bundled: the routes a phone opens first, and the small ones where a
  // second network round-trip would cost more than the code it saves.
  import Home from './pages/Home.svelte'
  import MyTeam from './pages/MyTeam.svelte'
  import Fixtures from './pages/Fixtures.svelte'
  import Help from './pages/Help.svelte'

  // Lazily loaded: heavy screens most sessions never open. Each is its own
  // chunk, fetched on first navigation and then cached by the browser.
  const LAZY: Record<string, () => Promise<{ default: unknown }>> = {
    planner: () => import('./pages/Planner.svelte'),
    players: () => import('./pages/Players.svelte'),
    live: () => import('./pages/Live.svelte'),
    league: () => import('./pages/MiniLeague.svelte'),
    news: () => import('./pages/News.svelte'),
    accuracy: () => import('./pages/Accuracy.svelte'),
  }

  let shell = $state<Shell | null>(null)
  let bundle = $state<Bundle | null>(null)
  let error = $state<string | null>(null)
  let route = $state(parseRoute())
  let now = $state(Date.now())
  let selectedId = $state<number | null>(null)
  let sidebarOpen = $state(false)
  let reloadKey = $state(0)

  // Resolved lazy components, by route key.
  let loaded = $state<Record<string, any>>({})
  let chunkError = $state<string | null>(null)

  // Unknown / malformed hashes fall back to DEFAULT_ROUTE, and a route that has
  // been merged away is forwarded to whatever absorbed it (see REDIRECTS), rather
  // than rendering an empty <main>. Normalising (instead of rewriting
  // location.hash) keeps back/forward working and cannot loop.
  function parseRoute(): string {
    return normaliseRoute(typeof location !== 'undefined' ? location.hash : '')
  }
  function nav(r: string) {
    location.hash = `#/${r}`
    sidebarOpen = false
  }

  const byId = $derived(new Map((bundle?.players ?? []).map((p) => [p.id, p])))
  const selected = $derived<Player | null>(selectedId != null ? byId.get(selectedId) ?? null : null)
  const meta = $derived(bundle?.meta ?? shell?.meta ?? null)
  const LazyPage = $derived(loaded[route] ?? null)
  const needsChunk = $derived(route in LAZY && !loaded[route])

  // Fetch the chunk for whichever heavy route is active. Failures surface as a
  // page-level message rather than a blank <main>.
  $effect(() => {
    const r = route
    if (!(r in LAZY) || loaded[r]) return
    chunkError = null
    LAZY[r]()
      .then((m) => (loaded = { ...loaded, [r]: m.default }))
      .catch((e) => (chunkError = String(e)))
  })

  $effect(() => {
    setTheme(getTheme())
    // meta.json first (~1 kB) so the shell shows the gameweek, deadline,
    // freshness and build mode without waiting for megabytes of players.
    loadShell()
      .then((s) => (shell = s))
      .catch(() => {})
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
  <a href="#main" class="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:m-2 focus:px-3 focus:py-2 focus:rounded-lg focus:bg-brand focus:text-[#05210f] focus:font-bold">
    Skip to content
  </a>

  <Topbar
    meta={meta}
    players={bundle?.players ?? []}
    {route}
    {now}
    onnav={nav}
    onpick={(id) => (selectedId = id)}
    onmenu={() => (sidebarOpen = true)}
  />

  <div class="flex flex-1 min-h-0">
    <Sidebar
      meta={meta}
      playerCount={bundle?.players.length ?? 0}
      open={sidebarOpen}
      {route}
      onnav={nav}
      onclose={() => (sidebarOpen = false)}
      onsaved={() => (reloadKey += 1)}
    />

    <main
      id="main"
      class="flex-1 min-w-0 overflow-y-auto p-3 sm:p-5"
      style="padding-bottom: calc(var(--gaffer-bottomnav) + env(safe-area-inset-bottom) + 1rem);"
    >
      {#if error}
        <!-- This is read by whoever opened the published site, who cannot run
             anything. The old copy said "run the pipeline so data/*.json is
             served", which is an instruction to the one person who does not
             need it. -->
        <div class="card p-4 text-red text-sm max-w-lg mx-auto">Couldn't load the latest data. The published files did not come back — usually a passing problem, so try reloading in a moment.<div class="mt-1 text-muted2">{error}</div></div>
      {:else if !bundle}
        <div class="flex flex-col items-center justify-center py-32 text-muted gap-3" role="status" aria-live="polite">
          <div class="w-8 h-8 rounded-full border-2 border-line border-t-brand animate-spin motion-reduce:animate-none"></div>
          {shell ? 'Loading players…' : 'Loading…'}
        </div>
      {:else if chunkError}
        <div class="card p-4 text-red text-sm max-w-lg mx-auto">
          Couldn't load that page.
          <div class="mt-1 text-muted2">{chunkError}</div>
          <button class="btn mt-3" onclick={() => nav('planner')}>Back to Planner</button>
        </div>
      {:else if needsChunk}
        <div class="flex flex-col items-center justify-center py-32 text-muted gap-3" role="status" aria-live="polite">
          <div class="w-8 h-8 rounded-full border-2 border-line border-t-brand animate-spin motion-reduce:animate-none"></div>
          Loading page…
        </div>
      {:else if route === 'home'}
        <Home {bundle} onnav={nav} onpick={(id) => (selectedId = id)} {now} />
      {:else if route === 'my-team'}
        {#key reloadKey}<MyTeam {bundle} onpick={(id) => (selectedId = id)} ongoSettings={() => (sidebarOpen = true)} onnav={nav} />{/key}
      {:else if route === 'fixtures'}
        <Fixtures fixtures={bundle.fixtures} />
      {:else if route === 'help'}
        <Help />
      {:else if route === 'players' && LazyPage}
        <LazyPage players={bundle.players} onpick={(id: number) => (selectedId = id)} />
      {:else if route === 'planner' && LazyPage}
        <LazyPage {bundle} onpick={(id: number) => (selectedId = id)} onnav={nav} />
      {:else if route === 'live' && LazyPage}
        <LazyPage {bundle} onpick={(id: number) => (selectedId = id)} />
      {:else if route === 'league' && LazyPage}
        {#key reloadKey}<LazyPage ongoSettings={() => (sidebarOpen = true)} {bundle} onnav={nav} onpick={(id: number) => (selectedId = id)} {now} />{/key}
      {:else if route === 'news' && LazyPage}
        <LazyPage {bundle} />
      {:else if route === 'accuracy' && LazyPage}
        <LazyPage {bundle} />
      {:else}
        <!-- Belt-and-braces: parseRoute() already normalises, so this only fires
             if a route key is added to NAV_TABS without a branch above. -->
        <Home {bundle} onnav={nav} onpick={(id) => (selectedId = id)} {now} />
      {/if}
    </main>
  </div>
</div>

<BottomNav {route} onnav={nav} onmore={() => (sidebarOpen = true)} />

<PlayerDetail player={selected} onclose={() => (selectedId = null)} />
