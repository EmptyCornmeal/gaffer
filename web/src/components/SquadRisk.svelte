<script lang="ts">
  /**
   * Structural risk: a squad made of good picks that is badly built.
   *
   * Rendered as a list of dated problems with the transfer that fixes each,
   * because that is the form a warning has to take to be actionable. A
   * concentration figure with no "you can fix this by rolling one transfer" is
   * trivia.
   *
   * Quiet when there is nothing to say. A warning panel that always warns is a
   * warning panel nobody reads, so `clear` renders one line and stops.
   */
  import type { SquadRisk } from '../lib/weekly'

  let { risk }: { risk: SquadRisk | null | undefined } = $props()

  const warnings = $derived(risk?.warnings ?? [])
  const KIND: Record<string, string> = {
    fixture_concentration: 'Concentration',
    dead_bench: 'Dead bench',
  }
  const when = (n: number) =>
    n === 0 ? 'this week' : n === 1 ? 'next week' : `in ${n} gameweeks`
</script>

{#if risk?.available}
  <section class="card p-4" aria-labelledby="risk-heading">
    <div class="flex items-baseline justify-between gap-2 flex-wrap">
      <h3 id="risk-heading" class="font-bold text-sm">How the squad is built</h3>
      <span class="text-micro text-muted2">next {risk.horizon} gameweeks</span>
    </div>

    {#if risk.clear}
      <p class="text-sm text-muted mt-2">
        Nothing structural to fix. The squad is spread across the fixtures and
        every bench slot can score.
      </p>
    {:else}
      <ul class="mt-3 space-y-2.5">
        {#each warnings as w}
          <li class="flex gap-2.5">
            <span class="chip {w.kind === 'dead_bench' ? 'chip-bad' : 'chip-warn'} shrink-0">
              {KIND[w.kind] ?? w.kind}
            </span>
            <span class="min-w-0 text-sm">
              <b class="text-text">GW{w.gameweek}</b>
              <span class="text-muted2">({when(w.gameweeks_away)})</span>
              <span class="text-muted"> — {w.detail}</span>
              <span class="block text-mini text-muted2 mt-0.5">
                Fix: {w.fixable_by}
              </span>
            </span>
          </li>
        {/each}
      </ul>
    {/if}

    <p class="text-mini text-muted2 mt-3 pt-2.5 border-t border-line">
      Descriptive, not predictive — every line is read off the published fixture
      list and the squad as it stands. This is <b>not</b> a rule against owning
      both sides of a match: two good players are two good players. It says
      which of your outcomes cancel, and how much of a week rides on one
      afternoon.
    </p>
  </section>
{/if}
