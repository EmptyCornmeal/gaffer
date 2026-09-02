"""Availability information, and how old it is.

**The constraint, stated first, because it shapes everything below.**

There is no legitimate free route to predicted lineups. Checked 2026-09-02:

  * **Sportmonks** carries expected lineups and publishes accuracy figures for
    them (~84% in the Premier League). It is an add-on to the Growth and Pro
    tiers at **EUR 159-199 a month** and is explicitly not on the free plan.
  * **Fantasy Football Scout** and **Fantasy Football Hub** have the best
    coverage in the game and no API. Reading them mechanically means scraping a
    subscription product, which is not something to build.
  * **Apify's** Premier League lineup actor is deprecated, and it is a scraping
    platform besides.
  * **TheStatsAPI** offers a seven-day trial and is paid thereafter.

So Gaffer does not have predicted lineups, and this module does not pretend
otherwise. What it does instead is two things worth more than a stub:

1. **Use what IS free and good.** FPL's own feed carries `status`, `news`,
   `chance_of_playing_next_round` and -- crucially -- `news_added`. On
   2026-09-02 that was 165 players with a news string and 217 with a chance
   figure, and it is fast: a loan to Borussia Dortmund was stamped into the API
   at 16:01 the same afternoon.

2. **Judge its FRESHNESS, which nobody else does.** Availability news is
   deadline-sensitive in a way a projection is not. "Groin injury - unknown
   return date", stamped six weeks ago with two matches played since, is a very
   different claim from the same sentence stamped this morning. Gaffer has the
   timestamp and has never used it.

**The interface exists so a better source can be plugged in without a rewrite.**
`AvailabilityProvider` is what a predicted-lineup feed would implement;
`NoLineupProvider` is the honest default, and it reports the absence rather
than returning an empty XI that reads like "nobody is expected to start".
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

AVAILABILITY_VERSION = "availability-1"

#: FPL's own status codes, and what each means for selection.
STATUS_MEANING = {
    "a": ("available", "no flag"),
    "d": ("doubtful", "carrying a doubt; check the chance figure"),
    "i": ("injured", "will not play"),
    "s": ("suspended", "will not play"),
    "u": ("unavailable", "not registered, on loan, or has left"),
    "n": ("ineligible", "not eligible for this gameweek"),
}

#: How old a piece of availability news may be before its age is worth saying
#: out loud. A DECLARED PRESENTATION CHOICE.
#:
#: Three days, because that is roughly a press-conference cycle: news older than
#: that has usually survived a manager being asked about it, and news younger
#: than it has not yet been tested.
STALE_NEWS_DAYS = 3


@dataclass(frozen=True)
class PlayerAvailability:
    """What is known about one player's availability, and when it was known."""
    player_id: int
    name: str
    status: str
    status_label: str
    chance: int | None
    news: str
    news_added: datetime | None
    matches_since_news: int
    #: True when the news predates a fixture the player's club has since
    #: played. That is the strongest available signal that a line is stale:
    #: an "unknown return date" that has survived a match is older evidence
    #: than the match is.
    outdated_by_a_match: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.player_id, "name": self.name,
            "status": self.status, "status_label": self.status_label,
            "chance_of_playing": self.chance,
            "news": self.news or None,
            "news_added": self.news_added.isoformat() if self.news_added else None,
            "news_age_days": (
                round((datetime.now(UTC) - self.news_added).total_seconds() / 86400, 1)
                if self.news_added else None),
            "matches_since_news": self.matches_since_news,
            "outdated_by_a_match": self.outdated_by_a_match,
        }


class AvailabilityProvider(Protocol):
    """What a predicted-lineup source would implement.

    Deliberately small. Gaffer does not want a provider's opinion about
    formations or tactics; it wants, per player, a probability that he starts
    and a timestamp saying when that was believed.
    """

    name: str

    def expected_starters(self, team: str, gameweek: int) -> dict[int, float]:
        """`{player_id: probability of starting}` for one club."""
        ...

    def as_of(self) -> datetime | None:
        """When this provider last refreshed. None means it does not know."""
        ...


class NoLineupProvider:
    """The honest default.

    Returns an ABSENCE, not an empty lineup. An empty dict from a real provider
    would mean "nobody is expected to start", which is never true, and a caller
    that cannot tell the two apart will render one as the other.
    """

    name = "none"

    def expected_starters(self, team: str, gameweek: int) -> dict[int, float]:
        return {}

    def as_of(self) -> datetime | None:
        return None

    def status(self) -> dict[str, Any]:
        return {
            "available": False,
            "provider": self.name,
            "reason": ("no predicted-lineup source is configured, and none is "
                       "obtainable for free"),
            "investigated": {
                "sportmonks": "expected lineups are a EUR 159-199/month add-on "
                              "to the Growth and Pro tiers, not on the free plan",
                "fantasy_football_scout": "best coverage in the game, no API, "
                                          "and reading it mechanically means "
                                          "scraping a subscription product",
                "fantasy_football_hub": "as above",
                "apify": "the Premier League lineup actor is deprecated, and it "
                         "is a scraping platform besides",
                "thestatsapi": "seven-day trial, paid thereafter",
            },
            "what_gaffer_uses_instead": (
                "FPL's own status, news, chance_of_playing and news_added -- "
                "free, fast, and never previously judged for freshness"),
            "how_to_plug_one_in": (
                "implement AvailabilityProvider: expected_starters(team, gw) "
                "returning {player_id: p_start} and as_of() returning the "
                "refresh time. Nothing else in Gaffer needs to change."),
        }


def _matches_since(conn: sqlite3.Connection, player_id: int,
                   since: datetime | None) -> int:
    """How many of the player's club's fixtures have kicked off since `since`."""
    if since is None:
        return 0
    try:
        row = conn.execute(
            "SELECT team_id FROM players WHERE id = ?", (player_id,)).fetchone()
        if not row:
            return 0
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM fixtures "
            "WHERE (team_h = ? OR team_a = ?) AND finished = 1 "
            "AND kickoff IS NOT NULL AND kickoff > ?",
            (row["team_id"], row["team_id"], since.isoformat())).fetchone()
        return int(n["n"] or 0)
    except sqlite3.Error:
        return 0


def _parse(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def squad_availability(
    conn: sqlite3.Connection, squad: list[int],
) -> dict[str, Any]:
    """Availability for the reader's own squad, with the age of each claim.

    The age is the point. A projection is allowed to be a week old; a note
    saying a player has a groin injury is not the same fact a week later, and
    Gaffer has always had the timestamp and never read it.
    """
    if not squad:
        return {"available": False, "reason": "no squad to describe"}
    marks = ",".join("?" * len(squad))
    try:
        rows = conn.execute(
            f"SELECT id, web_name, status, chance_playing, news, news_added "
            f"FROM players WHERE id IN ({marks})", squad).fetchall()
    except sqlite3.Error:
        return {"available": False, "reason": "availability could not be read"}

    out: list[PlayerAvailability] = []
    for r in rows:
        added = _parse(r["news_added"])
        since = _matches_since(conn, r["id"], added)
        label, _ = STATUS_MEANING.get(r["status"] or "a", ("unknown", ""))
        out.append(PlayerAvailability(
            player_id=r["id"], name=r["web_name"],
            status=r["status"] or "a", status_label=label,
            chance=r["chance_playing"], news=r["news"] or "",
            news_added=added, matches_since_news=since,
            outdated_by_a_match=bool(r["news"]) and since > 0))

    flagged = [p for p in out if p.status != "a" or p.news]
    stale = [p for p in flagged if p.outdated_by_a_match]
    return {
        "available": True,
        "version": AVAILABILITY_VERSION,
        "flagged": [p.as_dict() for p in flagged],
        "clear": len(out) - len(flagged),
        "of": len(out),
        "stale_claims": [p.as_dict() for p in stale],
        "freshness_rule": (
            f"a claim is called stale once the player's club has played a "
            f"fixture since it was written. Age alone is reported too, with a "
            f"{STALE_NEWS_DAYS}-day note as a DECLARED PRESENTATION CHOICE."),
        "reading": _reading(flagged, stale),
        "lineups": NoLineupProvider().status(),
        "limitation": (
            "this is FPL's own feed, which reports injuries and suspensions "
            "and does NOT report rotation. A fit, unflagged player who will be "
            "rested looks identical here to one who will start."),
    }


def _reading(flagged, stale) -> str:
    if not flagged:
        return "nobody in the squad carries a flag or a news line."
    parts = [f"{len(flagged)} player(s) carry a flag or a news line."]
    if stale:
        names = ", ".join(p.name for p in stale)
        parts.append(
            f"{len(stale)} of those predate a fixture the club has since "
            f"played ({names}) -- the news is older evidence than the match is.")
    return " ".join(parts)
