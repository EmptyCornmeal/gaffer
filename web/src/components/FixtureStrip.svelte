<script lang="ts">
  import type { TeamFixture } from '../lib/types'
  let { fixtures, max = 5 }: { fixtures: TeamFixture[]; max?: number } = $props()
</script>

<div class="flex gap-1">
  {#each fixtures.slice(0, max) as f}
    <div
      class="fdr-{f.difficulty} rounded px-1.5 py-0.5 text-center min-w-[34px]"
      title="GW{f.gw} · {f.home ? 'Home' : 'Away'} · difficulty {f.difficulty}"
    >
      <div class="text-[10px] font-bold leading-none">{f.opp}</div>
      <div class="text-[8px] opacity-80 leading-none">{f.home ? 'H' : 'A'}</div>
    </div>
  {/each}
  {#if !fixtures.length}<span class="text-xs text-muted2">—</span>{/if}
</div>
