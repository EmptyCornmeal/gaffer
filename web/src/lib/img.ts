// Official Premier League images, loaded directly via <img> (no proxy needed —
// CORS only blocks JS fetch, not <img> rendering).

export function playerPhoto(code: number | null | undefined, size: '110x140' | '250x250' = '110x140'): string {
  return code ? `https://resources.premierleague.com/premierleague/photos/players/${size}/p${code}.png` : ''
}

export function crest(teamCode: number | null | undefined): string {
  return teamCode ? `https://resources.premierleague.com/premierleague/badges/50/t${teamCode}@x2.png` : ''
}
