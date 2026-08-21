<script lang="ts">
  import type { Bundle } from '../lib/data'
  import type { NewsItem } from '../lib/types'
  import { mdLite } from '../lib/mdlite'
  import Icon from './Icon.svelte'

  let { bundle }: { bundle: Bundle } = $props()
  const news = $derived(bundle.news)
  const claims = $derived(news?.claims ?? [])
  // Resolve a claim's cited ids back to the items that were actually fetched.
  // The claim never carries a URL, so a generated link is not representable.
  const byId = $derived(new Map((news?.items ?? []).map((i) => [i.id, i])))
  const sourcesFor = (ids: string[]): NewsItem[] =>
    ids.map((i) => byId.get(i)).filter((i): i is NewsItem => i !== undefined)

  // `confirmed` pointed at `chip-ok`, which is not a class in app.css — so the
  // firmest claims were the only ones rendering with no chip colour at all.
  const CERTAINTY: Record<string, string> = {
    confirmed: 'chip-good', reported: 'chip-info', rumoured: 'chip-warn',
  }

  // The template digest emits one claim per headline, verbatim, so "The FPL
  // angle" reprinted the feed sitting directly beneath it — the same sentences
  // twice, the second copy carrying a source, a timestamp and a summary the
  // first lacked. A claim keeps its own line only when it says something its
  // sources' headlines do not. The rest survive where they are actually useful,
  // as a marker on the story itself, because that is the card's real signal:
  // which of the fetched stories bear on FPL, and how firm each one is.
  const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim()
  const additive = $derived(
    claims.filter(
      (c) => !sourcesFor(c.source_item_ids).some((s) => norm(s.title) === norm(c.text)),
    ),
  )
  const FIRMNESS: Record<string, number> = { rumoured: 1, reported: 2, confirmed: 3 }
  // item id -> the firmest certainty any claim asserts about it.
  const angle = $derived.by(() => {
    const m = new Map<string, string>()
    for (const c of claims) {
      for (const id of c.source_item_ids) {
        const cur = m.get(id)
        if (!cur || (FIRMNESS[c.certainty] ?? 0) > (FIRMNESS[cur] ?? 0)) m.set(id, c.certainty)
      }
    }
    return m
  })
  // One line per code in `grounding.ALL_FALLBACK_REASONS`. The reason arrives as
  // `code` or `code:detail`, and an unmapped code used to fall through to the raw
  // enum: the live page read "headline feed (narration_disabled)". A missing
  // mapping is now a vaguer sentence, never a symbol from the pipeline.
  const FALLBACK: Record<string, string> = {
    no_credentials: 'no AI key configured',
    narration_disabled: 'AI narration is switched off',
    no_source_items: 'no stories fetched',
    provider_error: 'the AI call failed',
    empty_output: 'the AI returned nothing',
    malformed_output: 'the AI output could not be parsed',
    grounding_rejected: 'no generated claim could be traced to a source',
  }
  const fallbackText = (r: string | null) =>
    r ? (FALLBACK[r.split(':')[0]] ?? 'the AI digest was not produced') : ''
  // A template run always carries a reason, but an absent one must not render as
  // an empty pair of brackets.
  const feedLabel = (r: string | null) => {
    const why = fallbackText(r)
    return why ? `headline feed (${why})` : 'headline feed'
  }

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
    {#if news}
      <span class="text-xs text-muted2">
        {news.count} stories ·
        {news.source === 'ai' ? 'AI digest' : feedLabel(news.fallback_reason)}
      </span>
    {/if}
  </div>

  {#if !news || !news.items.length}
    <div class="card p-6 text-center text-muted text-sm">No transfer stories fetched right now — check back after the next refresh.</div>
  {:else}
    <!-- FPL-angle claims, each beside the headlines that support it -->
    {#if additive.length}
      <div class="card p-4 border-brand/40 bg-brand/8">
        <div class="text-xs font-bold uppercase tracking-wider text-brand-light mb-2">
          The FPL angle
        </div>
        <ul class="space-y-3">
          {#each additive as c}
            <li>
              <div class="flex flex-wrap items-baseline gap-2">
                <span class="text-[15px] leading-relaxed">{c.text}</span>
                <span class="chip {CERTAINTY[c.certainty] ?? 'chip-info'} text-micro">
                  {c.certainty}
                </span>
              </div>
              <div class="flex flex-wrap gap-2 mt-1">
                {#each sourcesFor(c.source_item_ids) as src}
                  <a
                    href={src.link}
                    target="_blank"
                    rel="noopener noreferrer"
                    class="text-mini text-muted2 underline decoration-dotted hover:text-brand-light"
                    title={src.title}
                  >{src.source} ↗</a>
                {/each}
              </div>
            </li>
          {/each}
        </ul>
        {#if news.source === 'ai'}
          <p class="text-micro text-muted2 mt-3">
            Generated from the headlines below. Every line names its sources, and
            any claim that could not be traced to one was dropped.
          </p>
        {/if}
      </div>
    {:else if angle.size}
      <!-- Every claim just restated a headline, so there is no card to show. The
           part the feed did not already carry — which stories were judged
           FPL-relevant — becomes this legend plus the marker on each story. -->
      <p class="text-mini text-muted2 -mb-1">
        <span class="font-bold text-brand-light">{angle.size} of {news.items.length}</span>
        stories carry an FPL angle, marked below. This digest restated each one
        word for word, so the headline is printed once with how firm it is.
      </p>
    {:else if !claims.length}
      <div class="card p-4 border-brand/40 bg-brand/8">
        <div class="text-xs font-bold uppercase tracking-wider text-brand-light mb-1">The FPL angle</div>
        <div class="verdict text-[15px] leading-relaxed text-text">{@html mdLite(news.digest_md)}</div>
      </div>
    {/if}

    <!-- headline feed -->
    <div class="card divide-y divide-line/60">
      {#each news.items as it}
        {@const cert = angle.get(it.id)}
        <a
          href={it.link}
          target="_blank"
          rel="noopener noreferrer"
          class="group block px-4 py-3 hover:bg-card2 transition border-l-2 {cert ? 'border-l-brand/70' : 'border-l-transparent'}"
        >
          <div class="flex items-center gap-2 mb-0.5 flex-wrap">
            <span class="chip chip-info">{it.source}</span>
            {#if cert}<span class="chip {CERTAINTY[cert] ?? 'chip-info'}">FPL angle · {cert}</span>{/if}
            {#if it.published}<span class="text-micro text-muted2" title={it.published.replace(/ \+\d{4}$/, '')}>{relTime(it.published) || it.published.replace(/ \+\d{4}$/, '')}</span>{/if}
          </div>
          <div class="font-semibold text-sm flex items-start gap-1 group-hover:text-brand-light transition-colors">
            <span class="min-w-0">{it.title}</span>
            <span class="text-muted2 opacity-0 group-hover:opacity-100 transition shrink-0 mt-0.5">↗</span>
          </div>
          {#if it.summary}<div class="text-xs text-muted mt-0.5 line-clamp-2">{it.summary}</div>{/if}
        </a>
      {/each}
    </div>

    {#if news.quarantined?.length}
      <p class="text-micro text-muted2">
        {news.quarantined.length} feed item{news.quarantined.length === 1 ? '' : 's'}
        withheld: the text was shaped like an instruction rather than a headline.
      </p>
    {/if}
  {/if}
</div>
