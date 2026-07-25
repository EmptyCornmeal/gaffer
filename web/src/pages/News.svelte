<script lang="ts">
  import type { Bundle } from '../lib/data'
  import { mdLite } from '../lib/mdlite'

  let { bundle }: { bundle: Bundle } = $props()
  const news = $derived(bundle.news)
</script>

<div class="rise flex flex-col gap-4">
  <div class="flex items-center justify-between">
    <h2 class="font-bold text-lg">📰 Transfer News</h2>
    {#if news}<span class="text-xs text-muted2">{news.count} stories · {news.source.startsWith('ai') ? 'AI digest' : 'headline feed'}</span>{/if}
  </div>

  {#if !news || !news.items.length}
    <div class="card p-6 text-center text-muted text-sm">No transfer stories fetched right now — check back after the next refresh.</div>
  {:else}
    <!-- FPL-angle digest -->
    <div class="card p-4 border-brand/40 bg-brand/8">
      <div class="text-xs font-bold uppercase tracking-wider text-brand-light mb-1">The FPL angle</div>
      <div class="verdict text-[15px] leading-relaxed text-text">{@html mdLite(news.digest_md)}</div>
    </div>

    <!-- headline feed -->
    <div class="card divide-y divide-line/60">
      {#each news.items as it}
        <a href={it.link} target="_blank" rel="noreferrer" class="block px-4 py-3 hover:bg-card2 transition">
          <div class="flex items-center gap-2 mb-0.5">
            <span class="chip chip-info">{it.source}</span>
            {#if it.published}<span class="text-[10px] text-muted2">{it.published.replace(/ \+\d{4}$/, '')}</span>{/if}
          </div>
          <div class="font-semibold text-sm">{it.title}</div>
          {#if it.summary}<div class="text-xs text-muted mt-0.5 line-clamp-2">{it.summary}</div>{/if}
        </a>
      {/each}
    </div>
  {/if}
</div>
