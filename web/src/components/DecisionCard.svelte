<script lang="ts">
  /**
   * 4.1 / 4.5 -- the canonical decision card.
   *
   * Renders the object the MCP serves and the snapshot stored, so "what did
   * Gaffer advise?" has one answer with one digest. Everything here is read
   * from the card; nothing is recomputed, because a second code path is a
   * second chance to disagree.
   *
   * What it adds to the screen is the part the old composition dropped: the
   * two intervals side by side. An edge of +1.45 with a Monte-Carlo error of
   * ±0.3 and a realistic range of -6 to +10 is not a precise recommendation,
   * and the page said only the first half of that.
   */
  import { modelLink } from '../lib/evidence'
  import type { DecisionCard } from '../lib/weekly'

  let { card }: { card: DecisionCard | null | undefined } = $props()

  const num = (v: unknown, dp = 2): string =>
    typeof v === 'number' && Number.isFinite(v) ? v.toFixed(dp) : '—'
  const signed = (v: unknown, dp = 2): string =>
    typeof v === 'number' && Number.isFinite(v) ? `${v > 0 ? '+' : ''}${v.toFixed(dp)}` : '—'

  const margin = $derived(card?.margin)
  const hasMargin = $derived(!!margin?.available)
  const range = $derived(margin?.realistic_range ?? null)
  const changers = $derived(
    Array.isArray(card?.what_would_change_it) ? card.what_would_change_it : [],
  )
</script>

{#if card}
  <section class="card p-4" aria-labelledby="card-heading">
    <div class="flex items-baseline justify-between gap-2 flex-wrap">
      <h3 id="card-heading" class="font-bold text-sm">The margin, and what it is worth</h3>
      <span class="text-micro text-muted2 tabular-nums" title="The digest shared by this page, the MCP tool and the stored pre-deadline snapshot. If they ever disagreed, the artifact contract would fail.">
        card {card.card_version} · {card.content_hash}
      </span>
    </div>

    {#if hasMargin}
      <div class="mt-3 grid gap-3 sm:grid-cols-2">
        <!-- the edge, and how much of it is simulation noise -->
        <div class="rounded-lg bg-bg3 p-3">
          <div class="text-micro uppercase font-bold text-muted2">Edge over holding</div>
          <div class="text-xl font-black tabular-nums mt-0.5">{signed(margin?.value)}</div>
          {#if margin?.ci95}
            <div class="text-mini text-muted mt-1 tabular-nums">
              {num(margin.ci95[0])} to {num(margin.ci95[1])}
              <span class="text-muted2">· simulation error on the mean</span>
            </div>
          {/if}
          {#if typeof margin?.p_beats_hold === 'number'}
            <div class="text-mini text-muted mt-0.5">
              beats holding in {Math.round(margin.p_beats_hold * 100)}% of scenarios
            </div>
          {/if}
        </div>

        <!-- and what a week actually looks like -->
        <div class="rounded-lg bg-bg3 p-3">
          <div class="text-micro uppercase font-bold text-muted2">A realistic week</div>
          {#if range}
            <div class="text-xl font-black tabular-nums mt-0.5">
              {signed(range[0], 1)} to {signed(range[1], 1)}
            </div>
            <div class="text-mini text-muted mt-1">
              the spread of <b>football</b> outcomes, not simulation error — it does
              not shrink with more scenarios
            </div>
          {:else}
            <div class="text-sm text-muted mt-1">Not published for this decision.</div>
          {/if}
        </div>
      </div>
    {:else}
      <p class="text-sm text-muted mt-2">{margin?.reason ?? 'No margin was published.'}</p>
    {/if}

    {#if changers.length}
      <div class="mt-3">
        <div class="text-micro uppercase font-bold text-muted2 mb-1">What would change this</div>
        <ul class="text-sm space-y-1 list-disc pl-4 marker:text-muted2">
          {#each changers as c}<li>{c}</li>{/each}
        </ul>
      </div>
    {/if}

    {#if card.strength}
      <p class="text-mini text-muted2 mt-3">
        The bar a move must clear ({num(card.strength.min_actionable_points, 1)} points and
        {card.strength.min_actionable_probability
          ? `${Math.round(card.strength.min_actionable_probability * 100)}%`
          : '—'} of scenarios) is a policy floor, not a fitted parameter.
        <a class="underline decoration-dotted hover:text-brand-light" href={modelLink('decisions')}>
          How past decisions scored
        </a>
      </p>
    {/if}
  </section>
{/if}
