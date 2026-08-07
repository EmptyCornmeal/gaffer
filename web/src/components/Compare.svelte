<script lang="ts">
  import type { Player } from '../lib/types'
  import Radar from './Radar.svelte'
  import Crest from './Crest.svelte'

  let {
    players,
    pool,
    onclose,
    onremove,
    onpick,
  }: {
    players: Player[]
    pool: Player[]
    onclose: () => void
    onremove: (id: number) => void
    onpick: (id: number) => void
  } = $props()

  const COLORS = ['#10b981', '#3b82f6', '#f472b6']

  // Percentile of `v` within the same-position cohort (nailed-ish players only, so
  // the scale isn't dragged down by 0-minute fringe names).
  function pct(p: Player, get: (x: Player) => number): number {
    const cohort = pool.filter((x) => x.pos === p.pos && x.p_start >= 0.3)
    if (cohort.length < 4) return 0.5
    const v = get(p)
    const below = cohort.filter((x) => get(x) < v).length
    return below / (cohort.length - 1)
  }
  const val = (p: Player) => (p.price ? p.next_gw_xp / p.price : 0)

  const AXES = [
    { key: 'xP', label: 'xP', get: (p: Player) => p.next_gw_xp },
    { key: '6GW', label: '6-GW', get: (p: Player) => p.xp_window },
    { key: 'Threat', label: 'Threat', get: (p: Player) => p.xgi90 },
    { key: 'Defence', label: 'Defence', get: (p: Player) => p.defcon90 },
    { key: 'Form', label: 'Form', get: (p: Player) => p.form },
    { key: 'Value', label: 'Value', get: val },
  ]

  const series = $derived(
    players.map((p, i) => ({
      label: p.name,
      color: COLORS[i % COLORS.length],
      values: AXES.map((a) => pct(p, a.get)),
    })),
  )

  // Raw-value comparison rows, best value in each row highlighted.
  const rows = $derived([
    { label: 'Price', fmt: (p: Player) => `£${p.price.toFixed(1)}m`, get: (p: Player) => -p.price },
    { label: 'xP (next)', fmt: (p: Player) => p.next_gw_xp.toFixed(1), get: (p: Player) => p.next_gw_xp },
    { label: 'xP (6 GW)', fmt: (p: Player) => p.xp_window.toFixed(0), get: (p: Player) => p.xp_window },
    { label: 'Value (xP/£m)', fmt: (p: Player) => val(p).toFixed(2), get: val },
    { label: 'Owned', fmt: (p: Player) => `${p.owned_by}%`, get: (p: Player) => p.owned_by },
    { label: 'Form', fmt: (p: Player) => (p.form ? p.form.toFixed(1) : '—'), get: (p: Player) => p.form },
    { label: 'xGI/90', fmt: (p: Player) => p.xgi90.toFixed(2), get: (p: Player) => p.xgi90 },
    { label: 'DEFCON/90', fmt: (p: Player) => (p.defcon90 ? p.defcon90.toFixed(1) : '—'), get: (p: Player) => p.defcon90 },
    { label: 'xMins', fmt: (p: Player) => p.xmins_badge.label, get: (p: Player) => p.p_start },
  ])
  function best(get: (p: Player) => number): number {
    return Math.max(...players.map(get))
  }

  // Focus management, matching PlayerDetail: focus the close button on open and
  // keep Tab inside the dialog (WCAG 2.4.3), with Escape to dismiss.
  let closeBtn = $state<HTMLButtonElement | null>(null)
  let card = $state<HTMLElement | null>(null)
  $effect(() => {
    closeBtn?.focus()
  })
  function trap(e: KeyboardEvent) {
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
</script>

<div class="fixed inset-0 z-50 flex items-center justify-center p-4">
  <!-- A real button, not a div with a click handler: the backdrop is genuinely
       an interactive control (it dismisses the dialog) and must be reachable
       and operable by keyboard like one. -->
  <button
    class="absolute inset-0 bg-black/60 backdrop-blur-sm"
    aria-label="Close comparison"
    onclick={onclose}
  ></button>
  <div
    bind:this={card}
    class="card w-full max-w-3xl max-h-[90vh] overflow-y-auto p-4 relative"
    role="dialog"
    aria-modal="true"
    tabindex="-1"
    aria-label="Player comparison"
    onkeydown={trap}
  >
    <button bind:this={closeBtn} onclick={onclose} aria-label="Close" class="absolute top-3 right-3 min-w-11 min-h-11 text-muted hover:text-text text-xl leading-none">✕</button>
    <h2 class="font-bold text-lg mb-3">Compare players</h2>

    <div class="grid gap-4 md:grid-cols-2">
      <div>
        <Radar axes={AXES.map((a) => a.label)} {series} />
        <p class="text-[11px] text-muted2 text-center mt-1">Percentile vs same-position starters (outer = elite).</p>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left border-b border-line">
              <th class="py-1.5 font-semibold text-muted text-xs"></th>
              {#each players as p, i}
                <th class="py-1.5 px-1 text-center">
                  <button onclick={() => onpick(p.id)} class="flex flex-col items-center gap-0.5 mx-auto hover:opacity-80">
                    <Crest code={p.team_code} short={p.team} size={20} />
                    <span class="font-bold text-xs leading-tight" style="color:{COLORS[i % COLORS.length]}">{p.name}</span>
                    <span class="text-[9px] text-muted">{p.pos}·{p.team}</span>
                  </button>
                </th>
              {/each}
            </tr>
          </thead>
          <tbody>
            {#each rows as r}
              <tr class="border-b border-line/40">
                <td class="py-1.5 text-xs text-muted">{r.label}</td>
                {#each players as p}
                  {@const isBest = players.length > 1 && r.get(p) === best(r.get)}
                  <td class="py-1.5 px-1 text-center tabular-nums {isBest ? 'font-bold text-brand-light' : ''}">{r.fmt(p)}</td>
                {/each}
              </tr>
            {/each}
          </tbody>
        </table>
        <div class="flex flex-wrap gap-2 mt-3">
          {#each players as p}
            <button onclick={() => onremove(p.id)} class="text-[11px] px-2 py-0.5 rounded-full border border-line text-muted hover:text-red hover:border-red/40">remove {p.name}</button>
          {/each}
        </div>
      </div>
    </div>
  </div>
</div>
