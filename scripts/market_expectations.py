"""Market-implied team strength, read from the Football Exchange.

Gaffer's backtest rebuilds attack and defence ratings every gameweek from goals
already scored, shrunk toward last season. It is leak-free and it has one
structural blind spot: **before a ball is kicked it knows nothing except last
season**, which is the regime the initial squad -- the highest-stakes decision
of the FPL year -- is chosen in.

Bookmakers price that gameweek sharply. This module turns their opening prices
into ratings on exactly the scale `histdata.team_form_ratings` produces, so the
two can be swapped with nothing else changing.

**Nothing here imports Ledger.** It reads a frozen CSV that Ledger wrote, with
its manifest, at a pinned version. If the exchange directory is absent this
raises a named error and the caller reports BLOCKED.

**This lives in `scripts/`, not in the package.** `pyproject.toml` packages
`src/` only, so nothing here is importable from an installed Gaffer and no
part of the shipped product can reach a Ledger artifact even by accident.
E2 was REJECTED; rejected experiments do not get to sit in the runtime
surface waiting to be imported by someone who assumes they were adopted.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from gaffer.model import features as F

EXCHANGE = Path.home() / "Projects" / "Football Exchange"

#: Same shrinkage the control uses, so the comparison is about information.
SHRINK_K = 5.0


class ExpectationsUnavailable(RuntimeError):
    """The market export is missing. A BLOCKED verdict, not a crash."""


@dataclass(frozen=True)
class MarketFixture:
    season: str
    kickoff: date
    home: str
    away: str
    lam_home: float
    lam_away: float


def load(version: str, season: str) -> tuple[list[MarketFixture], dict]:
    root = EXCHANGE / "ledger" / "market_team_expectations" / version
    if not (root / "manifest.json").exists():
        raise ExpectationsUnavailable(
            f"no market export at {root}. Run Ledger's "
            f"lab.runs.export_market_expectations, or report BLOCKED."
        )
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if int(manifest.get("schema_version", 0)) != 1:
        raise ExpectationsUnavailable(
            f"market_team_expectations schema {manifest.get('schema_version')} "
            f"is not supported by this reader."
        )
    rows: list[MarketFixture] = []
    with (root / "data.csv").open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            if raw["season"] != season:
                continue
            rows.append(MarketFixture(
                season=raw["season"],
                kickoff=date.fromisoformat(raw["kickoff_date"]),
                home=raw["home"], away=raw["away"],
                lam_home=float(raw["lam_home"]),
                lam_away=float(raw["lam_away"]),
            ))
    return rows, manifest


def fixture_gameweeks(hist) -> dict[tuple[str, str], int]:
    """(home club, away club) -> gameweek, from the archive's own fixtures.

    Built from rows where ``was_home`` is true, so the pairing comes from the
    same source as everything else rather than from a second fixture list that
    could disagree with it.
    """
    frame = hist.frame
    names = dict(zip(hist.teams["id"], hist.teams["name"], strict=False))
    out: dict[tuple[str, str], int] = {}
    home_rows = frame[frame["was_home"] == 1].drop_duplicates(["team_id", "fixture"])
    for row in home_rows.itertuples(index=False):
        home = names.get(int(row.team_id))
        away = names.get(int(row.opponent_team))
        if home and away:
            out[(home, away)] = int(row.GW)
    return out


def market_context(
    hist, decision_gw: int, fixtures: list[MarketFixture],
    *, include_current_round: bool,
) -> F.TeamContext:
    """A TeamContext built from prices instead of results.

    ``include_current_round`` decides which question is being asked, and the
    two are genuinely different:

    ``True``   every quote published by the deadline, INCLUDING the round being
               projected. This is what a manager actually has in front of them,
               and it is the only variant in which the market can say anything
               at all about gameweek 1.
    ``False``  completed fixtures only, matching the control's information set
               exactly. Answers the narrower question of whether prices are a
               better summary of the PAST than goals are.

    Neither is a leak: an opening quote precedes the deadline. But only the
    first is a fair description of what is available, and only the second is a
    like-for-like contest with the control, so both are run.
    """
    gameweeks = fixture_gameweeks(hist)
    ids = {name: int(tid) for tid, name in
           zip(hist.teams["id"], hist.teams["name"], strict=False)}
    prior = hist.prior_rates()

    scored: dict[str, dict[int, list[float]]] = {
        "home": defaultdict(list), "away": defaultdict(list)}
    conceded: dict[str, dict[int, list[float]]] = {
        "home": defaultdict(list), "away": defaultdict(list)}
    against_all: dict[int, list[float]] = defaultdict(list)

    for fx in fixtures:
        gw = gameweeks.get((fx.home, fx.away))
        if gw is None:
            continue
        if include_current_round:
            if gw > decision_gw:
                continue
        elif gw >= decision_gw:
            continue
        home_id, away_id = ids.get(fx.home), ids.get(fx.away)
        if home_id is None or away_id is None:
            continue
        scored["home"][home_id].append(fx.lam_home)
        conceded["home"][home_id].append(fx.lam_away)
        scored["away"][away_id].append(fx.lam_away)
        conceded["away"][away_id].append(fx.lam_home)
        against_all[home_id].append(fx.lam_away)
        against_all[away_id].append(fx.lam_home)

    teams = [int(t) for t in hist.teams["id"]]
    out: dict[str, dict[int, float]] = {
        "att_home": {}, "att_away": {}, "def_home": {}, "def_away": {}}
    for venue in ("home", "away"):
        for team in teams:
            gf = scored[venue].get(team, [])
            ga = conceded[venue].get(team, [])
            n = float(len(gf))
            p_gf, p_ga = prior.for_team(team)
            denom = n + SHRINK_K
            att_rate = (sum(gf) + SHRINK_K * p_gf) / denom if denom > 0 else p_gf
            def_rate = (sum(ga) + SHRINK_K * p_ga) / denom if denom > 0 else p_ga
            # Identical conversion and orientation to team_form_ratings:
            # conceding less gives a HIGHER defence rating.
            out[f"att_{venue}"][team] = 1000.0 * att_rate / max(prior.league_gf, 1e-6)
            out[f"def_{venue}"][team] = 1000.0 * prior.league_ga / max(def_rate, 1e-6)

    # The market's own read of goals conceded per match, replacing the control's
    # keeper/defender xGC proxy. Same units, different instrument.
    team_xgc = {
        team: sum(values) / len(values)
        for team, values in against_all.items() if values
    }
    return F.TeamContext.from_ratings(
        att_home=out["att_home"], att_away=out["att_away"],
        def_home=out["def_home"], def_away=out["def_away"],
        team_xgc=team_xgc,
    )
