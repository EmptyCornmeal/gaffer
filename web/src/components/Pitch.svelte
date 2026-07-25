<script lang="ts">
  import type { Pos, RecPlayer } from '../lib/types'
  import { playerPhoto } from '../lib/img'
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

<div
  class="relative rounded-xl overflow-hidden border border-line py-4 px-1"
  style="background: repeating-linear-gradient(0deg,#0c3a24 0,#0c3a24 38px,#0e4229 38px,#0e4229 76px);"
>
  <div class="absolute inset-x-6 top-1/2 h-px bg-white/10"></div>
  <div class="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-14 h-14 rounded-full border border-white/10"></div>

  <div class="relative flex flex-col gap-2.5">
    {#each rows as row}
      <div class="flex justify-center gap-2 sm:gap-5 flex-wrap">
        {#each row as p}
          <button class="w-[58px] sm:w-[72px] flex flex-col items-center gap-1 group" onclick={() => onpick(p)}>
            <div class="relative">
              <div class="w-11 h-11 rounded-full overflow-hidden bg-bg3 border border-white/25 shadow-lg group-active:scale-95 transition flex items-center justify-center">
                {#if p.code && !failed[p.id]}
                  <img src={playerPhoto(p.code)} alt={p.name} class="w-full h-full object-cover object-top" onerror={() => (failed = { ...failed, [p.id]: true })} />
                {:else}
                  <span class="text-[10px] font-bold text-accent-light">{p.team}</span>
                {/if}
              </div>
              <span class="absolute -bottom-1 -left-1"><Crest code={p.team_code} short={p.team} size={15} /></span>
              {#if p.id === captainId}
                <span class="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-brand text-[9px] font-black text-[#05210f] flex items-center justify-center">C</span>
              {:else if p.id === viceId}
                <span class="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-accent text-[9px] font-black text-white flex items-center justify-center">V</span>
              {/if}
            </div>
            <div class="text-[10px] leading-tight font-semibold truncate w-full text-center text-white/90">{p.name}</div>
            <div class="text-[10px] font-bold text-brand-light tabular-nums">{p.next_gw_xp.toFixed(1)}</div>
          </button>
        {/each}
      </div>
    {/each}
  </div>
</div>
