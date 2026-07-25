<script lang="ts">
  import { crest } from '../lib/img'
  let { code, short = '', size = 18 }: { code: number | null | undefined; short?: string; size?: number } = $props()
  let failed = $state(false)
  const url = $derived(crest(code))
</script>

{#if url && !failed}
  <img src={url} alt={short} width={size} height={size} style="width:{size}px;height:{size}px" class="object-contain inline-block" onerror={() => (failed = true)} />
{:else}
  <span class="inline-flex items-center justify-center rounded bg-bg3 text-[9px] font-bold text-muted" style="width:{size}px;height:{size}px">{short}</span>
{/if}
