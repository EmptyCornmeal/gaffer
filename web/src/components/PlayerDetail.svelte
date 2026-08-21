<script lang="ts">
  import type { Player } from '../lib/types'
  import { playerPhoto } from '../lib/img'
  import Crest from './Crest.svelte'
  import FixtureStrip from './FixtureStrip.svelte'
  import Icon from './Icon.svelte'

  let { player, onclose }: { player: Player | null; onclose: () => void } = $props()

  // Focus management: focus the close button on open, and trap Tab inside the
  // dialog so keyboard focus can't wander to the page behind it (WCAG 2.4.3).
  let closeBtn = $state<HTMLButtonElement | null>(null)
  let card = $state<HTMLElement | null>(null)
  $effect(() => {
    if (player) closeBtn?.focus()
  })
  function trap(e: KeyboardEvent) {
    // Escape closes, which is the behaviour a screen-reader user expects from
    // anything announced as a modal dialog.
    if (e.key === 'Escape') { e.preventDefault(); onclose(); return }
    if (e.key !== 'Tab' || !card) return
    const f = card.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    )
    if (!f.length) return
    const first = f[0]
    const last = f[f.length - 1]
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
  }

  const parts = $derived(
    player
      ? [
          { label: 'Appearance', v: player.breakdown.appearance, c: '#94a3b8' },
          { label: 'Goals', v: player.breakdown.goals, c: '#f04452' },
          { label: 'Assists', v: player.breakdown.assists, c: '#fab219' },
          { label: 'Clean sheet', v: player.breakdown.clean_sheet, c: '#60a5fa' },
          { label: 'DEFCON', v: player.breakdown.defcon, c: '#10b981' },
          { label: 'Saves', v: player.breakdown.saves ?? 0, c: '#38bdf8' },
          { label: 'Bonus', v: player.breakdown.bonus, c: '#34d399' },
        ].filter((p) => p.v > 0.001)
      : [],
  )
  const total = $derived(player?.next_gw_xp ?? 0)
  // The card used to show six of the ten components under a headline that was a
  // blend of a seventh thing, so the arithmetic on screen could never close.
  // `other` carries the negative terms and `blend` names the gap between
  // Gaffer's own model number and the published one, so the column adds up.
  const other = $derived(player?.breakdown.other ?? 0)
  const modelXp = $derived(player?.model_xp ?? null)
  const blend = $derived(
    player && modelXp != null ? Math.round((player.next_gw_xp - modelXp) * 100) / 100 : 0,
  )
  const showsLedger = $derived(Math.abs(other) >= 0.005 || Math.abs(blend) >= 0.005)
  const hasProjection = $derived(total > 0.05 && parts.length > 0)
  const partsSum = $derived(parts.reduce((s, p) => s + p.v, 0) || 1)
  const photo = $derived(player ? playerPhoto(player.code, '250x250') : '')
  let photoBroken = $state(false)
  $effect(() => {
    player // reset when the player changes
    photoBroken = false
  })

  // Distribution scaled from 0 so it reads as a real range, not a progress bar.
  const dist = $derived(player?.dist ?? null)
  const distSpan = $derived(dist ? (Math.max(dist.ceiling, total) * 1.08 || 1) : 1)
  const pct = (v: number) => `${Math.max(0, Math.min(100, (v / distSpan) * 100))}%`

  // Header pills: the xMins badge + tags, deduped (drop any tag that just repeats
  // the minutes badge) and rendered as one consistent pill row.
  const pills = $derived(
    player
      ? [
          { label: `${player.xmins_badge.label} ${player.xmins_badge.hint}`.trim(), kind: player.xmins_badge.kind },
          ...player.tags.filter(
            (t) => t.label.toLowerCase() !== player.xmins_badge.label.toLowerCase(),
          ),
        ]
      : [],
  )
  const kindClass: Record<string, string> = {
    good: 'chip-good', warn: 'chip-warn', bad: 'chip-bad', info: 'chip-info',
  }
</script>

<svelte:window onkeydown={(e) => player && e.key === 'Escape' && onclose()} />

{#if player}
  <!-- The keydown handler lives on the element that IS the dialog, so the role,
       the modal semantics and the keyboard behaviour are the same node. -->
  <div
    class="fixed inset-0 z-50 flex items-end sm:items-center sm:justify-center"
    role="dialog"
    aria-modal="true"
    tabindex="-1"
    aria-label="{player.name} details"
    onkeydown={trap}
  >
    <button class="absolute inset-0 bg-black/60 backdrop-blur-sm" aria-label="Close" onclick={onclose}></button>
    <div
      bind:this={card}
      class="relative w-full sm:max-w-lg card rounded-t-2xl sm:rounded-2xl rise max-h-[90vh] overflow-y-auto"
    >
      <button
        bind:this={closeBtn}
        onclick={onclose}
        aria-label="Close"
        class="absolute top-3.5 right-3.5 z-10 w-8 h-8 rounded-full bg-bg3/80 border border-line text-muted hover:text-text flex items-center justify-center"
      ><Icon name="x" size={16} /></button>

      <!-- ===== HERO ===== -->
      <div class="aura px-5 pt-5 pb-4 border-b border-line">
        <div class="w-9 h-1 rounded-full bg-line2 mx-auto mb-3 sm:hidden"></div>
        <div class="relative flex items-start gap-3.5 pr-8">
          {#if photo && !photoBroken}
            <img src={photo} alt={player.name} onerror={() => (photoBroken = true)}
              class="w-16 h-16 rounded-xl object-cover object-top bg-bg3 border border-line shrink-0" />
          {:else}
            <div class="w-16 h-16 rounded-xl bg-bg3 border border-line flex items-center justify-center shrink-0"><Crest code={player.team_code} short={player.team} size={34} /></div>
          {/if}
          <div class="flex-1 min-w-0">
            <div class="text-xl font-black leading-tight truncate">{player.name}</div>
            <div class="text-sm text-muted flex items-center gap-1.5 mt-0.5">
              <Crest code={player.team_code} short={player.team} size={15} />
              <span>{player.pos} · {player.team}</span>
            </div>
            <div class="text-[13px] text-muted2 mt-0.5">£{player.price.toFixed(1)}m · {player.owned_by}% owned</div>
          </div>
          <div class="text-right shrink-0">
            <div class="text-4xl font-black text-brand-light tabular-nums leading-none">{total.toFixed(1)}</div>
            <div class="text-micro uppercase tracking-wide text-muted2 mt-1">xP next GW</div>
            {#if dist && dist.ceiling > 0}
              <div class="text-mini text-muted2 mt-1 tabular-nums">{dist.floor}–{dist.ceiling} range</div>
            {/if}
          </div>
        </div>
        <div class="relative mt-2.5 flex flex-wrap gap-1">
          {#each pills as p}<span class="chip {kindClass[p.kind] ?? 'chip-info'}">{p.label}</span>{/each}
        </div>
      </div>

      <div class="p-5 flex flex-col gap-4">
        <!-- WHY -->
        <p class="text-sm leading-relaxed text-text">
          <span class="text-brand-light font-semibold">Why:</span>{' '}{player.rationale}
        </p>
        {#if player.news}
          <div class="text-xs chip-bad rounded-lg px-3 py-2 -mt-1">{player.news}</div>
        {/if}

        <!-- ===== PROJECTION ===== -->
        <section class="rounded-xl bg-bg2 border border-line p-3.5">
          <div class="flex items-center justify-between mb-2.5">
            <h3 class="text-mini font-bold uppercase tracking-wide text-muted2">Projection · next GW</h3>
            {#if dist && dist.boom >= 8}<span class="chip chip-good">🔥 {dist.boom}% haul</span>{/if}
          </div>

          {#if hasProjection}
            <!-- stacked contribution -->
            <div class="flex h-2.5 rounded-full overflow-hidden bg-bg3">
              {#each parts as p}<div style="width:{(p.v / partsSum) * 100}%; background:{p.c}" title="{p.label} {p.v.toFixed(2)}"></div>{/each}
            </div>
            <div class="mt-2.5 grid grid-cols-2 gap-x-5 gap-y-1.5 text-[13px]">
              {#each parts as p}
                <div class="flex items-center justify-between">
                  <span class="flex items-center gap-2 text-muted"><span class="w-2.5 h-2.5 rounded-sm" style="background:{p.c}"></span>{p.label}</span>
                  <span class="tabular-nums text-text">{p.v.toFixed(2)}</span>
                </div>
              {/each}
            </div>

            {#if showsLedger}
              <div class="mt-2.5 pt-2.5 border-t border-line grid grid-cols-2 gap-x-5 gap-y-1.5 text-[13px]">
                {#if Math.abs(other) >= 0.005}
                  <div class="flex items-center justify-between">
                    <span class="text-muted">Conceded, cards, other</span>
                    <span class="tabular-nums text-text">{other.toFixed(2)}</span>
                  </div>
                {/if}
                {#if Math.abs(blend) >= 0.005}
                  <div class="flex items-center justify-between">
                    <span class="text-muted" title="FPL publishes its own one-week expected points; the pipeline blends it in when it carries information.">FPL ep_next blend</span>
                    <span class="tabular-nums text-text">{blend > 0 ? '+' : ''}{blend.toFixed(2)}</span>
                  </div>
                {/if}
                <div class="flex items-center justify-between col-span-2 font-semibold">
                  <span class="text-muted">Total xP</span>
                  <span class="tabular-nums text-brand-light">{total.toFixed(2)}</span>
                </div>
              </div>
            {/if}

            {#if dist && dist.ceiling > 0}
              <!-- distribution, scaled from 0 -->
              <div class="mt-4">
                <div class="text-mini text-muted2 mb-1.5">Likely range this week</div>
                <div class="relative h-7 rounded-lg bg-bg3 overflow-hidden">
                  <div class="absolute inset-y-1 rounded bg-brand/25 border-x border-brand/40" style="left:{pct(dist.floor)}; right:calc(100% - {pct(dist.ceiling)})"></div>
                  <div class="absolute inset-y-0 w-[3px] rounded bg-brand-light" style="left:{pct(total)}"></div>
                </div>
                <div class="flex justify-between text-mini mt-1.5 tabular-nums">
                  <span class="text-muted2">Floor <b class="text-muted">{dist.floor}</b></span>
                  <span class="text-muted2">Expected <b class="text-brand-light">{total.toFixed(1)}</b></span>
                  <span class="text-muted2">Ceiling <b class="text-muted">{dist.ceiling}</b></span>
                </div>
              </div>
            {/if}
          {:else}
            <div class="text-sm text-muted py-1">
              No projected points this week{player.status && player.status !== 'a' ? ` — ${player.news || 'flagged / not expected to feature'}` : ' — not expected to feature'}.
            </div>
          {/if}
        </section>

        <!-- ===== UNDERLYING ===== -->
        <section>
          <h3 class="text-mini font-bold uppercase tracking-wide text-muted2 mb-2">Underlying</h3>
          <div class="grid grid-cols-4 gap-2 text-center">
            {#each [
              { k: 'Start', v: `${Math.round(player.p_start * 100)}%` },
              { k: 'Form', v: player.form.toFixed(1) },
              { k: '6-GW xP', v: player.xp_window.toFixed(0), accent: true },
              { k: 'Conf.', v: `${Math.round(player.confidence * 100)}%` },
            ] as s}
              <div class="rounded-lg bg-bg2 border border-line py-2">
                <div class="text-micro uppercase tracking-wide text-muted2">{s.k}</div>
                <div class="font-black tabular-nums {s.accent ? 'text-accent-light' : 'text-text'}">{s.v}</div>
              </div>
            {/each}
          </div>
          <div class="grid grid-cols-2 gap-2 mt-2 text-center">
            <div class="rounded-lg bg-bg2 border border-line py-2">
              <div class="text-micro uppercase tracking-wide text-muted2">xGI / 90</div>
              <div class="font-black tabular-nums text-text">{player.xgi90.toFixed(2)}</div>
            </div>
            <div class="rounded-lg bg-bg2 border border-line py-2">
              <div class="text-micro uppercase tracking-wide text-muted2">DEFCON / 90</div>
              <div class="font-black tabular-nums text-text">{player.defcon90.toFixed(1)}</div>
            </div>
          </div>

          <!-- DEFCON projection (only where it's a real returns source, not 0% noise) -->
          {#if player.defcon && player.defcon.p_hit >= 0.05}
            {@const dc = player.defcon}
            <div class="mt-2 rounded-lg bg-bg2 border border-line px-3 py-2.5">
              <div class="flex items-center justify-between">
                <div class="text-mini font-bold uppercase tracking-wide text-muted2 flex items-center gap-1.5">
                  DEFCON +2 chance
                  {#if dc.near_hit}<span class="chip chip-warn">near-hit</span>{/if}
                </div>
                <div class="text-sm font-black tabular-nums {dc.p_hit >= 0.5 ? 'text-brand-light' : 'text-text'}">{Math.round(dc.p_hit * 100)}%</div>
              </div>
              <div class="mt-1.5 h-1.5 rounded-full bg-bg3 overflow-hidden">
                <div class="h-full {dc.p_hit >= 0.5 ? 'bg-brand' : 'bg-accent'}" style="width:{Math.round(dc.p_hit * 100)}%"></div>
              </div>
              <div class="text-mini text-muted2 mt-1">{dc.per90} defensive actions/90 · needs {dc.threshold} for the +2</div>
            </div>
          {/if}
        </section>

        <!-- ===== CONTEXT ===== -->
        <section>
          <div class="flex items-center justify-between mb-2">
            <h3 class="text-mini font-bold uppercase tracking-wide text-muted2">Next fixtures</h3>
            <FixtureStrip fixtures={player.fixtures} />
          </div>

          {#if player.last_season}
            <div class="rounded-lg bg-bg2 border border-line px-3 py-2.5">
              <div class="text-micro uppercase tracking-wide mb-1.5
                          {player.last_season.is_prior_season === false
                            ? 'text-amber' : 'text-muted2'}">
                {#if player.last_season.is_prior_season === false}
                  {player.last_season.season} · not last season
                {:else if player.last_season.season}
                  Last season · {player.last_season.season}
                {:else}
                  Most recent recorded season
                {/if}
              </div>
              {#if player.last_season.is_prior_season === false}
                <div class="text-micro text-amber/90 mb-1.5 leading-snug">
                  His last Premier League season, not the one just gone — the
                  projection is leaning on old evidence.
                </div>
              {/if}
              <div class="grid grid-cols-4 gap-2 text-center text-[13px]">
                <div><div class="font-bold tabular-nums">{player.last_season.minutes.toLocaleString()}</div><div class="text-micro text-muted2">mins</div></div>
                <div><div class="font-bold tabular-nums">{player.last_season.starts}</div><div class="text-micro text-muted2">starts</div></div>
                <div><div class="font-bold tabular-nums text-brand-light">{player.last_season.xg90.toFixed(2)}</div><div class="text-micro text-muted2">xG/90</div></div>
                <div><div class="font-bold tabular-nums text-accent-light">{player.last_season.xa90.toFixed(2)}</div><div class="text-micro text-muted2">xA/90</div></div>
              </div>
            </div>
          {:else}
            <div class="text-xs text-muted2">No Premier League history last season — the projection leans on a position/price prior.</div>
          {/if}

          {#if player.set_pieces || player.price_pred.dir !== 'stable' || (player.price_pred.progress && Math.abs(player.price_pred.momentum) > 0)}
            <div class="mt-2 flex flex-wrap items-center gap-2 text-xs">
              {#if player.set_pieces}<span class="chip chip-info">⚽ {player.set_pieces}</span>{/if}
              {#if player.price_pred.dir === 'up'}
                <span class="chip chip-good">▲ rising · {(player.price_pred.momentum / 1000).toFixed(0)}k in</span>
              {:else if player.price_pred.dir === 'down'}
                <span class="chip chip-bad">▼ falling · {(Math.abs(player.price_pred.momentum) / 1000).toFixed(0)}k out</span>
              {/if}
            </div>
            {#if player.price_pred.progress && Math.abs(player.price_pred.momentum) > 0}
              {@const up = player.price_pred.momentum > 0}
              <div class="mt-2">
                <div class="flex items-center justify-between text-mini text-muted2 mb-1">
                  <span>Est. progress to price {up ? 'rise' : 'fall'}</span>
                  <span class="tabular-nums text-muted">{Math.round(player.price_pred.progress * 100)}%</span>
                </div>
                <div class="h-1.5 rounded-full bg-bg3 overflow-hidden">
                  <div class="h-full {up ? 'bg-brand' : 'bg-red'}" style="width:{Math.min(100, player.price_pred.progress * 100)}%"></div>
                </div>
              </div>
            {/if}
          {/if}
        </section>
      </div>
    </div>
  </div>
{/if}
