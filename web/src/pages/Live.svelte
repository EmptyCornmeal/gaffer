<script lang="ts">
  import type { Bundle } from '../lib/data'
  import { fetchLive, type LiveSourceName } from '../lib/live/source'
  import {
    parseLive, FIXTURE_STATE_LABELS, LIVE_UNAVAILABLE_LABELS, signed,
    type LiveState,
  } from '../lib/weekly'
  import { Poller, freshnessLabel, isStale, dataTimestamp } from '../lib/refresh'
  import Icon from '../components/Icon.svelte'

  let { bundle, onpick }: { bundle: Bundle | null; onpick: (id: number) => void } =
    $props()

  // Scored in the browser from the live endpoints, once a minute. `live.json` is
  // published three times a day, so polling it could never move during a match;
  // it remains the fallback when the proxy cannot answer, and the source is
  // always stated rather than assumed.
  let raw = $state<unknown>(null)
  let source = $state<LiveSourceName>('proxy')
  let fallbackReason = $state<string | null>(null)
  let incomplete = $state<string | null>(null)
  let status = $state<'idle' | 'loading' | 'ok' | 'stale' | 'error'>('idle')
  let lastSuccess = $state<number | null>(null)
  let lastError = $state<string | null>(null)
  let tick = $state(Date.now())

  const parsed = $derived(parseLive(raw))
  const s = $derived<LiveState | null>(parsed.kind === 'ok' ? parsed.data : null)
  const anyLive = $derived(
    !!s?.fixtures?.some((f) => f.state === 'live' || f.state === 'half_time'),
  )
  // Age of the DATA, not of the request that carried it. On the artifact
  // fallback those differ by hours.
  const dataAt = $derived(dataTimestamp(s?.as_of, lastSuccess))
  const stale = $derived(isStale(dataAt, tick))

  // The one number worth interrupting a screen reader for. `aria-live` used to
  // sit on the "Updated Ns ago" clock, which re-renders every fifteen seconds
  // and says nothing anybody needs, while the score itself moved in silence —
  // exactly backwards. Svelte writes a text node only when the string it holds
  // actually differs, so a poll that moves no points announces nothing.
  const announcement = $derived(
    s?.available && s.squad ? `Live total ${s.squad.current} points` : '',
  )

  $effect(() => {
    const poller = new Poller<unknown>(
      // Read `bundle` at call time, not setup time, so arriving data does not
      // tear down and rebuild the poller.
      () => fetchLive(
        // The gameweek being PLAYED, which is not `current_gw`. The instant a
        // deadline passes those diverge: decisions move to the next event while
        // this one's matches are still to be played. Polling `current_gw` asked
        // FPL for a gameweek that had not happened, got nothing back, and left
        // this page spinning on "Reading the live scores..." through a live match.
        Number(bundle?.meta?.squad_source_event ?? 0)
          || Number(bundle?.meta?.current_gw ?? 0)
          || 0,
        bundle?.players ?? [],
      ).then((r) => {
        source = r.source
        fallbackReason = r.fallbackReason
        incomplete = r.incomplete
        return r.state
      }),
      (st) => {
        if (st.data != null) raw = st.data
        status = st.status
        lastSuccess = st.lastSuccess
        lastError = st.lastError
      },
      {},
      // Stop polling once every fixture is done: there is nothing left to move.
      () => !(s?.fixture_summary?.all_finished && s?.fixture_summary?.bonus_final),
    )
    poller.start()
    const onVis = () => { if (!document.hidden) poller.wake() }
    document.addEventListener('visibilitychange', onVis)
    const t = setInterval(() => (tick = Date.now()), 15_000)
    return () => {
      poller.stop()
      document.removeEventListener('visibilitychange', onVis)
      clearInterval(t)
    }
  })

  function stateTone(st: string) {
    if (st === 'live' || st === 'half_time') return 'chip-good'
    if (st === 'awaiting_bonus') return 'chip-warn'
    if (st === 'postponed' || st === 'abandoned') return 'chip-bad'
    return 'chip-info'
  }
</script>

<div class="rise flex flex-col gap-4 w-full">
  <!-- Announced, never shown. It sits outside the branch chain below so the
       region exists before the first score does — a live region created in the
       same frame as its content is not reliably announced. `.sr-only` is this
       app's visually-hidden class (app.css), already used by the skip link and
       by the scoreboard's own heading. -->
  <p class="sr-only" role="status">{announcement}</p>

  <div class="flex items-start justify-between gap-2 flex-wrap">
    <div>
      <h2 class="font-bold text-lg flex items-center gap-2">
        <Icon name="flame" size={18} /> Live
        {#if anyLive}<span class="chip chip-good">in play</span>{/if}
      </h2>
      <p class="text-sm text-muted">
        Confirmed, provisional and predicted points, kept apart. Updates itself
        every minute while a match is on.
      </p>
    </div>
    <div class="text-right">
      <!-- `freshnessLabel` returns a whole clause, not a duration: with nothing
           fetched yet it is "never updated", and the "Updated" prefix turned that
           into "Updated never updated". Only a real timestamp takes the prefix.
           Deliberately NOT a live region: this text changes on a 15s cosmetic
           tick, so announcing it buries the score. See the status line above. -->
      <div class="text-mini text-muted2">
        {dataAt == null ? 'Not updated yet' : `Updated ${freshnessLabel(dataAt, tick)}`}
      </div>
      {#if source === 'artifact'}
        <span class="chip chip-warn" title={fallbackReason ?? ''}>
          published snapshot
        </span>
      {/if}
      {#if stale && dataAt != null}
        <span class="chip chip-warn">stale</span>
      {/if}
      {#if status === 'loading'}<span class="chip chip-info">refreshing…</span>{/if}
    </div>
  </div>

  {#if lastError && s}
    <div class="text-xs chip-warn rounded-lg px-3 py-2">
      Last refresh failed ({lastError}). Showing the last good state.
    </div>
  {/if}

  {#if incomplete && s}
    <!-- Partial data has to name itself. A rivals table four managers short is
         indistinguishable from a smaller league, and silently wrong is a worse
         failure than visibly incomplete. -->
    <div class="text-xs chip-warn rounded-lg px-3 py-2">
      Couldn't read {incomplete}. Everything else here is live; the rest is
      retried within a few minutes.
    </div>
  {/if}

  {#if parsed.kind === 'missing' && (status === 'idle' || status === 'loading')}
    <!-- Every branch below describes data we already have. Until the first fetch
         settles there is none, and saying nothing rendered the page as an empty
         rectangle for however long the proxy took to answer. -->
    <div class="flex flex-col items-center gap-3 py-24 text-muted">
      <div class="w-8 h-8 rounded-full border-2 border-line border-t-brand animate-spin"></div>
      <p class="text-sm">Reading the live scores…</p>
    </div>
  {:else if parsed.kind === 'missing'}
    <div class="card p-6 text-center">
      <div class="inline-flex items-center justify-center w-12 h-12 rounded-full bg-accent/12 text-accent-light mb-3">
        <Icon name="hourglass" size={22} />
      </div>
      <h3 class="font-bold text-lg">No live data</h3>
      <p class="text-sm text-muted mt-2">
        {#if fallbackReason}
          The live proxy could not answer ({fallbackReason}) and no published
          snapshot exists yet.
        {:else}
          Nothing has been published for this gameweek yet.
        {/if}
      </p>
      <p class="text-sm text-muted mt-2">
        This page fills itself in from the first whistle of the gameweek.
      </p>
    </div>
  {:else if parsed.kind === 'unsupported' || parsed.kind === 'malformed'}
    <div class="card p-6">
      <h3 class="font-bold text-red">This build can't render that live state</h3>
      <p class="text-sm text-muted mt-2">{parsed.detail}</p>
    </div>
  {:else if s && !s.available}
    <div class="card p-6 text-center">
      <div class="inline-flex items-center justify-center w-12 h-12 rounded-full bg-accent/12 text-accent-light mb-3">
        <Icon name="hourglass" size={22} />
      </div>
      <h3 class="font-bold text-lg">Nothing to score yet</h3>
      <p class="text-sm text-muted mt-2">
        {LIVE_UNAVAILABLE_LABELS[s.unavailable_reason ?? ''] ?? s.note ?? ''}
      </p>
      <p class="text-sm text-muted mt-2">
        Your XI, the league swing and every player's points appear here once the
        first match kicks off.
      </p>
      {#if s.fixture_summary?.total}
        <p class="text-mini text-muted2 mt-2">
          {s.fixture_summary.total}
          fixture{s.fixture_summary.total === 1 ? '' : 's'} in GW{s.gameweek}.
        </p>
      {/if}
    </div>
  {:else if s?.squad}
    <!-- ── the three kinds of points, never merged ─────────────────── -->
    <section class="card p-4" aria-labelledby="score">
      <h3 id="score" class="sr-only">Your live score</h3>
      <div class="text-center">
        <div class="text-5xl font-black tabular-nums">{s.squad.current}</div>
        <div class="text-sm text-muted">
          projected {s.squad.projected} · {s.squad.players_yet_to_play} yet to play
        </div>
      </div>
      <div class="grid grid-cols-3 gap-2 mt-3 text-center">
        <div class="rounded-lg bg-bg3 p-2">
          <div class="text-micro uppercase text-muted2 font-bold">Confirmed</div>
          <div class="text-lg font-black tabular-nums">{s.squad.confirmed}</div>
        </div>
        <div class="rounded-lg bg-bg3 p-2 ring-1 ring-yellow/40">
          <div class="text-micro uppercase text-yellow font-bold">Provisional</div>
          <div class="text-lg font-black tabular-nums">+{s.squad.provisional_bonus}</div>
        </div>
        <div class="rounded-lg bg-bg3 p-2">
          <div class="text-micro uppercase text-muted2 font-bold">Predicted</div>
          <div class="text-lg font-black tabular-nums">+{s.squad.predicted_remaining}</div>
        </div>
      </div>
      <p class="text-mini text-muted2 mt-2">{s.separation?.note}</p>
    </section>

    <!-- ── autosubs ───────────────────────────────────────────────── -->
    {#if s.squad.autosubs.subs_in.length || s.squad.autosubs.captain_source !== 'captain' || s.squad.autosubs.notes.length}
      <section class="card p-3">
        <div class="flex items-center justify-between">
          <h3 class="font-bold text-sm">Substitutions</h3>
          <span class="chip {s.squad.autosubs.provisional ? 'chip-warn' : 'chip-good'}">
            {s.squad.autosubs.provisional ? 'projected' : 'final'}
          </span>
        </div>
        {#if s.squad.autosubs.subs_in.length}
          <p class="text-sm mt-1">
            {s.squad.autosubs.subs_in.length} substitution(s) applied.
          </p>
        {/if}
        {#if s.squad.autosubs.captain_source === 'vice'}
          <p class="text-sm text-yellow mt-1">Armband passed to your vice-captain.</p>
        {:else if s.squad.autosubs.captain_source === 'none'}
          <p class="text-sm text-red mt-1">
            Captain and vice both blanked — nobody is multiplied.
          </p>
        {/if}
        <ul class="text-mini text-muted2 mt-1 list-disc pl-4">
          {#each s.squad.autosubs.notes as n}<li>{n}</li>{/each}
        </ul>
      </section>
    {/if}

    <!-- ── the swing ──────────────────────────────────────────────── -->
    {#if s.largest_swing}
      <section class="card p-3">
        <h3 class="font-bold text-sm">Biggest league swing</h3>
        <p class="text-sm mt-1">
          <b>{s.largest_swing.name}</b>
          <span class={s.largest_swing.swing > 0 ? 'text-brand-light' : 'text-red'}>
            {signed(s.largest_swing.swing, 0)}
          </span>
          — {s.largest_swing.note}.
        </p>
      </section>
    {/if}

    <!-- ── rivals ─────────────────────────────────────────────────── -->
    {#if s.rivals?.length}
      <section class="card overflow-x-auto">
        <table class="data">
          <thead>
            <tr><th>#</th><th class="!text-left">Manager</th><th>GW</th><th>Total</th><th>Left</th></tr>
          </thead>
          <tbody>
            {#each s.rivals as r (r.entry_id)}
              <tr class={r.you ? 'bg-brand/10' : ''}>
                <td>{r.provisional_position}</td>
                <td class="!text-left">{r.name}{#if r.you}<span class="chip chip-good ml-1">you</span>{/if}</td>
                <td class="tabular-nums font-semibold">{r.gw_points}</td>
                <td class="tabular-nums">{r.current}</td>
                <td class="tabular-nums text-muted2">{r.yet_to_play}</td>
              </tr>
            {/each}
          </tbody>
        </table>
        <p class="text-mini text-muted2 p-2">
          Positions are provisional: they include bonus that is not yet confirmed.
        </p>
      </section>
    {/if}

    <!-- ── players ────────────────────────────────────────────────── -->
    {#if s.players?.length}
      <section class="card overflow-x-auto">
        <table class="data">
          <thead>
            <tr>
              <th class="!text-left">Player</th><th>Min</th><th>Conf</th>
              <th>Prov</th><th>Pred</th>
            </tr>
          </thead>
          <tbody>
            {#each s.players as p (p.id)}
              <tr class={p.in_xi ? '' : 'opacity-60'}>
                <td class="!text-left">
                  <button class="min-h-11 text-left" onclick={() => onpick(p.id)}>
                    {p.name}{#if p.is_captain}<span class="chip chip-good ml-1">C</span>{/if}
                    {#if p.yet_to_play}<span class="chip chip-info ml-1">to play</span>{/if}
                  </button>
                </td>
                <td class="tabular-nums text-muted2">{p.minutes}</td>
                <td class="tabular-nums font-semibold">{p.confirmed}</td>
                <td class="tabular-nums text-yellow">{p.provisional ? `+${p.provisional}` : '—'}</td>
                <td class="tabular-nums text-muted2">{p.predicted ? `+${p.predicted}` : '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
    {/if}
  {:else}
    <!-- Unreachable today: a null squad becomes `no_squad` upstream, and every
         other parse outcome has a branch of its own above. Kept anyway, because
         this chain and the shape it switches on are maintained in separate
         files. If they ever drift, this is the difference between a page that
         looks wrong and a page that is blank, and blank in the middle of a
         gameweek is the worst thing this product can do. -->
    <div class="card p-6">
      <h3 class="font-bold text-lg">Live data arrived without a squad</h3>
      <p class="text-sm text-muted mt-2">
        The gameweek reports itself as scoreable and then carried nothing to
        score, which should not be possible. Read this as a bug rather than as a
        result. The page keeps refreshing every minute.
      </p>
    </div>
  {/if}

  <!-- ── fixtures ─────────────────────────────────────────────────── -->
  {#if s?.fixtures?.length}
    <section class="card p-3">
      <h3 class="font-bold text-sm mb-2">Fixtures</h3>
      <div class="flex flex-wrap gap-1.5">
        {#each s.fixtures as f (f.id)}
          <span class="chip {stateTone(f.state)}">
            {FIXTURE_STATE_LABELS[f.state]}{#if f.state === 'live'} {f.minutes}'{/if}
          </span>
        {/each}
      </div>
    </section>
  {/if}
</div>
