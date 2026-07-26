<script lang="ts">
  import type { Bundle } from '../lib/data'
  import { mdLite } from '../lib/mdlite'
  import Icon from '../components/Icon.svelte'

  let { bundle }: { bundle: Bundle } = $props()
  const news = $derived(bundle.news)

  // Relative time reads faster in a feed than a full RSS timestamp.
  function relTime(pub: string): string {
    const t = Date.parse(pub)
    if (isNaN(t)) return ''
    const s = (Date.now() - t) / 1000
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`
    if (s < 86400) return `${Math.round(s / 3600)}h ago`
    return `${Math.round(s / 86400)}d ago`
  }
</script>

<div class="rise flex flex-col gap-4 max-w-3xl mx-auto">
  <div class="flex items-center justify-between">
    <h2 class="font-bold text-lg flex items-center gap-2"><Icon name="news" size={18} /> Transfer News</h2>
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
        <a href={it.link} target="_blank" rel="noopener noreferrer" class="group block px-4 py-3 hover:bg-card2 transition">
          <div class="flex items-center gap-2 mb-0.5">
            <span class="chip chip-info">{it.source}</span>
            {#if it.published}<span class="text-[10px] text-muted2" title={it.published.replace(/ \+\d{4}$/, '')}>{relTime(it.published) || it.published.replace(/ \+\d{4}$/, '')}</span>{/if}
          </div>
          <div class="font-semibold text-sm flex items-start gap-1 group-hover:text-brand-light transition-colors">
            <span class="min-w-0">{it.title}</span>
            <span class="text-muted2 opacity-0 group-hover:opacity-100 transition shrink-0 mt-0.5">↗</span>
          </div>
          {#if it.summary}<div class="text-xs text-muted mt-0.5 line-clamp-2">{it.summary}</div>{/if}
        </a>
      {/each}
    </div>
  {/if}
</div>
