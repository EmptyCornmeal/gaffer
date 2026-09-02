import { describe, expect, it } from 'vitest'
import {
  badgeCaption, badgeError, evidenceCaption, evidenceTone, MODEL_SECTION, modelLink,
} from './evidence'
import type { Meta } from './types'

const meta = {
  badge_calibration: {
    available: true,
    population: 'considered',
    bands: {
      'NAILED': { claimed: 0.943, start_rate: 0.895, n: 3490, over_claims_by: 0.048 },
      'CAMEO?': { claimed: 0.232, start_rate: 0.233, n: 4285, over_claims_by: -0.001 },
    },
  },
} as unknown as Meta

describe('badge calibration', () => {
  it('prints what the badge claimed and what the badged players did', () => {
    // The whole point of 4.7: NAILED is a one-word confidence statement in
    // capital letters, and it over-claims by five points.
    expect(badgeCaption(meta, 'NAILED')).toBe(
      'NAILED claims 94%, started 90% (n=3,490, considered)')
  })

  it('names the population, because the two populations disagree', () => {
    // CAMEO?'s error changes SIGN between `overall` and `considered`. A caption
    // that did not say which it quoted would be worse than no caption.
    expect(badgeCaption(meta, 'CAMEO?')).toContain('considered')
  })

  it('returns null rather than a placeholder when nothing was measured', () => {
    // An ungraded badge must look ungraded, not confidently zero.
    expect(badgeCaption({} as Meta, 'NAILED')).toBeNull()
    expect(badgeCaption(meta, 'INVENTED')).toBeNull()
    expect(badgeCaption(null, 'NAILED')).toBeNull()
  })

  it('exposes the signed error for callers that colour it', () => {
    expect(badgeError(meta, 'NAILED')).toBeCloseTo(0.048)
    expect(badgeError(meta, 'CAMEO?')).toBeCloseTo(-0.001)
    expect(badgeError(meta, 'nope')).toBeNull()
  })
})

describe('evidence quality captions', () => {
  it('says what the number rests on, and never says confidence', () => {
    const c = evidenceCaption({
      weak_evidence_share: 0.602, largest_weak_component: 'clean_sheet',
    })!
    expect(c).toContain('60%')
    expect(c).toContain('clean sheets')
    expect(c.toLowerCase()).not.toContain('confidence')
    expect(c.toLowerCase()).not.toContain('likely')
  })

  it('is absent, not zero, when the share is missing', () => {
    expect(evidenceCaption(null)).toBeNull()
    expect(evidenceCaption({})).toBeNull()
    expect(evidenceCaption({ weak_evidence_share: NaN })).toBeNull()
  })

  it('grades the tone in three steps and refuses when there is nothing to grade', () => {
    expect(evidenceTone({ weak_evidence_share: 0.60 })).toBe('bad')
    expect(evidenceTone({ weak_evidence_share: 0.30 })).toBe('warn')
    expect(evidenceTone({ weak_evidence_share: 0.10 })).toBe('good')
    expect(evidenceTone(undefined)).toBeNull()
  })
})

describe('links to the evidence', () => {
  it('deep-links through the router rather than past it', () => {
    // The hash IS the route. `#acc-minutes` would navigate to the default page.
    expect(modelLink('badge')).toBe('#/model/acc-minutes')
    expect(modelLink('xp')).toBe('#/model/acc-horizon')
  })

  it('every graded quantity points at a section id', () => {
    for (const [what, id] of Object.entries(MODEL_SECTION)) {
      expect(id, what).toMatch(/^acc-[a-z0-9]+$/)
    }
  })
})
