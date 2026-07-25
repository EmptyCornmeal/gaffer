// Abbreviations -> plain-English definitions with FPL decision context.
// Ported from v1 (js/glossary.js) and extended for Gaffer's model (DEFCON, xGC, xP window).
export const GLOSSARY: Record<string, string> = {
  GW: 'Gameweek — a round of fixtures. The deadline is when you must finalise your team.',
  DGW: 'Double Gameweek — a team plays twice in one GW. Great for captaincy and Bench Boost.',
  BGW: 'Blank Gameweek — a team has no fixture. Avoid unless you have bench cover.',
  FT: 'Free Transfers — roll up to 5 (2026/27). Extra transfers cost -4 points each.',
  BB: 'Bench Boost — all 15 players score. Best in a DGW with strong bench fixtures.',
  FH: 'Free Hit — unlimited transfers for one GW only. Great for BGWs/DGWs.',
  TC: 'Triple Captain — captain scores 3×. Best on a fixture-proof premium in a DGW.',
  WC: 'Wildcard — unlimited transfers that persist. Use to restructure your squad.',
  xP: 'Expected Points — Gaffer projects points from minutes, xGI, clean-sheet odds, DEFCON and bonus. The core ranking.',
  'xP window': 'Expected points summed over the next few gameweeks — rewards a good fixture run, not just next week.',
  xMins: 'Projected minutes. NAILED ≈ certain starter, ROTATION ≈ start risk, CAMEO ≈ likely off the bench.',
  xGI: 'Expected Goal Involvements (xG + xA) — underlying attacking output. Compare to actual returns to spot form/luck.',
  'xGI/90': 'xGI per 90 minutes — normalises for playing time so part-timers and regulars compare fairly.',
  xGC: 'Expected Goals Conceded — how many goals a defence is expected to allow. Gaffer uses this for clean-sheet odds and fixture difficulty.',
  DEFCON:
    'Defensive Contribution points (2026/27) — +2 for hitting a threshold of defensive actions (10 for DEF, 12 for MID). A big source of value for ball-winners.',
  EO: 'Effective Ownership — % of managers owning a player, captaincy weighted 2×. High EO = template (safe), low = differential.',
  FDR: "Fixture Difficulty. Gaffer's is xGC-based (position-aware), not FPL's flat 1-5.",
  CS: 'Clean Sheet — 4 pts for DEF/GKP, 1 for MID if the team concedes 0.',
  Form: 'Average points over the last 5 GWs — current hot/cold streak.',
  BPS: 'Bonus Points System — underlying score that awards 1-3 bonus points per match.',
  Value: 'Expected points per £million — find the efficient picks, not just the best.',
}
