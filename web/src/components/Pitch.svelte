<script lang="ts">
  import type { Pos, RecPlayer } from '../lib/types'
  import { playerPhoto } from '../lib/img'
  import { formatMargin, marginBand } from '../lib/margins'
  import Crest from './Crest.svelte'

  let {
    starting,
    captainId = -1,
    viceId = -1,
    onpick = (_: RecPlayer) => {},
  }: {
    starting: RecPlayer[]
    captainId?: number
    viceId?: number
    onpick?: (p: RecPlayer) => void
  } = $props()

  const order: Pos[] = ['GKP', 'DEF', 'MID', 'FWD']
  const rows = $derived(order.map((pos) => starting.filter((p) => p.pos === pos)))
  let failed = $state<Record<number, boolean>>({})
</script>

<div class="relative rounded-2xl overflow-hidden border border-line py-5 px-2">
  <!-- turf: vertical gradient + faint mowing stripes -->
  <div
    class="absolute inset-0"
    style="background:
      linear-gradient(180deg,#0c2a1e 0%,#0a1f18 100%),
      repeating-linear-gradient(0deg, rgba(255,255,255,0.018) 0 44px, transparent 44px 88px);
      background-blend-mode: normal;"
  ></div>
  <!-- pitch markings -->
  <div class="absolute inset-3 rounded-xl border border-white/10 pointer-events-none"></div>
  <div class="absolute inset-x-6 top-1/2 h-px bg-white/10 pointer-events-none"></div>
  <div class="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-16 h-16 rounded-full border border-white/10 pointer-events-none"></div>
  <div class="absolute left-1/2 top-3 -translate-x-1/2 w-24 h-8 border border-t-0 border-white/10 rounded-b-lg pointer-events-none"></div>
  <div class="absolute left-1/2 bottom-3 -translate-x-1/2 w-24 h-8 border border-b-0 border-white/10 rounded-t-lg pointer-events-none"></div>

  <div class="relative flex flex-col gap-3">
    {#each rows as row}
      <div class="flex justify-center gap-1.5 sm:gap-4 flex-wrap">
        {#each row as p}
          {@const isCap = p.id === captainId}
          {@const isVice = p.id === viceId}
          {@const band = marginBand(p.margin)}
          <button
            class="group w-[62px] sm:w-[74px] flex flex-col items-center"
            onclick={() => onpick(p)}
          >
            <div class="relative">
              <div
                class="w-[52px] h-[52px] rounded-xl overflow-hidden bg-bg3 flex items-center justify-center
                       border transition-all duration-150 group-hover:-translate-y-0.5
                       {isCap ? 'border-brand ring-2 ring-brand/50' : 'border-white/20'}
                       group-hover:border-brand/60 shadow-lg group-hover:shadow-[0_0_0_1px_rgba(16,185,129,0.4)]"
              >
                {#if p.code && !failed[p.id]}
                  <img
                    src={playerPhoto(p.code)}
                    alt={p.name}
                    class="w-full h-full object-cover object-top"
                    onerror={() => (failed = { ...failed, [p.id]: true })}
                  />
                {:else}
                  <span class="text-[11px] font-bold text-accent-light">{p.team}</span>
                {/if}
              </div>
              <span class="absolute -bottom-1 -left-1"><Crest code={p.team_code} short={p.team} size={16} /></span>
              {#if isCap}
                <span class="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-brand text-[10px] font-black text-[#05210f] flex items-center justify-center shadow ring-2 ring-bg2">C</span>
              {:else if isVice}
                <span class="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-accent text-[9px] font-black text-white flex items-center justify-center shadow ring-2 ring-bg2">VC</span>
              {/if}
            </div>
            <!-- name plate -->
            <div class="mt-1.5 w-full rounded-md bg-bg2/80 backdrop-blur-sm px-1 py-0.5 text-center">
              <div class="text-[10px] leading-tight font-semibold truncate text-white/90">{p.name}</div>
            </div>
            <div class="mt-0.5 rounded-full bg-brand/15 px-1.5 text-[10px] font-bold text-brand-light tabular-nums leading-4">
              {p.next_gw_xp.toFixed(1)}
            </div>
            <!-- What this slot is actually worth, under what it is projected to
                 score. The two disagree often enough to be the point: a tile
                 showing 27.6 xP and 0.62 margin is a good player in a contested
                 position, and the pitch used to imply all eleven were equally
                 settled. Rendered only where a margin was measured — an absent
                 one shows nothing rather than a zero. -->
            {#if band}
              <div
                class="mt-0.5 rounded-full px-1.5 text-[9px] font-bold tabular-nums leading-4 {band.tone}"
                title="{band.label} — {band.hint}"
              >
                {formatMargin(p.margin)}
              </div>
            {/if}
          </button>
        {/each}
      </div>
    {/each}
  </div>
</div>
