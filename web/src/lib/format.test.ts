import { describe, expect, it } from 'vitest'
import { formatPrice, valuePerMillion } from './format'

describe('formatPrice', () => {
  it('renders artifact prices (already in millions) correctly', () => {
    // The regression: Meta.svelte divided again and showed £0.55m for a £5.5m player.
    expect(formatPrice(5.0)).toBe('£5.0m')
    expect(formatPrice(5.5)).toBe('£5.5m')
    expect(formatPrice(14.0)).toBe('£14.0m')
    expect(formatPrice(15.5)).toBe('£15.5m')
    expect(formatPrice(4.0)).toBe('£4.0m')
  })

  it('treats zero as a real value, not a missing one', () => {
    expect(formatPrice(0)).toBe('£0.0m')
  })

  it('renders a placeholder only for genuinely missing values', () => {
    expect(formatPrice(null)).toBe('—')
    expect(formatPrice(undefined)).toBe('—')
    expect(formatPrice(Number.NaN)).toBe('—')
    expect(formatPrice(null, 'n/a')).toBe('n/a')
  })
})

describe('valuePerMillion', () => {
  it('divides points by a price already in millions', () => {
    expect(valuePerMillion(30, 7.5)).toBeCloseTo(4.0)
    expect(valuePerMillion(22, 5.5)).toBeCloseTo(4.0)
  })

  it('guards divide-by-zero and missing prices', () => {
    expect(valuePerMillion(30, 0)).toBe(0)
    expect(valuePerMillion(30, null)).toBe(0)
    expect(valuePerMillion(30, undefined)).toBe(0)
  })
})
