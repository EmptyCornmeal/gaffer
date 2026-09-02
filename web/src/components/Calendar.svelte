<script lang="ts">
  /**
   * 5.1 / 5.3 -- what is still to come, and whether waiting costs anything.
   *
   * The scope note is not a disclaimer bolted on the bottom; it is the feature.
   * A reader who sees an empty list here must not conclude that no team news is
   * coming, because this cannot know that. So "what this does not watch" is
   * rendered at the same weight as the events, and is never collapsed away.
   */
  import type { InfoCalendar, WaitVsAct } from '../lib/weekly'

  let { calendar, wait }: {
    calendar: InfoCalendar | null | undefined
    wait: WaitVsAct | null | undefined
  } = $props()

  const events = $derived(calendar?.events ?? [])
  const priced = $derived(events.filter((e) => e.kind !== 'deadline'))
  const deadline = $derived(events.find((e) => e.kind === 'deadline') ?? null)

  const TONE: Record<string, string> = {
    act: 'text-yellow',
    'money may be at stake': 'text-yellow',
    'waiting is cheaper': 'text-brand-light',
    'no rush': 'text-muted',
    'no money either way': 'text-muted',
    'nothing to wait on': 'text-muted',
  }

  const KIND_LABEL: Record<string, string> = {
    price_change_due: 'change due',
    price_change_near: 'close',
    price_locked: 'locked',
  }
</script>

{#if calendar}
  <section class="card p-4" aria-labelledby="cal-heading">
    <h3 id="cal-heading" class="font-bold text-sm">Still to come</h3>

    {#if deadline && !deadline.passed}
      <p class="text-sm text-muted mt-1">
        <b class="text-text tabular-nums">{deadline.in_hours}h</b> to the deadline.
      </p>
    {:else if deadline?.passed}
      <p class="text-sm text-yellow mt-1">The deadline has passed — this gameweek is locked.</p>
    {/if}

    {#if wait}
      <p class="mt-2 text-sm">
        <span class="font-semibold {TONE[wait.verdict] ?? 'text-text'}">{wait.verdict}</span>
        <span class="text-muted"> — {wait.reason}.</span>
      </p>
      {#if wait.money_cost_of_waiting_m}
        <p class="text-mini text-muted2 mt-1 tabular-nums">
          Waiting {wait.money_cost_of_waiting_m > 0 ? 'costs' : 'saves'}
          £{Math.abs(wait.money_cost_of_waiting_m).toFixed(1)}m of team value.
          Reported separately from the {typeof wait.edge_points === 'number'
            ? `${wait.edge_points > 0 ? '+' : ''}${wait.edge_points.toFixed(2)}-point`
            : ''} edge on purpose: there is no exchange rate between them.
        </p>
      {/if}
    {/if}

    {#if priced.length}
      <ul class="mt-3 space-y-1.5 text-sm">
        {#each priced as e}
          <li class="flex items-baseline gap-2">
            <span class="chip {e.kind === 'price_change_due' ? 'chip-warn' : 'chip-info'} shrink-0">
              {KIND_LABEL[e.kind] ?? e.kind}
            </span>
            <span class="min-w-0">
              <b>{e.player?.name}</b>
              <span class="text-muted">{e.changes}</span>
              {#if typeof e.percent === 'number'}
                <span class="text-muted2 tabular-nums"> ({Math.round(Math.abs(e.percent))}% there)</span>
              {/if}
            </span>
          </li>
        {/each}
      </ul>
    {/if}

    <!-- Rendered at the same weight as the list above, and never collapsed.
         The dangerous reading of an empty calendar is "nothing is coming". -->
    <div class="mt-3 pt-2.5 border-t border-line">
      <div class="text-micro uppercase font-bold text-muted2 mb-1">What this does not watch</div>
      <p class="text-mini text-muted">
        {(calendar.does_not_cover ?? []).map((x) => x.what).join(' · ')}.
      </p>
      <p class="text-mini text-muted2 mt-1">{calendar.honesty}</p>
    </div>
  </section>
{/if}
