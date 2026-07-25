// Accent-insensitive search. "joao pedro" matches "João Pedro"; matches web_name,
// full name, and team.
import type { Player } from './types'

const DIACRITICS = /[̀-ͯ]/g

export function normalize(s: string): string {
  return (s || '').normalize('NFD').replace(DIACRITICS, '').toLowerCase().trim()
}

export function matches(p: Player, query: string): boolean {
  if (!query) return true
  const q = normalize(query)
  return (
    normalize(p.name).includes(q) ||
    normalize(p.full_name).includes(q) ||
    normalize(p.team).includes(q)
  )
}
