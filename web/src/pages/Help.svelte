<script lang="ts">
  import { GLOSSARY_SECTIONS } from '../lib/glossary'

  let q = $state('')
  const sections = $derived(
    GLOSSARY_SECTIONS.map((s) => ({
      title: s.title,
      terms: s.terms.filter(
        (t) => !q.trim() || `${t.term} ${t.def}`.toLowerCase().includes(q.trim().toLowerCase()),
      ),
    })).filter((s) => s.terms.length),
  )
</script>

<div class="rise max-w-3xl">
  <h1 class="font-black text-xl mb-1">How Gaffer works</h1>
  <p class="text-sm text-muted mb-4">
    Every projection is a transparent sum of parts — appearance, goals, assists, clean sheet,
    DEFCON and bonus — gated by projected minutes, and each pick comes with a plain-English reason.
  </p>

  <input
    bind:value={q}
    placeholder="Filter terms…"
    aria-label="Filter glossary terms"
    class="w-full sm:w-72 rounded-lg bg-card border border-line px-3 py-2 text-sm mb-5 focus:outline-none focus:border-accent"
  />

  {#each sections as s (s.title)}
    <section class="mb-6">
      <h2 class="text-xs font-bold uppercase tracking-wide text-brand-light mb-2.5">{s.title}</h2>
      <dl class="grid sm:grid-cols-2 gap-3">
        {#each s.terms as t (t.term)}
          <div class="card p-3">
            <dt><span class="chip chip-info">{t.term}</span></dt>
            <dd class="text-sm text-muted mt-1.5 leading-relaxed">{t.def}</dd>
          </div>
        {/each}
      </dl>
    </section>
  {:else}
    <p class="text-sm text-muted2">No terms match “{q}”.</p>
  {/each}
</div>
