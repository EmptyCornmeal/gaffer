<script lang="ts">
  import type { TeamFixture } from '../lib/types'
  let { fixtures, max = 5 }: { fixtures: TeamFixture[]; max?: number } = $props()
</script>

<div class="flex gap-1">
  {#each fixtures.slice(0, max) as f}
    <div
      class="fdr-{f.difficulty} rounded px-1 py-0.5 text-center min-w-[34px]"
      title="GW{f.gw} · {f.home ? 'Home' : 'Away'} · difficulty {f.difficulty}"
    >
      <div class="text-[10px] font-bold leading-none">{f.opp}</div>
      <!-- Difficulty was hue-only, with the numeral parked in `title` — which a
           touch device never shows. The Fixtures grid prints it in the cell
           corner; at 34px wide there is no room for an overlay, so venue and
           number take one bottom corner each instead of stacking. -->
      <div class="flex items-baseline justify-between gap-1 leading-none mt-0.5">
        <span class="text-[8px] opacity-90">{f.home ? 'H' : 'A'}</span>
        <span class="text-[9px] font-black">{f.difficulty}</span>
      </div>
    </div>
  {/each}
  {#if !fixtures.length}<span class="text-xs text-muted2">—</span>{/if}
</div>
