import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs'
import { join } from 'node:path'

// --------------------------------------------------------------------------
// Design-system budgets
//
// Two failure modes this codebase has already paid for.
//
// 1. Tailwind fails SILENTLY on a utility with no token behind it: `text-amber`
//    and `text-green` were used in eight places and compiled to nothing, and
//    neither svelte-check nor the perf budget noticed. So any class the design
//    system claims to provide is asserted against the BUILT css, not the source.
//
// 2. Tailwind's type scale stops at 12px, this app legitimately needs smaller,
//    and arbitrary values were the only way to say so. 242 `text-[Npx]` classes
//    across seven sizes accumulated that way — thirteen font sizes in total.
//    Naming the two missing steps only helps if the arbitrary ones cannot
//    quietly return, so their number is capped and can only be revised down.
// --------------------------------------------------------------------------

const SRC = join(process.cwd(), 'src')
const DIST = join(process.cwd(), 'dist', 'assets')

/** Utilities the design system promises. A missing one compiles to nothing. */
const PROMISED = ['text-micro', 'text-mini']

/**
 * Arbitrary font sizes still in the source, by size.
 *
 * These are the survivors of the 2026-08-21 codemod: the ones whose nearest
 * named step is a different size, so rewriting them would change layout rather
 * than just rename a class. Each is a real decision someone has to make. The
 * budget exists so the number goes DOWN — never edit it upward to make a build
 * pass; add the step to the scale, or use one.
 */
const ARBITRARY_TEXT_BUDGET = 27

function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry)
    if (statSync(p).isDirectory()) out.push(...sourceFiles(p))
    else if (/\.(svelte|ts)$/.test(entry) && !entry.endsWith('.test.ts')) out.push(p)
  }
  return out
}

const files = sourceFiles(SRC)
const source = files.map((f) => readFileSync(f, 'utf8')).join('\n')

const builtCss = (() => {
  if (!existsSync(DIST)) return null
  const css = readdirSync(DIST).find((f) => f.endsWith('.css'))
  return css ? readFileSync(join(DIST, css), 'utf8') : null
})()

describe('design system', () => {
  it.each(PROMISED)('%s compiles to real css, not silence', (cls) => {
    if (!builtCss) return // no dist in this run; perf.test.ts owns that warning
    expect(builtCss).toContain(`.${cls}{font-size:`)
  })

  it('does not accumulate new arbitrary font sizes', () => {
    const hits = source.match(/text-\[[0-9]+px\]/g) ?? []
    expect(
      hits.length,
      `${hits.length} arbitrary font sizes (budget ${ARBITRARY_TEXT_BUDGET}). ` +
        `Use text-micro/text-mini or Tailwind's scale; if a new step is genuinely ` +
        `needed, add it to @theme in app.css rather than inlining a pixel value.`,
    ).toBeLessThanOrEqual(ARBITRARY_TEXT_BUDGET)
  })

  // 8px and 9px survive deliberately, and are NOT counted as drift: every one is
  // a glyph inside a tight badge — H/A in a fixture strip, an FDR digit, the VC
  // disc on the pitch, crest initials, a sort caret. They are sized to their
  // container, not read as prose, and bumping them to the 10px floor would
  // break the badge rather than help anyone. 7px and below has no such excuse.
  it('has no font size at or below 7px, which nothing can justify', () => {
    const tooSmall = source.match(/text-\[[0-7]px\]/g) ?? []
    expect(tooSmall).toEqual([])
  })
})
