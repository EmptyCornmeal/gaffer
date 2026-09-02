"""Fixtures Gaffer's own data cannot see: Europe, the domestic cups, internationals.

**Why this exists.** On 2026-09-02 Gaffer recommended holding the Triple Captain
for GW7 because Ipswich are the worst defence Manchester City meet. GW7 is
Saturday 17 October -- three days after PSG at home and three days before AEK
Athens at home. The Premier League fixture list shows City with six days' rest.
Every input Gaffer had said the week was clean.

**What this is NOT.** It is not a model of those matches. Gaffer does not
project a Champions League tie and has no opinion about who wins one. It needs
four things and stops there: that the fixture exists, when it kicks off, which
FPL clubs it involves, and how far the club had to travel. Anything more would
be a second football model maintained for the benefit of the first.

**Architecture.** A `Source` yields `Fixture` records; a `Competition` names one
tournament and the source that carries it. Adding the Europa League, the FA Cup
or a set of international windows is a new entry in `COMPETITIONS`, not a
rewrite -- which is the requirement this module was designed against.

**Sources are deliberately boring.** Static, public-domain, version-controlled
text over HTTP. No API key, no account, no scraping of a rendered page, no
terms to violate. `openfootball` is a git repository of plain text that has
carried European fixtures since 2011, which makes it auditable in a way a
rate-limited JSON endpoint is not.

**The honest limitation, stated first.** openfootball is community-maintained
and it LAGS. As of 2026-09-02 it carries a complete 2025-26 Champions League
(189 matches, dates, kick-off times) and has no 2026-27 file at all. So this
module is complete for the archive and incomplete for the live season, and
`availability()` reports which. A live-season source is a separate problem and
is recorded as one rather than faked here.
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from gaffer import config

COMPETITIONS_VERSION = "competitions-1"

#: How long a fixture in another competition is treated as being "in the legs".
#: Not fitted -- a declared window, used only to bound the lookups below.
LOOKBACK_DAYS = 14

_UA = {"User-Agent": "gaffer/1.0 (+https://github.com/EmptyCornmeal/gaffer)"}

#: Marks a cached 404 so an absent season is remembered as absent rather than
#: re-asked every fifteen minutes.
_MISS_MARKER = "#gaffer-miss"


@dataclass(frozen=True)
class Fixture:
    """One match in a competition Gaffer does not model."""
    competition: str
    kind: str                 # "european" | "domestic_cup" | "international"
    kickoff: datetime | None  # None when only the date is published
    day: date
    home: str                 # source's own club name
    away: str
    home_country: str | None
    away_country: str | None

    def involves_country(self, code: str) -> bool:
        return code in (self.home_country, self.away_country)


class SourceUnavailable(RuntimeError):
    """The source could not be read. Never silently treated as 'no fixtures'."""


# ---------------------------------------------------------------------------
# openfootball's plain-text format
# ---------------------------------------------------------------------------

#: "  Tue Sep 16 2025" -- and, on every subsequent day of the same matchday,
#: "  Wed Sep 17" with the year omitted. Carrying the year forward is not a
#: nicety: without it every fixture after the first day of a matchday lands on
#: the wrong date, which is the one thing this module exists to get right.
_DATE = re.compile(
    r"^\s{2,}([A-Z][a-z]{2})\s+([A-Z][a-z]{2})\s+(\d{1,2})(?:\s+(\d{4}))?\s*$")
_TIME = re.compile(r"^\s{2,}(\d{2}:\d{2})\s+(.*)$")
_CLUB = re.compile(r"^(.*?)\s*\(([A-Z]{3})\)\s*$")


def _split_match(body: str) -> tuple[str, str] | None:
    """Split 'Home FC (ENG) v Away CF (ESP)  2-1 (1-0)' into two club strings."""
    if " v " not in body:
        return None
    left, _, right = body.partition(" v ")
    # The score trails the away club, separated by run-on whitespace.
    right = re.split(r"\s{2,}", right.strip())[0]
    return left.strip(), right.strip()


def _club(raw: str) -> tuple[str, str | None]:
    m = _CLUB.match(raw)
    return (m.group(1).strip(), m.group(2)) if m else (raw.strip(), None)


def parse_openfootball(text: str, competition: str, kind: str) -> list[Fixture]:
    """Every match in an openfootball competition file, with real dates."""
    out: list[Fixture] = []
    day: date | None = None
    year: int | None = None
    for line in text.splitlines():
        if not line.strip() or line.startswith(("=", "#", "▪")):
            continue
        m = _DATE.match(line)
        if m:
            _, mon, dom, yr = m.groups()
            if yr:
                year = int(yr)
            if year is None:
                continue
            try:
                day = datetime.strptime(f"{mon} {dom} {year}", "%b %d %Y").date()
            except ValueError:
                day = None
            continue
        if day is None:
            continue
        t = _TIME.match(line)
        clock, body = (t.group(1), t.group(2)) if t else (None, line.strip())
        pair = _split_match(body)
        if not pair:
            continue
        home, away = pair
        hn, hc = _club(home)
        an, ac = _club(away)
        ko = None
        if clock:
            hh, mm = clock.split(":")
            ko = datetime(day.year, day.month, day.day, int(hh), int(mm))
        out.append(Fixture(competition=competition, kind=kind, kickoff=ko,
                           day=day, home=hn, away=an,
                           home_country=hc, away_country=ac))
    return out


@dataclass(frozen=True)
class Competition:
    key: str
    name: str
    kind: str
    #: `{season}` is substituted, e.g. "2025-26".
    url: str


#: Everything Gaffer would like to see, and whether a source carries it.
#: Adding one is a line here. The Europa and Conference League files exist for
#: some seasons and not others, which `availability()` reports rather than
#: hides.
COMPETITIONS: tuple[Competition, ...] = (
    Competition("cl", "UEFA Champions League", "european",
                "https://raw.githubusercontent.com/openfootball/champions-league/"
                "master/{season}/cl.txt"),
    Competition("el", "UEFA Europa League", "european",
                "https://raw.githubusercontent.com/openfootball/champions-league/"
                "master/{season}/el.txt"),
    Competition("conf", "UEFA Conference League", "european",
                "https://raw.githubusercontent.com/openfootball/champions-league/"
                "master/{season}/conf.txt"),
)


#: How long a fetched competition file is trusted before it is re-read.
#:
#: Twenty-four hours. A fixture list is one of the slowest-moving things in
#: football -- kick-off times move occasionally, the fixtures themselves almost
#: never -- and the pipeline runs on a fifteen-minute schedule. Re-fetching
#: three files on every tick would put ~300 pointless requests a day on a
#: volunteer-run public repository to learn nothing.
CACHE_TTL_SECONDS = 24 * 3600


def _cache_path(comp: Competition, season: str) -> Path:
    return config.CACHE_DIR / "competitions" / f"{comp.key}-{season}.txt"


def _cached(path: Path) -> str | None:
    """The cached body, if it exists and is young enough."""
    try:
        age = time.time() - path.stat().st_mtime
        if age > CACHE_TTL_SECONDS:
            return None
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _store(path: Path, body: str) -> None:
    """Best effort. A cache that cannot be written must not fail a run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError:
        pass


def fetch(comp: Competition, season: str, *, timeout: int = 30,
          use_cache: bool = True) -> list[Fixture]:
    """One competition's fixtures, from the cache when it is fresh.

    A 404 is cached as an empty marker too. `openfootball` has no file for a
    season until someone writes one, and asking three times a minute for the
    next twelve weeks would be rude as well as pointless.
    """
    path = _cache_path(comp, season)
    if use_cache:
        body = _cached(path)
        if body is not None:
            if body.startswith(_MISS_MARKER):
                raise SourceUnavailable(body[len(_MISS_MARKER):].strip())
            return parse_openfootball(body, comp.name, comp.kind)

    url = comp.url.format(season=season)
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
            text = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        msg = f"{comp.key} {season}: HTTP {e.code}"
        if use_cache and e.code == 404:
            _store(path, f"{_MISS_MARKER} {msg}")
        raise SourceUnavailable(msg) from e
    except Exception as e:  # noqa: BLE001 -- any transport failure is the same answer
        # NOT cached. A timeout or a DNS failure is a fact about this moment,
        # and caching it would turn one bad minute into a bad day.
        raise SourceUnavailable(f"{comp.key} {season}: {type(e).__name__}") from e
    if use_cache:
        _store(path, text)
    return parse_openfootball(text, comp.name, comp.kind)


def load_season(
    season: str, keys: Iterable[str] | None = None,
) -> tuple[list[Fixture], dict[str, Any]]:
    """Every fixture Gaffer can find for a season, plus what it could not find.

    Returns the coverage report alongside the fixtures, always. A caller that
    cannot see which competitions were missing cannot tell an uncongested week
    from an unobserved one.
    """
    want = [c for c in COMPETITIONS if keys is None or c.key in set(keys)]
    fixtures: list[Fixture] = []
    coverage: dict[str, Any] = {"season": season, "found": {}, "missing": {}}
    for c in want:
        try:
            got = fetch(c, season)
        except SourceUnavailable as e:
            coverage["missing"][c.key] = str(e)
            continue
        fixtures.extend(got)
        coverage["found"][c.key] = len(got)
    coverage["complete"] = not coverage["missing"]
    return fixtures, coverage


# ---------------------------------------------------------------------------
# Joining to FPL clubs
# ---------------------------------------------------------------------------

#: openfootball's club names against the names the FPL archive uses.
#:
#: Explicit rather than fuzzy-matched, and the reason is a bug this map was
#: written after hitting: a fuzzy join that silently misses one club produces a
#: club that looks like it has NO European football, which is precisely the
#: error this whole module exists to remove. A miss must be loud.
#:
#: openfootball is not internally consistent across seasons -- "Aston Villa FC"
#: in one file and "Aston Villa" in another, "Manchester United FC" and
#: "Manchester United" -- so both forms are listed rather than normalised by a
#: rule that would have to guess. `_canon` handles the mechanical part (case,
#: the trailing FC/AFC) and this map handles the rest.
ENGLISH_CLUBS: dict[str, str] = {
    "arsenal": "Arsenal",
    "aston villa": "Aston Villa",
    "bournemouth": "Bournemouth",
    "brentford": "Brentford",
    "brighton hove albion": "Brighton",
    "brighton": "Brighton",
    "burnley": "Burnley",
    "chelsea": "Chelsea",
    "crystal palace": "Crystal Palace",
    "everton": "Everton",
    "fulham": "Fulham",
    "ipswich town": "Ipswich",
    "leeds united": "Leeds",
    "leicester city": "Leicester",
    "liverpool": "Liverpool",
    "luton town": "Luton",
    "manchester city": "Man City",
    "manchester united": "Man Utd",
    "newcastle united": "Newcastle",
    "nottingham forest": "Nott'm Forest",
    "sheffield united": "Sheffield Utd",
    "southampton": "Southampton",
    "sunderland": "Sunderland",
    "tottenham hotspur": "Spurs",
    "west ham united": "West Ham",
    "wolverhampton wanderers": "Wolves",
}

#: Suffixes and punctuation openfootball varies on. Stripping these is
#: mechanical and safe; anything beyond it belongs in the map above.
_STRIP = (" fc", " afc", " f.c.", " a.f.c.")


def _canon(name: str) -> str:
    n = name.strip().lower().replace("&", " ").replace(".", "")
    for suf in _STRIP:
        if n.endswith(suf):
            n = n[: -len(suf)]
    return " ".join(n.split())


def english_fixtures(fixtures: Iterable[Fixture]) -> list[dict[str, Any]]:
    """Fixtures involving an English club, keyed by FPL short name.

    An English club the map does not know is REPORTED, not dropped: a silently
    skipped club looks exactly like a club with no European football.
    """
    out: list[dict[str, Any]] = []
    for f in fixtures:
        for side, other, home in ((f.home, f.away, True), (f.away, f.home, False)):
            if "ENG" not in (f.home_country if home else f.away_country, ""):
                continue
            short = ENGLISH_CLUBS.get(_canon(side))
            out.append({
                "team": short, "unmapped_name": None if short else side,
                "competition": f.competition, "kind": f.kind,
                "day": f.day, "kickoff": f.kickoff,
                "opponent": other, "home": home,
            })
    return out


def congestion_index(rows: Iterable[dict[str, Any]]) -> dict[str, list[date]]:
    """`{FPL team name: [dates of its non-league fixtures]}`, sorted."""
    idx: dict[str, list[date]] = {}
    for r in rows:
        if r["team"]:
            idx.setdefault(r["team"], []).append(r["day"])
    return {k: sorted(v) for k, v in idx.items()}


def extra_matches_between(
    dates: list[date], start: date, end: date,
) -> int:
    """How many non-league matches a club played in the half-open window."""
    return sum(1 for d in dates if start <= d < end)


def rest_days_before(dates: list[date], target: date) -> int | None:
    """Days between a club's most recent non-league match and `target`."""
    prior = [d for d in dates if d < target]
    return (target - max(prior)).days if prior else None


def next_match_after(dates: list[date], target: date) -> int | None:
    """Days until the club's next non-league match after `target`."""
    later = [d for d in dates if d > target]
    return (min(later) - target).days if later else None


def availability() -> dict[str, Any]:
    """What this module can and cannot currently see. Published, not assumed."""
    return {
        "version": COMPETITIONS_VERSION,
        "competitions": [c.key for c in COMPETITIONS],
        "source": "openfootball (public domain, git, no API key)",
        "covers": "European club competitions, where the season file exists",
        "does_not_cover": [
            "the FA Cup and the League Cup -- openfootball's England repository "
            "carries them for past seasons and not for the current one",
            "international fixtures -- derivable instead from gaps between FPL "
            "deadlines, which needs no source at all",
            "any match result or projection; this module knows that a fixture "
            "exists and nothing about who wins it",
        ],
        "known_lag": (
            "community-maintained and it LAGS. Measured 2026-09-02: the "
            "2025-26 Champions League is complete (189 matches with dates and "
            "kick-off times) and no 2026-27 file exists. Complete for the "
            "archive, incomplete for the live season."),
    }
