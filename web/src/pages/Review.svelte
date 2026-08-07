<script lang="ts">
  import type { Bundle } from '../lib/data'
  import {
    parseReview, VERDICT_LABELS, VERDICT_TONE, ATTRIBUTION_LABELS,
    signed, pctOf,
  } from '../lib/weekly'
  import Icon from '../components/Icon.svelte'

  let { bundle, onnav }: { bundle: Bundle; onnav: (r: string) => void } = $props()

  const parsed = $derived(parseReview(bundle.review))
  const r = $derived(parsed.kind === 'ok' ? parsed.data : null)
  const q = $derived(r?.quality ?? null)
  const cmp = $derived(r?.comparison ?? null)

  let showLimits = $state(false)

  const tone = $derived(q ? VERDICT_TONE[q.verdict] : 'info')
  const toneClass = $derived(
    tone === 'good' ? 'chip-good' : tone === 'warn' ? 'chip-warn'
      : tone === 'bad' ? 'chip-bad' : 'chip-info',
  )

  // Attribution, largest absolute contribution first — the story is where the
  // points went, not the order of the dataclass fields.
  const lines = $derived(
    Object.entries(r?.attribution ?? {})
      .filter(([, v]) => Number.isFinite(v))
      .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])),
  )
  const maxAbs = $derived(Math.max(1, ...lines.map(([, v]) => Math.abs(v))))
</script>

<div class="rise flex flex-col gap-4 max-w-3xl mx-auto w-full">
  <div>
    <h2 class="font-bold text-lg flex items-center gap-2">
      <Icon name="award" size={18} /> Review
    </h2>
    <p class="text-sm text-muted">
      What the decision was worth, separated from what the dice did.
    </p>
  </div>

  {#if parsed.kind === 'missing'}
    <div class="card p-6 text-center">
      <h3 class="font-bold">No gameweek has been reviewed yet</h3>
      <p class="text-sm text-muted mt-2">
        A review appears once a gameweek has finished and its results are in.
        Nothing is generated before then.
      </p>
    </div>
  {:else if parsed.kind !== 'ok'}
    <div class="card p-6">
      <h3 class="font-bold text-red">This build can't render that review</h3>
      <p class="text-sm text-muted mt-2">{parsed.detail}</p>
    </div>
  {:else if r && q}
    <!-- ── the verdict ────────────────────────────────────────────── -->
    <section class="card p-4" aria-labelledby="verdict">
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <h3 id="verdict" class="font-bold">Gameweek {r.event}</h3>
        <span class="chip {toneClass}">{VERDICT_LABELS[q.verdict]}</span>
      </div>
      <p class="text-sm mt-2">{q.explanation}</p>

      <div class="grid grid-cols-3 gap-2 mt-3 text-center">
        <div class="rounded-lg bg-bg3 p-2">
          <div class="text-[10px] uppercase text-muted2 font-bold">Expected</div>
          <div class="text-xl font-black tabular-nums">
            {q.expected_at_decision ?? '—'}
          </div>
        </div>
        <div class="rounded-lg bg-bg3 p-2">
          <div class="text-[10px] uppercase text-muted2 font-bold">Scored</div>
          <div class="text-xl font-black tabular-nums">{q.realised ?? '—'}</div>
        </div>
        <div class="rounded-lg bg-bg3 p-2">
          <div class="text-[10px] uppercase text-muted2 font-bold">Percentile</div>
          <div class="text-xl font-black tabular-nums">
            {q.outcome_percentile == null ? '—' : pctOf(q.outcome_percentile)}
          </div>
        </div>
      </div>
      {#if !r.has_snapshot}
        <p class="text-[11px] text-yellow mt-2">
          No pre-deadline record exists for this gameweek, so only the result is
          shown — the decision itself cannot be judged.
        </p>
      {/if}
    </section>

    <!-- ── four worlds ────────────────────────────────────────────── -->
    {#if cmp}
      <section class="card p-4" aria-labelledby="worlds">
        <h3 id="worlds" class="font-bold text-sm">What each choice would have scored</h3>
        <table class="data mt-2">
          <tbody>
            <tr>
              <td class="!text-left text-muted">Gaffer's recommendation</td>
              <td class="!text-left tabular-nums">{cmp.recommended_points ?? '—'}</td>
            </tr>
            <tr>
              <td class="!text-left text-muted">What you did</td>
              <td class="!text-left tabular-nums font-bold">{cmp.actual_points ?? '—'}</td>
            </tr>
            <tr>
              <td class="!text-left text-muted">Holding</td>
              <td class="!text-left tabular-nums">{cmp.hold_points ?? '—'}</td>
            </tr>
            {#if cmp.hindsight_points != null}
              <tr class="opacity-60">
                <td class="!text-left text-muted2">
                  Perfect hindsight
                  <span class="chip chip-warn ml-1">unknowable at the time</span>
                </td>
                <td class="!text-left tabular-nums">{cmp.hindsight_points}</td>
              </tr>
            {/if}
          </tbody>
        </table>
        <p class="text-[11px] text-muted2 mt-2">{cmp.note}</p>
      </section>
    {/if}

    <!-- ── attribution ────────────────────────────────────────────── -->
    {#if lines.length}
      <section class="card p-4" aria-labelledby="attr">
        <h3 id="attr" class="font-bold text-sm mb-2">Where the points came from</h3>
        <div class="space-y-1.5">
          {#each lines as [key, value]}
            <div class="flex items-center gap-2">
              <span class="text-[11px] w-28 shrink-0 text-muted">
                {ATTRIBUTION_LABELS[key] ?? key}
              </span>
              <div class="flex-1 h-4 rounded bg-bg3 overflow-hidden">
                <div
                  class="h-full {value >= 0 ? 'bg-brand/70' : 'bg-red/70'}"
                  style="width:{(Math.abs(value) / maxAbs) * 100}%"
                ></div>
              </div>
              <span class="text-xs tabular-nums w-12 text-right">{signed(value, 0)}</span>
            </div>
          {/each}
        </div>
      </section>
    {/if}

    <!-- ── the lesson ─────────────────────────────────────────────── -->
    {#if r.lesson}
      <section class="card p-4" aria-labelledby="lesson">
        <h3 id="lesson" class="font-bold text-sm">This week's lesson</h3>
        <p class="text-sm mt-1">{r.lesson.text}</p>
        {#if r.lesson.key !== 'no_pattern_yet'}
          <p class="text-[11px] text-muted2 mt-1">
            Based on {r.lesson.weeks} reviewed gameweek(s).
          </p>
        {/if}
      </section>
    {/if}

    <!-- ── limitations ────────────────────────────────────────────── -->
    {#if r.limitations?.length}
      <section class="card p-3">
        <button
          class="text-xs font-bold text-muted min-h-11"
          aria-expanded={showLimits}
          onclick={() => (showLimits = !showLimits)}
        >{showLimits ? '▾' : '▸'} How this was measured</button>
        {#if showLimits}
          <ul class="text-[11px] text-muted2 mt-2 list-disc pl-4 space-y-0.5">
            {#each r.limitations as x}<li>{x}</li>{/each}
            {#if r.snapshot_as_of}
              <li>Scored against the pre-deadline snapshot taken at
                <code>{r.snapshot_as_of}</code>.</li>
            {/if}
          </ul>
        {/if}
      </section>
    {/if}
  {/if}
</div>
