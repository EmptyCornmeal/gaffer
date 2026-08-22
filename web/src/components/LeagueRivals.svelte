<script lang="ts">
  // Who owns what, and what has actually decided this league.
  //
  // Everything on this page comes from rival squads — the one thing Gaffer holds
  // that the FPL app does not show you. The grid is this gameweek; the ledger is
  // the season, and is the Live page's "biggest swing" widened from one player in
  // one week to every player across every week that has finished.

  import type { Bundle } from '../lib/data'
  import type { LeagueStanding } from '../lib/fpl'
  import { fpl } from '../lib/fpl'
  import { managerLabel, type ManagerSquad } from '../lib/league/squads'
  import { columnLabels, ownershipGrid, overlap, type GridRow } from '../lib/league/grid'
  import { concentration, rivalLedger } from '../lib/league/ledger'
  import { loadGrid, loadLedger, visibleGw, type Reader } from '../lib/league/load'
  import Icon from './Icon.svelte'

  let {
    bundle, rows, myEntry, onpick, now = Date.now(),
  }: {
    bundle: Bundle
    rows: LeagueStanding[]
    myEntry: number | null
    onpick: (id: number) => void
    now?: number
  } = $props()

  const reader: Reader = {
    picks: (entry, gw) => fpl.picks(entry, gw) as unknown as Promise<any>,
    live: (gw) => fpl.live(gw),
  }

  const currentGw = $derived(Number(bundle.meta?.current_gw ?? 0) || 0)
  const lastFinished = $derived(Number(bundle.meta?.last_finished_gw ?? 0) || 0)
  const gridGw = $derived(visibleGw(currentGw, bundle.meta?.deadline, now))
  const entries = $derived(rows.map((r) => r.entry))
  const nameOf = $derived(new Map(rows.map((r) => [r.entry, managerLabel(r)])))

  // Player metadata for the grid rows. `players.json` is already in the bundle.
  const players = $derived(new Map(bundle.players.map((p) => [p.id, p])))

  type Phase = 'idle' | 'loading' | 'ok' | 'error'
  let phase = $state<Phase>('idle')
  let failure = $state('')
  let squads = $state<ManagerSquad[]>([])
  let unread = $state(0)
  let loadedGw = $state<number | null>(null)

  // ---- the grid -----------------------------------------------------------
  $effect(() => {
    const gw = gridGw
    const ids = entries
    if (!gw || !ids.length || !fpl.configured()) {
      phase = 'idle'
      return
    }
    let cancelled = false
    phase = 'loading'
    loadGrid(ids, gw, reader)
      .then((out) => {
        if (cancelled) return
        squads = out.squads
        unread = out.unread
        loadedGw = out.gw
        phase = 'ok'
      })
      .catch((e) => {
        if (cancelled) return
        failure = e instanceof Error ? e.message : String(e)
        phase = 'error'
      })
    return () => { cancelled = true }
  })

  const grid = $derived(ownershipGrid(squads, myEntry))
  const managers = $derived(squads.map((s) => s.entry))
  const heads = $derived(columnLabels(
    new Map(managers.map((e) => [e, nameOf.get(e) ?? String(e)]))))
  // A player nobody in the league scores is noise in a 15-row-per-manager table;
  // the ones held but benched by everyone are still worth showing, because that
  // is a real thing to know about a rival.
  const gridRows = $derived(grid.filter((r) => r.held > 0))
  const yourDifferentials = $derived(gridRows.filter((r) => r.yourDifferential))
  // A player nobody in the league started is real information and stays
  // reachable, but he is the tail of a table sorted by what is actually
  // scoring. Folded away by default, and the count is always on screen — a
  // table that quietly drops rows is just a wrong answer with a confident face.
  let showBenched = $state(false)
  const benchedOnly = $derived(gridRows.filter((r) => r.effectiveOwnership === 0))
  const shownRows = $derived(
    showBenched ? gridRows : gridRows.filter((r) => r.effectiveOwnership > 0))
  const mySquad = $derived(squads.find((s) => s.entry === myEntry) ?? null)

  function weightLabel(r: GridRow, entry: number): string {
    const w = r.byEntry.get(entry)
    if (w == null) return ''
    if (w === 0) return 'B'
    if (w === 1) return '•'
    return `${w}×`
  }
  function weightClass(r: GridRow, entry: number): string {
    const w = r.byEntry.get(entry)
    if (w == null) return 'text-line'
    if (w === 0) return 'text-muted2'
    if (w >= 2) return 'text-yellow font-bold'
    return 'text-brand-light'
  }

  // ---- the ledger ---------------------------------------------------------
  const finalGws = $derived(
    Array.from({ length: Math.max(0, lastFinished) }, (_, i) => i + 1))

  type LedgerPhase = 'idle' | 'loading' | 'ok' | 'error'
  let ledgerPhase = $state<LedgerPhase>('idle')
  let ledgerFail = $state('')
  let byGw = $state<Map<number, Map<number, ManagerSquad>>>(new Map())
  let gwPoints = $state<Map<number, { gw: number; points: Map<number, number> }>>(new Map())
  let unreadableGws = $state<number[]>([])
  let target = $state<number | null>(null)

  $effect(() => {
    const gws = finalGws
    const ids = entries
    if (!gws.length || !ids.length || !myEntry || !fpl.configured()) {
      ledgerPhase = 'idle'
      return
    }
    let cancelled = false
    ledgerPhase = 'loading'
    loadLedger(ids, gws, reader)
      .then((out) => {
        if (cancelled) return
        byGw = out.byGw
        gwPoints = out.points
        unreadableGws = out.unreadable
        ledgerPhase = 'ok'
      })
      .catch((e) => {
        if (cancelled) return
        ledgerFail = e instanceof Error ? e.message : String(e)
        ledgerPhase = 'error'
      })
    return () => { cancelled = true }
  })

  /** One manager's squads across the season, keyed by gameweek. */
  function seasonOf(entry: number): Map<number, ManagerSquad> {
    const out = new Map<number, ManagerSquad>()
    for (const [gw, forGw] of byGw) {
      const s = forGw.get(entry)
      if (s) out.set(gw, s)
    }
    return out
  }

  // Default to the manager immediately above you — the one the season is
  // actually against. At the top of the table that is the one immediately below.
  const defaultTarget = $derived.by(() => {
    if (!myEntry) return null
    const i = rows.findIndex((r) => r.entry === myEntry)
    if (i < 0) return rows[0]?.entry ?? null
    return rows[i - 1]?.entry ?? rows[i + 1]?.entry ?? null
  })
  const chosen = $derived(target ?? defaultTarget)

  const ledger = $derived.by(() => {
    if (!myEntry || !chosen || ledgerPhase !== 'ok') return null
    return rivalLedger(chosen, seasonOf(myEntry), seasonOf(chosen), gwPoints)
  })
  const topShare = $derived(ledger ? concentration(ledger, 3) : 0)
  const rivalSquad = $derived(squads.find((s) => s.entry === chosen) ?? null)
  const squadOverlap = $derived(
    mySquad && rivalSquad ? overlap(mySquad, rivalSquad) : null)

  function pname(id: number): string {
    return players.get(id)?.name ?? `#${id}`
  }
  function signed(n: number): string {
    return n > 0 ? `+${Math.round(n)}` : String(Math.round(n))
  }
</script>

<div class="rise flex flex-col gap-4 max-w-5xl mx-auto w-full">
  <div>
    <h2 class="font-bold text-lg flex items-center gap-2">
      <Icon name="users" size={18} /> Rivals
    </h2>
    <p class="text-sm text-muted">
      Who owns what, and what has actually decided this league — from their real
      squads, not global ownership.
    </p>
  </div>

  <!-- ─────────────────────────────── the grid ─────────────────────────── -->
  {#if phase === 'idle'}
    <div class="card p-6 text-center">
      <h3 class="font-bold">No squads to compare yet</h3>
      <p class="text-sm text-muted mt-2">
        FPL publishes everyone's picks at the deadline and not before, so there is
        nothing to read until the first gameweek of the season has started.
      </p>
    </div>
  {:else if phase === 'loading'}
    <div class="flex justify-center py-16 text-muted" role="status" aria-live="polite">
      <div class="w-8 h-8 rounded-full border-2 border-line border-t-brand animate-spin motion-reduce:animate-none"></div>
    </div>
  {:else if phase === 'error'}
    <div class="card p-6">
      <h3 class="font-bold text-yellow">Couldn't read the league's squads</h3>
      <p class="text-sm text-muted mt-2">{failure}</p>
    </div>
  {:else if !squads.length}
    <div class="card p-6 text-center">
      <h3 class="font-bold">Nobody's squad could be read</h3>
      <p class="text-sm text-muted mt-2">
        The proxy answered, but no manager in this league had picks published for
        GW{loadedGw}.
      </p>
    </div>
  {:else}
    <div class="card p-3">
      <div class="flex items-baseline justify-between gap-2 flex-wrap">
        <h3 class="font-bold text-sm">Who owns what · GW{loadedGw}</h3>
        <span class="text-mini text-muted2">
          {squads.length} squad{squads.length === 1 ? '' : 's'}{#if unread}, {unread} unreadable{/if}
        </span>
      </div>
      <p class="text-mini text-muted2 mt-0.5 mb-2">
        <b class="text-brand-light">&bull;</b> in the XI ·
        <b class="text-yellow">2&times;</b> captained ·
        <b class="text-muted2">B</b> benched.
        <b>EO</b> is effective ownership: how many copies of his points land in
        this league per manager, so a captain counts twice.
      </p>
      {#if unread}
        <p class="text-mini chip-warn rounded-lg px-3 py-1.5 mb-2 inline-flex items-center gap-1.5">
          <Icon name="hourglass" size={12} />
          {unread} manager{unread === 1 ? '' : 's'} could not be read — this grid is
          that many squads short.
        </p>
      {/if}
      <div class="overflow-x-auto">
        <table class="data">
          <thead>
            <tr>
              <th class="!text-left">Player</th>
              <th class="!text-left">Team</th>
              {#each managers as entry}
                <th title={nameOf.get(entry) ?? String(entry)}
                    class={entry === myEntry ? 'text-brand-light' : ''}>
                  {heads.get(entry) ?? '?'}
                </th>
              {/each}
              <th>EO</th>
            </tr>
          </thead>
          <tbody>
            {#each shownRows as r}
              <tr
                class="cursor-pointer transition hover:bg-line/20 {r.yourDifferential ? 'bg-brand/10' : ''}"
                onclick={() => onpick(r.playerId)}
                title="Open {pname(r.playerId)}">
                <td class="!text-left">
                  {pname(r.playerId)}
                  {#if r.yourDifferential}<span class="chip chip-good ml-1">only you</span>{/if}
                </td>
                <td class="!text-left text-muted">{players.get(r.playerId)?.team ?? ''}</td>
                {#each managers as entry}
                  <td class={weightClass(r, entry)}>{weightLabel(r, entry)}</td>
                {/each}
                <td class="tabular-nums {r.effectiveOwnership >= 100 ? 'text-yellow font-semibold' : 'text-muted'}">
                  {Math.round(r.effectiveOwnership)}%
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if benchedOnly.length}
        <button
          class="text-mini text-accent-light hover:underline mt-2"
          onclick={() => (showBenched = !showBenched)}
        >
          {#if showBenched}
            Hide the {benchedOnly.length} nobody started
          {:else}
            {benchedOnly.length} more nobody in this league started &mdash; show {benchedOnly.length === 1 ? 'him' : 'them'}
          {/if}
        </button>
      {/if}
      {#if myEntry && mySquad}
        <p class="text-mini mt-2 {yourDifferentials.length ? 'text-brand-light' : 'text-muted2'}">
          {#if yourDifferentials.length}
            <b>{yourDifferentials.length}</b> player{yourDifferentials.length === 1 ? '' : 's'}
            nobody else in this league holds:
            {yourDifferentials.map((r) => pname(r.playerId)).join(', ')}.
          {:else}
            You hold nothing this league does not. Every point you score, somebody
            else scores too.
          {/if}
        </p>
      {/if}
    </div>
  {/if}

  <!-- ───────────────────────────── the ledger ─────────────────────────── -->
  <div class="card p-3">
    <div class="flex items-baseline justify-between gap-2 flex-wrap">
      <h3 class="font-bold text-sm">What has decided it</h3>
      {#if ledger && ledger.counted.length}
        <span class="text-mini text-muted2">
          GW{ledger.counted[0]}&ndash;{ledger.counted[ledger.counted.length - 1]}
        </span>
      {/if}
    </div>
    <p class="text-mini text-muted2 mt-0.5 mb-2">
      Every point between you and one rival, attributed to the player who won or
      lost it. A gameweek only counts here once our arithmetic reproduces FPL's
      own score for it exactly.
    </p>

    {#if !myEntry}
      <p class="text-sm text-muted">
        Set your entry id in Settings and this becomes a ledger of you against
        each of them.
      </p>
    {:else if !finalGws.length}
      <p class="text-sm text-muted">
        No gameweek has finished yet. Until one has, the only honest reading of
        this league is the live table — points still move, and a bonus point that
        has not been confirmed cannot be attributed to anybody.
      </p>
    {:else if ledgerPhase === 'loading'}
      <div class="flex justify-center py-10 text-muted" role="status" aria-live="polite">
        <div class="w-8 h-8 rounded-full border-2 border-line border-t-brand animate-spin motion-reduce:animate-none"></div>
      </div>
    {:else if ledgerPhase === 'error'}
      <p class="text-sm text-yellow">Couldn't read the season: {ledgerFail}</p>
    {:else if ledger}
      <div class="flex gap-1 flex-wrap mb-3">
        {#each rows.filter((r) => r.entry !== myEntry) as r}
          <button
            onclick={() => (target = r.entry)}
            class="px-2.5 py-1 rounded-lg text-xs font-semibold border transition {chosen === r.entry ? 'border-brand bg-brand/15 text-brand-light' : 'border-line text-muted hover:text-text'}"
          >{nameOf.get(r.entry)}</button>
        {/each}
      </div>

      {#if !ledger.counted.length}
        <p class="text-sm text-muted">
          Nothing counted yet.
          {#if ledger.dropped.length}
            GW{ledger.dropped.join(', GW')} did not reconcile against FPL's published
            score, so {ledger.dropped.length === 1 ? 'it is' : 'they are'} left out
            rather than guessed at.
          {/if}
        </p>
      {:else}
        <p class="text-sm">
          You are
          <b class={ledger.gap >= 0 ? 'text-brand-light' : 'text-red'}>
            {signed(ledger.gap)}
          </b>
          on <b>{nameOf.get(ledger.entry)}</b> over
          {ledger.counted.length} counted gameweek{ledger.counted.length === 1 ? '' : 's'}.
          {#if squadOverlap != null}
            Your squads overlap <b>{Math.round(squadOverlap)}%</b>.
          {/if}
        </p>
        <div class="overflow-x-auto mt-2">
          <table class="data">
            <thead>
              <tr>
                <th class="!text-left">Player</th>
                <th class="!text-left">Swing</th>
                <th>Weeks</th>
              </tr>
            </thead>
            <tbody>
              {#each ledger.lines.slice(0, 12) as l}
                <tr class="cursor-pointer transition hover:bg-line/20" onclick={() => onpick(l.playerId)}>
                  <td class="!text-left">{pname(l.playerId)}</td>
                  <td class="!text-left font-bold tabular-nums {l.delta > 0 ? 'text-brand-light' : 'text-red'}">
                    {signed(l.delta)}
                  </td>
                  <td class="text-muted2 tabular-nums">{l.weeks}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        <p class="text-mini text-muted2 mt-2">
          The top three account for {Math.round(topShare)}% of everything that moved
          between you. Hits are in the gap but in nobody's row — they belong to no
          player.
          {#if ledger.dropped.length}
            GW{ledger.dropped.join(', GW')} did not reconcile and
            {ledger.dropped.length === 1 ? 'is' : 'are'} excluded.
          {/if}
          {#if unreadableGws.length}
            GW{unreadableGws.join(', GW')} could not be read at all.
          {/if}
        </p>
      {/if}
    {/if}
  </div>
</div>
