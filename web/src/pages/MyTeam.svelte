<script lang="ts">
  import type { Bundle } from '../lib/data'
  import type { Player, RecPlayer } from '../lib/types'
  import { fpl, type PicksResponse, type Pick } from '../lib/fpl'
  import { getEntryId } from '../lib/config'
  import Pitch from '../components/Pitch.svelte'
  import FixtureStrip from '../components/FixtureStrip.svelte'
  import Icon from '../components/Icon.svelte'

  let { bundle, onpick, ongoSettings }: { bundle: Bundle; onpick: (id: number) => void; ongoSettings: () => void } = $props()

  const byId = $derived(new Map(bundle.players.map((p) => [p.id, p])))
  let phase = $state<'idle' | 'loading' | 'ok' | 'error' | 'nosetup' | 'preseason'>('idle')
  let msg = $state('')
  let picks = $state<PicksResponse | null>(null)

  const gw = $derived(Number(bundle.meta.current_gw || 1))

  $effect(() => {
    const entry = getEntryId()
    if (!entry || !fpl.configured()) {
      phase = 'nosetup'
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
        // Pre-season the picks endpoint 404s (FPL keeps squads private until the
        // GW1 deadline). That's expected, not a setup error — don't blame the ID.
        if (!Number(bundle.meta.last_finished_gw)) {
          phase = 'preseason'
        } else {
          phase = 'error'
          msg = String(e?.message ?? e)
        }
      })
  })

  const deadlineStr = $derived(
    bundle.meta.deadline
      ? new Date(bundle.meta.deadline).toLocaleDateString(undefined, { day: 'numeric', month: 'long' })
      : 'the GW1 deadline',
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
  <div class="card p-6 text-center rise max-w-lg mx-auto">
    <div class="inline-flex items-center justify-center w-12 h-12 rounded-full bg-accent/12 text-accent-light mb-3"><Icon name="hourglass" size={22} /></div>
    <h2 class="font-bold text-lg">Your squad isn't public yet</h2>
    <p class="text-sm text-muted mt-2">
      FPL keeps everyone's team private until the <b>GW1 deadline ({deadlineStr})</b>. Your
      live XI will load here automatically once it passes.
    </p>
    <p class="text-sm text-muted mt-2">
      In the meantime, head to the <b>Planner</b> — it's pre-loaded with the model's
      optimal squad, and you can build and compare your own.
    </p>
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
          <h2 class="font-bold">Your XI · GW{gw}</h2>
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
                <div class="flex items-center gap-1 font-semibold">{p.name}
                  <span class="badge badge-{p.xmins_badge.kind}">{p.xmins_badge.label}</span>
                  {#if p.id === captainId}<span class="badge badge-good">C</span>{/if}
                </div>
                <div class="text-[10px] text-muted">{p.pos} · {p.team} · £{p.price.toFixed(1)}</div>
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
