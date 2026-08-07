// Whether Gaffer can actually see your team, and what the Overview briefing is
// therefore allowed to say.
//
// In production the Overview's briefing ended
//
//     "Trust Bruno with the armband and roll with this £100m XI as is — no hit
//      needed."
//
// while `meta.squad_status` was `no_public_squad_yet`. The squad it was talking
// about was one the optimiser built from scratch, and "no hit needed" is not a
// statement anyone could make about a team they cannot read. The briefing is an
// artifact and is not rewritten here; instead the page states what the briefing
// is about, before the reader reaches it.
//
// The decision is made from `meta.squad_status`, never from the date. A calendar
// check would go wrong the moment a fetch failed mid-season.

/** `ingest.py` writes exactly one of these into `meta.squad_status`. */
export const SQUAD_LOADED = 'loaded'
export const SQUAD_NO_PUBLIC_YET = 'no_public_squad_yet'
export const SQUAD_NOT_FOUND = 'not_found'
export const SQUAD_FETCH_FAILED = 'fetch_failed'
export const SQUAD_MALFORMED = 'malformed'
export const SQUAD_NO_ENTRY_ID = 'no_entry_id'
export const SQUAD_STALE = 'stale'

/** What the page may assume about the squad on screen. */
export type SquadKnowledge = 'known' | 'stale' | 'unknown'

export function squadKnowledge(status: string | null | undefined): SquadKnowledge {
  if (status === SQUAD_LOADED) return 'known'
  if (status === SQUAD_STALE) return 'stale'
  // Everything else — including a status this build has never heard of — is
  // unknown. A new failure mode must degrade to the honest branch, not the
  // confident one.
  return 'unknown'
}

/** Which squad the briefing on screen is describing. */
export type BriefingSubject = 'model' | 'plan'

export interface BriefingCaveat {
  /** 'unknown' when we cannot read the squad at all; 'stale' when it is old. */
  tone: 'unknown' | 'stale'
  headline: string
  body: string
  /** The producer's own reason, shown verbatim rather than paraphrased. */
  reason: string | null
}

export interface SquadMeta {
  squad_status?: string | null
  squad_status_reason?: string | null
  squad_source_event?: number | string | null
  entry_name?: string | null
}

function reasonOf(meta: SquadMeta): string | null {
  const r = meta.squad_status_reason
  return typeof r === 'string' && r.trim() ? r.trim() : null
}

/**
 * The caveat to place *above* the briefing, or `null` if none is owed.
 *
 * `null` is returned only when Gaffer has read the real squad and the briefing
 * is about that squad. Every other combination gets a caveat, because every
 * other combination means the advice below is about a team that is not yours.
 */
export function briefingCaveat(
  meta: SquadMeta, subject: BriefingSubject,
): BriefingCaveat | null {
  const knowledge = squadKnowledge(meta.squad_status)

  if (subject === 'plan') {
    // A squad the user assembled in the Planner. Theirs in the sense that they
    // chose it, but still not their FPL entry, so a transfer or hit count
    // against it would be invented.
    if (knowledge === 'known') return null
    return {
      tone: 'unknown',
      headline: 'This is the squad you built in the Planner, not your FPL team.',
      body:
        'Gaffer cannot read your actual FPL squad, so it cannot tell you which '
        + 'transfers to make, whether to take a hit, or how many free transfers you have.',
      reason: reasonOf(meta),
    }
  }

  if (knowledge === 'known') return null

  if (knowledge === 'stale') {
    const gw = meta.squad_source_event
    return {
      tone: 'stale',
      headline: gw
        ? `This reads your squad as it stood at GW${gw}, not as it stands now.`
        : 'This reads a stored squad, not your current one.',
      body:
        'Any transfer or hit advice below is measured against that stored squad, '
        + 'so check it against your real team before acting on it.',
      reason: reasonOf(meta),
    }
  }

  return {
    tone: 'unknown',
    headline: 'This is a model-built reference squad, not your team.',
    body:
      'Gaffer cannot read your squad, so nothing below is a transfer, captain or '
      + 'hit recommendation for you personally. It is the XI the optimiser would '
      + 'build from scratch at this budget — read it as a reference, not an instruction.',
    reason: reasonOf(meta),
  }
}
