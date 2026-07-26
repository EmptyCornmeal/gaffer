// Abbreviations -> plain-English definitions with FPL decision context, grouped
// into categories so the Help page reads as sections, not a bag of 28 cards.
export interface GlossaryTerm {
  term: string
  def: string
}
export interface GlossarySection {
  title: string
  terms: GlossaryTerm[]
}

export const GLOSSARY_SECTIONS: GlossarySection[] = [
  {
    title: 'The FPL calendar',
    terms: [
      { term: 'GW', def: 'Gameweek — a round of fixtures. The deadline is when you must finalise your team.' },
      { term: 'DGW', def: 'Double Gameweek — a team plays twice in one GW. Great for captaincy and Bench Boost.' },
      { term: 'BGW', def: 'Blank Gameweek — a team has no fixture. Avoid unless you have bench cover.' },
      { term: 'FT', def: 'Free Transfers — roll up to 5 (2026/27). Extra transfers cost -4 points each.' },
      { term: 'Hit (-4)', def: 'The 4-point cost of each transfer beyond your free ones. Only worth it if the incoming player beats the outgoing by more than 4 over the weeks you own him.' },
    ],
  },
  {
    title: 'Chips',
    terms: [
      { term: 'BB', def: 'Bench Boost — all 15 players score. Best in a DGW with strong bench fixtures.' },
      { term: 'FH', def: 'Free Hit — unlimited transfers for one GW only. Great for BGWs/DGWs.' },
      { term: 'TC', def: 'Triple Captain — captain scores 3×. Best on a fixture-proof premium in a DGW.' },
      { term: 'WC', def: 'Wildcard — unlimited transfers that persist. Use to restructure your squad.' },
    ],
  },
  {
    title: 'How Gaffer scores players',
    terms: [
      { term: 'xP', def: 'Expected Points — Gaffer projects points from minutes, xGI, clean-sheet odds, DEFCON and bonus. The core ranking.' },
      { term: 'xP window', def: 'Expected points summed over the next few gameweeks — rewards a good fixture run, not just next week.' },
      { term: 'Ceiling / Floor', def: "A player's realistic best and worst week from a Monte-Carlo simulation. Ceiling is what you captain for; a high floor is a safe, consistent pick." },
      { term: 'Boom %', def: 'The simulated chance of a 10+ point haul — the explosive upside behind a captaincy call.' },
      { term: 'xMins', def: 'Projected minutes. NAILED ≈ certain starter, ROTATION ≈ start risk, CAMEO ≈ likely off the bench.' },
      { term: 'xGI', def: 'Expected Goal Involvements (xG + xA) — underlying attacking output. Compare to actual returns to spot form/luck.' },
      { term: 'xGI/90', def: 'xGI per 90 minutes — normalises for playing time so part-timers and regulars compare fairly.' },
      { term: 'xGC', def: 'Expected Goals Conceded — how many goals a defence is expected to allow. Gaffer uses this for clean-sheet odds and fixture difficulty.' },
      { term: 'DEFCON', def: 'Defensive Contribution points (2026/27) — +2 for hitting a threshold of defensive actions (10 for DEF, 12 for MID/FWD). A big source of value for ball-winners.' },
      { term: 'CS', def: 'Clean Sheet — 4 pts for DEF/GKP, 1 for MID if the team concedes 0.' },
      { term: 'BPS', def: 'Bonus Points System — an underlying score that awards 1-3 bonus points per match.' },
      { term: 'ICT', def: "FPL's Influence-Creativity-Threat index — a composite of a player's on-ball involvement. A useful cross-check on xGI." },
    ],
  },
  {
    title: 'Ownership & strategy',
    terms: [
      { term: 'EO', def: 'Effective Ownership — % of managers owning a player, captaincy weighted 2×. High EO = template (safe), low = differential.' },
      { term: 'FDR', def: "Fixture Difficulty. Gaffer's is xGC-based (position-aware), not FPL's flat 1-5." },
      { term: 'Form', def: 'Average points over the last 5 GWs — current hot/cold streak.' },
      { term: 'Value', def: 'Expected points per £million — find the efficient picks, not just the best.' },
      { term: 'Differential', def: 'A low-owned player (roughly under 12%). If they return, you gain rank on the crowd who don’t own them.' },
      { term: 'Template', def: 'The highly-owned core (25%+). Not owning a template pick is effectively a punt against the field.' },
      { term: 'Price change', def: 'Prices drift with net transfers. Gaffer estimates how close a player is to a rise/fall so you can buy/sell before it moves.' },
    ],
  },
]

// Flat map kept for any consumer that just wants term -> definition.
export const GLOSSARY: Record<string, string> = Object.fromEntries(
  GLOSSARY_SECTIONS.flatMap((s) => s.terms.map((t) => [t.term, t.def])),
)
