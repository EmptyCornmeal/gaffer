// Shared display formatters.
//
// `export/artifacts.py` already divides prices by 10, so `Player.price` is
// millions (5.0 = £5.0m). Two sites on the Meta page divided again and rendered
// "£0.55m". One helper, used everywhere, so that class of bug cannot recur.

/**
 * Format an already-in-millions price as `£5.0m`.
 *
 * Zero is a real value, not a missing one — only null/undefined/NaN render as
 * the placeholder.
 */
export function formatPrice(price: number | null | undefined, fallback = '—'): string {
  if (price == null || typeof price !== 'number' || Number.isNaN(price)) return fallback
  return `£${price.toFixed(1)}m`
}

/** Points-per-million, guarding the divide-by-zero. */
export function valuePerMillion(points: number, price: number | null | undefined): number {
  if (price == null || !Number.isFinite(price) || price <= 0) return 0
  return points / price
}
