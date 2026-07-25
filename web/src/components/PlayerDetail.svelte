<script lang="ts">
  import type { Player } from '../lib/types'
  import { playerPhoto } from '../lib/img'
  import Crest from './Crest.svelte'
  import FixtureStrip from './FixtureStrip.svelte'

  let { player, onclose }: { player: Player | null; onclose: () => void } = $props()

  const parts = $derived(
    player
      ? [
          { label: 'Appearance', v: player.breakdown.appearance, c: 'bg-slate-400' },
          { label: 'Goals', v: player.breakdown.goals, c: 'bg-red' },
          { label: 'Assists', v: player.breakdown.assists, c: 'bg-yellow' },
          { label: 'Clean sheet', v: player.breakdown.clean_sheet, c: 'bg-accent' },
          { label: 'DEFCON', v: player.breakdown.defcon, c: 'bg-brand' },
          { label: 'Bonus', v: player.breakdown.bonus, c: 'bg-brand-light' },
        ].filter((p) => p.v > 0.001)
      : [],
  )
  const total = $derived(player?.next_gw_xp ?? 0)
  // Guard the stacked bar against divide-by-zero when xP≈0 (breakdown parts can
  // still be non-zero and mismatch the headline total slightly).
  const denom = $derived(Math.max(total, ...parts.map((p) => p.v), 0.001))
  const photo = $derived(player ? playerPhoto(player.code, '250x250') : '')
  let photoBroken = $state(false)
  $effect(() => {
    player // reset when the player changes
    photoBroken = false
  })
</script>

<svelte:window onkeydown={(e) => player && e.key === 'Escape' && onclose()} />

{#if player}
  <div
    class="fixed inset-0 z-50 flex items-end sm:items-center sm:justify-center"
    role="dialog"
    aria-modal="true"
    aria-label="{player.name} details"
  >
    <button class="absolute inset-0 bg-black/60" aria-label="close" onclick={onclose}></button>
    <div
      class="relative w-full sm:max-w-lg card rounded-t-2xl sm:rounded-2xl p-5 rise max-h-[88vh] overflow-y-auto"
    >
      <div class="w-10 h-1 rounded-full bg-line2 mx-auto mb-4 sm:hidden"></div>

      <div class="flex items-start gap-3">
        {#if photo && !photoBroken}
          <img
            src={photo}
            alt={player.name}
            onerror={() => (photoBroken = true)}
            class="w-16 h-16 rounded-lg object-cover object-top bg-bg3 border border-line"
          />
        {/if}
        <div class="flex-1">
          <div class="text-xl font-bold">{player.name}</div>
          <div class="text-sm text-muted flex items-center gap-1.5">
            <Crest code={player.team_code} short={player.team} size={16} />
            {player.pos} · {player.team} · £{player.price.toFixed(1)}m · {player.owned_by}% owned
          </div>
          <div class="mt-1 flex flex-wrap gap-1">
            <span class="badge badge-{player.xmins_badge.kind}">{player.xmins_badge.label} {player.xmins_badge.hint}</span>
            {#each player.tags as t}<span class="chip chip-{t.kind}">{t.label}</span>{/each}
          </div>
        </div>
        <div class="text-right">
          <div class="text-2xl font-black text-brand-light tabular-nums">{total.toFixed(2)}</div>
          <div class="text-[10px] uppercase text-muted">next-GW xP</div>
        </div>
      </div>

      <!-- WHY -->
      <div class="mt-3 rounded-lg bg-bg2 border border-line px-3 py-2 text-sm">
        <span class="text-brand-light font-semibold">Why: </span>{player.rationale}
      </div>

      {#if player.news}
        <div class="mt-2 text-xs chip-bad rounded-lg px-3 py-2">{player.news}</div>
      {/if}

      <!-- stacked contribution -->
      <div class="mt-4">
        <div class="text-xs font-bold uppercase text-muted mb-1">Where the points come from</div>
        <div class="flex h-4 rounded-full overflow-hidden bg-bg3">
          {#each parts as p}<div class={p.c} style="width: {(p.v / denom) * 100}%" title={p.label}></div>{/each}
        </div>
        <div class="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          {#each parts as p}
            <div class="flex items-center justify-between">
              <span class="flex items-center gap-2"><span class="w-2.5 h-2.5 rounded {p.c}"></span>{p.label}</span>
              <span class="tabular-nums text-muted">{p.v.toFixed(2)}</span>
            </div>
          {/each}
        </div>
      </div>

      <div class="mt-4 grid grid-cols-4 gap-2 text-center text-xs">
        <div class="card py-2"><div class="text-muted">Start</div><div class="font-bold">{Math.round(player.p_start * 100)}%</div></div>
        <div class="card py-2"><div class="text-muted">Conf.</div><div class="font-bold">{Math.round(player.confidence * 100)}%</div></div>
        <div class="card py-2"><div class="text-muted">6-GW</div><div class="font-bold text-accent-light">{player.xp_window.toFixed(0)}</div></div>
        <div class="card py-2"><div class="text-muted">Form</div><div class="font-bold">{player.form.toFixed(1)}</div></div>
      </div>

      <div class="mt-3 flex items-center justify-between">
        <div class="flex gap-4 text-xs text-muted">
          <span>xGI/90 <b class="text-text">{player.xgi90.toFixed(2)}</b></span>
          <span>DEFCON/90 <b class="text-text">{player.defcon90.toFixed(1)}</b></span>
        </div>
        <FixtureStrip fixtures={player.fixtures} />
      </div>
    </div>
  </div>
{/if}
