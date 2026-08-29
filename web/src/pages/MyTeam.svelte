<script lang="ts">
  import type { Bundle } from '../lib/data'
  import type { Player, RecPlayer } from '../lib/types'
  import { fpl, type PicksResponse, type Pick } from '../lib/fpl'
  import { getEntryId } from '../lib/config'
  import Pitch from '../components/Pitch.svelte'
  import FixtureStrip from '../components/FixtureStrip.svelte'
  import Icon from '../components/Icon.svelte'

  let { bundle, onpick, ongoSettings, onnav }: { bundle: Bundle; onpick: (id: number) => void; ongoSettings: () => void; onnav: (r: string) => void } = $props()

  // A chip is something your squad has, not somewhere you go.
  let tab = $state<'squad' | 'chips'>('squad')
  const TABS = [
    { key: 'squad', label: 'Squad' },
    { key: 'chips', label: 'Chips' },
  ] as const

  const byId = $derived(new Map(bundle.players.map((p) => [p.id, p])))
  const entryId = getEntryId()
  let phase = $state<'idle' | 'loading' | 'ok' | 'error' | 'nosetup' | 'preseason'>('idle')
  let msg = $state('')
  let picks = $state<PicksResponse | null>(null)

  // The squad FPL will actually serve: the last event whose deadline has
  // passed. `current_gw` is the event being decided, and its picks 404 until
  // its own deadline — which is what sent this page into its pre-season
  // branch ninety minutes into GW1.
  const gw = $derived(Number(bundle.meta.squad_source_event || 0))
  // The card is headed by the gameweek the picks were READ for and filled with
  // the xP of the gameweek being PROJECTED. FPL exposes picks only for the
  // locked gameweek, so past a deadline these are legitimately different
  // numbers — and the header named neither, which made a GW2 heading sit on top
  // of GW3 projections with nothing on screen to say so.
  const projGw = $derived(Number(bundle.meta.projection_event || 0))

  $effect(() => {
    const entry = getEntryId()
    if (!entry || !fpl.configured()) {
      phase = 'nosetup'
      return
    }
    // No deadline has passed at all: genuinely pre-season, and there is no
    // gameweek to ask about. Asking anyway is what produced a 404 that then got
    // reported as a mystery.
    if (!gw) {
      phase = 'preseason'
      return
    }
    phase = 'loading'
    fpl
      .picks(entry, gw)
      .then((p) => {
        picks = p
        phase = 'ok'
      })
      .catch((e) => {
        // `gw` only has a value because a deadline passed, so a failure here
        // is FPL being unreachable — not "too early". The old test used
        // `last_finished_gw`, which stays null all through a gameweek that is
        // being played, so a live GW1 was reported as pre-season.
        phase = 'error'
        msg = String(e?.message ?? e)
      })
  })

  const deadlineStr = $derived(
    bundle.meta.deadline
      ? new Date(bundle.meta.deadline).toLocaleDateString(undefined, { day: 'numeric', month: 'long' })
      : 'the first deadline',
  )

  function toRec(p: Player): RecPlayer {
    return { id: p.id, code: p.code, team_code: p.team_code, name: p.name, team: p.team, pos: p.pos, price: p.price, next_gw_xp: p.next_gw_xp, confidence: p.confidence }
  }
  const squad = $derived(
    picks
      ? picks.picks
          .map((pk) => ({ pk, p: byId.get(pk.element) }))
          .filter((x): x is { pk: Pick; p: Player } => !!x.p)
      : [],
  )
  const starters = $derived(squad.filter((x) => x.pk.multiplier > 0).map((x) => toRec(x.p as Player)))
  const bench = $derived(squad.filter((x) => x.pk.multiplier === 0).map((x) => x.p as Player))
  const captainId = $derived(picks?.picks.find((p) => p.is_captain)?.element ?? -1)
  const viceId = $derived(picks?.picks.find((p) => p.is_vice_captain)?.element ?? -1)
  const suggestedCaptain = $derived([...starters].sort((a, b) => b.next_gw_xp - a.next_gw_xp)[0])
</script>

<div class="flex items-center gap-0.5 rounded-lg border border-line bg-bg2 p-0.5 w-fit mb-3">
  {#each TABS as t}
    <button
      onclick={() => (tab = t.key)}
      class="px-3 py-1 rounded-md text-xs font-bold transition {tab === t.key ? 'bg-brand text-[#05210f]' : 'text-muted hover:text-text'}"
    >{t.label}</button>
  {/each}
</div>

{#if tab === 'chips'}
  <!-- Lazy: this page is eagerly imported, and ChipsView is not why anyone
       opens it. Loading it with the tab keeps it out of the entry chunk. -->
  {#await import('../components/ChipsView.svelte')}
    <div class="flex justify-center py-16 text-muted" role="status" aria-live="polite">
      <div class="w-6 h-6 rounded-full border-2 border-line border-t-brand animate-spin motion-reduce:animate-none"></div>
    </div>
  {:then M}
    <M.default {bundle} {onnav} />
  {:catch}
    <p class="text-sm text-muted text-center py-16">That section failed to load. Reload the page.</p>
  {/await}
{:else}


{#if phase === 'nosetup'}
  <div class="card p-8 text-center rise max-w-lg mx-auto">
    <div class="inline-flex items-center justify-center w-12 h-12 rounded-full bg-brand/12 text-brand-light mb-3"><Icon name="shirt" size={22} /></div>
    <h2 class="font-bold text-lg">Connect your team</h2>
    <p class="text-sm text-muted mt-2">
      Add your <b>FPL Entry ID</b> in Settings and your live squad appears here —
      every player scored, badged, and explained.
    </p>
    <button class="btn mt-4" onclick={ongoSettings}>Open settings</button>
  </div>
{:else if phase === 'loading'}
  <div class="flex justify-center py-24 text-muted"><div class="w-8 h-8 rounded-full border-2 border-line border-t-brand animate-spin"></div></div>
{:else if phase === 'preseason'}
  <div class="rise max-w-xl mx-auto flex flex-col items-center text-center py-10">
    <div class="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-accent/12 text-accent-light mb-4"><Icon name="hourglass" size={26} /></div>
    <h2 class="font-black text-xl">Your squad isn't public yet</h2>
    <p class="text-sm text-muted mt-2 max-w-md">
      FPL keeps every squad private until the season's <b>first deadline ({deadlineStr})</b> — your
      live XI, per-player xP, badges and fixtures load here automatically once it passes.
    </p>
    <button class="btn mt-5" onclick={() => onnav('planner')}>Build a team in the Planner →</button>
    <button class="text-xs text-muted2 mt-3 hover:text-muted" onclick={ongoSettings}>Watching entry #{entryId} · change</button>
    <div class="mt-8 grid grid-cols-3 gap-3 w-full max-w-md">
      {#each [{ i: 'shirt', t: 'Your XI on the pitch' }, { i: 'zap', t: 'Per-player xP & badges' }, { i: 'calendar', t: 'Next-5 fixtures' }] as f}
        <div class="card p-3 text-left"><Icon name={f.i} size={16} class="text-brand-light" /><div class="text-xs text-muted mt-1.5">{f.t}</div></div>
      {/each}
    </div>
    <div class="text-mini text-muted2 mt-6">In-season, this page becomes your live team, scored and explained.</div>
  </div>
{:else if phase === 'error'}
  <div class="card p-6 text-center rise max-w-lg mx-auto">
    <h2 class="font-bold">Couldn't load your team</h2>
    <p class="text-sm text-muted mt-2">We couldn't reach your live picks just now. Double-check your Entry ID in Settings.</p>
    <p class="text-xs text-muted2 mt-1">{msg}</p>
  </div>
{:else if phase === 'ok' && picks}
  <div class="flex flex-col gap-4 rise">
    <div class="grid lg:grid-cols-3 gap-4">
      <div class="lg:col-span-2 card p-3">
        <div class="flex items-center justify-between mb-2 px-1">
          <div>
            <h2 class="font-bold">Your XI · GW{gw}</h2>
            {#if projGw && gw && projGw !== gw}
              <p class="text-xs text-muted2 mt-0.5">
                {bundle.meta.squad_status_reason
                  ?? `picks read for GW${gw} while projecting GW${projGw}`}
              </p>
            {/if}
          </div>
          <span class="text-xs text-muted">Bank £{((picks.entry_history?.bank ?? 0) / 10).toFixed(1)}m · TV £{((picks.entry_history?.value ?? 0) / 10).toFixed(1)}m</span>
        </div>
        <Pitch starting={starters} {captainId} {viceId} onpick={(p) => onpick(p.id)} />
      </div>
      <div class="card p-3">
        <h2 class="font-bold mb-1">Captain pick</h2>
        {#if suggestedCaptain}
          <div class="text-sm">
            Model favours <b class="text-brand-light">{suggestedCaptain.name}</b>
            ({suggestedCaptain.next_gw_xp.toFixed(1)} xP)
            {#if suggestedCaptain.id !== captainId}<span class="chip chip-warn ml-1">you have {byId.get(captainId)?.name ?? '—'}</span>{/if}
          </div>
        {/if}
        <h2 class="font-bold mt-4 mb-1">Bench</h2>
        <div class="space-y-1">
          {#each bench as p}
            <button onclick={() => onpick(p.id)} class="w-full flex justify-between text-sm py-1 hover:opacity-80">
              <span>{p.name} <span class="text-muted">{p.pos}</span></span>
              <span class="text-brand-light font-bold">{p.next_gw_xp.toFixed(1)}</span>
            </button>
          {/each}
        </div>
      </div>
    </div>

    <!-- squad table with WHY -->
    <div class="card overflow-x-auto">
      <table class="data">
        <thead><tr><th>Player</th><th class="!text-center">Fixtures</th><th>xP</th><th>6GW</th><th class="!text-left">Why</th></tr></thead>
        <tbody>
          {#each squad.map((x) => x.p as Player).sort((a, b) => b.next_gw_xp - a.next_gw_xp) as p (p.id)}
            <tr onclick={() => onpick(p.id)}>
              <td>
                <!-- `<tr onclick>` is not focusable and not announced as a
                     control. The name becomes a real button on the same `onpick`
                     the bench list above already uses; the row click is left
                     alone as a pointer convenience. -->
                <button class="w-full text-left" onclick={(e) => { e.stopPropagation(); onpick(p.id) }}>
                  <span class="flex items-center gap-1 font-semibold">{p.name}
                    <span class="badge badge-{p.xmins_badge.kind}">{p.xmins_badge.label}</span>
                    {#if p.id === captainId}<span class="badge badge-good">C</span>{/if}
                  </span>
                  <span class="block text-micro text-muted">{p.pos} · {p.team} · £{p.price.toFixed(1)}</span>
                </button>
              </td>
              <td><div class="flex justify-center"><FixtureStrip fixtures={p.fixtures} max={4} /></div></td>
              <td class="font-bold text-brand-light">{p.next_gw_xp.toFixed(1)}</td>
              <td class="text-accent-light">{p.xp_window.toFixed(0)}</td>
              <td class="!text-left text-xs text-muted max-w-md">{p.rationale}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>
{/if}
{/if}
