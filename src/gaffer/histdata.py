"""Adapter over the audited historical dataset (vaastav merged_gw CSVs).

Everything the backtest knows about past seasons comes through here, so the
leakage contract has exactly one place to be enforced. The adapter's job is to
split each season into:

  * **pre-deadline features** — season-to-date aggregates built with ``shift(1)``
    so gameweek G never sees its own result, plus genuinely-known-in-advance
    fields (price, venue, opponent, ownership).
  * **post-match targets** — realised points and the raw stat lines, used only
    for scoring.

The dataset is required, not optional: if the CSVs are missing the backtest
fails loudly rather than substituting current-season data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from gaffer import config, leakage

#: CSV position labels -> the model's position codes.
POSITION_MAP = {"GK": "GKP", "GKP": "GKP", "DEF": "DEF", "MID": "MID",
                "AM": "MID", "FWD": "FWD"}

#: Columns the adapter exposes as safe pre-deadline features.
FEATURE_COLUMNS = (
    "element", "GW", "position", "team", "opponent_team", "was_home", "value",
    "selected", "min_td", "starts_td", "xg90_td", "xa90_td", "defcon90_td",
    "pts_td", "games_td", "base_minutes", "base_starts", "base_xg90", "base_xa90",
    "base_defcon90",
    # Which season base_* came from. Names a season already finished before this
    # one began, so it is pre-deadline by construction.
    "base_season",
)

#: Columns used only as evaluation targets.
TARGET_COLUMNS = ("total_points", "minutes")


class MissingHistoryError(FileNotFoundError):
    """Raised when a required historical season is not present on disk."""


#: Promoted sides have no top-flight prior. Historically they score fewer and
#: concede more than the league average; these multipliers are applied to the
#: league mean rather than fabricating a team-specific record.
PROMOTED_GF_FACTOR = 0.78
PROMOTED_GA_FACTOR = 1.25


@dataclass
class PriorRates:
    """Season-opening goals-for / goals-against beliefs, per team."""

    league_gf: float
    league_ga: float
    by_team: dict[int, tuple[float, float]]
    promoted: set[int]

    def for_team(self, team_id: int) -> tuple[float, float]:
        if team_id in self.by_team:
            return self.by_team[team_id]
        if team_id in self.promoted:
            return (self.league_gf * PROMOTED_GF_FACTOR,
                    self.league_ga * PROMOTED_GA_FACTOR)
        return (self.league_gf, self.league_ga)


def _prior_rates(
    season: str, team_ids: list[int], names: dict[int, str]
) -> PriorRates:
    """Per-team goals for/against from the PREVIOUS season, matched by name.

    A team with no previous-season record is treated as promoted and given a
    documented league-relative prior — never a fabricated team-specific one.
    """
    prev = _prior_season(season)
    path = config.HISTORY_DIR / f"merged_gw_{prev}.csv"
    league_gf = league_ga = 1.4  # PL long-run goals per team per match
    if not path.exists():
        return PriorRates(league_gf, league_ga, {}, set(team_ids))

    df = pd.read_csv(path)
    if "team_h_score" not in df or "was_home" not in df:
        return PriorRates(league_gf, league_ga, {}, set(team_ids))
    per_fx = df[df["minutes"] > 0].drop_duplicates(["team", "fixture"])
    gf = per_fx.apply(
        lambda r: r["team_h_score"] if r["was_home"] else r["team_a_score"], axis=1)
    ga = per_fx.apply(
        lambda r: r["team_a_score"] if r["was_home"] else r["team_h_score"], axis=1)
    per_fx = per_fx.assign(_gf=gf, _ga=ga)
    agg = per_fx.groupby("team").agg(gf=("_gf", "mean"), ga=("_ga", "mean"))
    if len(agg):
        league_gf = float(agg["gf"].mean())
        league_ga = float(agg["ga"].mean())

    by_team: dict[int, tuple[float, float]] = {}
    promoted: set[int] = set()
    for tid in team_ids:
        name = names.get(tid)
        if name in agg.index:
            by_team[tid] = (float(agg.loc[name, "gf"]), float(agg.loc[name, "ga"]))
        else:
            promoted.add(tid)
    return PriorRates(league_gf, league_ga, by_team, promoted)


@dataclass
class SeasonHistory:
    """One season, split into pre-deadline features and post-match targets."""

    season: str
    frame: pd.DataFrame          # one row per (player, fixture)
    teams: pd.DataFrame          # team ratings for the season
    name_to_id: dict[str, int]

    # -- team context ------------------------------------------------------
    def team_ratings(self) -> dict[str, dict[int, float]]:
        t = self.teams
        return {
            "att_home": dict(zip(t["id"], t["strength_attack_home"], strict=False)),
            "att_away": dict(zip(t["id"], t["strength_attack_away"], strict=False)),
            "def_home": dict(zip(t["id"], t["strength_defence_home"], strict=False)),
            "def_away": dict(zip(t["id"], t["strength_defence_away"], strict=False)),
        }

    # -- pre-deadline team strength (T-12) ---------------------------------
    def team_form_ratings(
        self, before_gw: int, prior: PriorRates | None = None, shrink_k: float = 5.0
    ) -> dict[str, dict[int, float]]:
        """Attack/defence ratings from matches played strictly before ``before_gw``.

        The season-end ``teams_*.csv`` ratings encode how the season turned out,
        so using them for GW3 tells the model the future. These are rebuilt each
        gameweek from results already played, shrunk toward a prior so early
        gameweeks are not driven by one fixture.

        Returned on the ~1000 scale FPL uses in-season, with the same
        orientation: a HIGHER defence rating means a HARDER opponent to score
        against.
        """
        df = self.frame
        played = df[(df["GW"] < before_gw) & (df["minutes"] > 0)]
        prior = prior or self.prior_rates()

        scored, conceded, games = {}, {}, {}
        for venue, home in (("home", True), ("away", False)):
            sub = played[played["was_home"] == home] if len(played) else played
            if len(sub):
                # One row per (team, fixture): scores repeat across a team's players.
                per_fx = sub.drop_duplicates(["team_id", "fixture"])
                gf = per_fx.groupby("team_id")["team_h_score" if home else "team_a_score"]
                ga = per_fx.groupby("team_id")["team_a_score" if home else "team_h_score"]
                scored[venue] = gf.sum().to_dict()
                conceded[venue] = ga.sum().to_dict()
                games[venue] = per_fx.groupby("team_id").size().to_dict()
            else:
                scored[venue], conceded[venue], games[venue] = {}, {}, {}

        teams = [int(t) for t in self.teams["id"]]
        out: dict[str, dict[int, float]] = {
            "att_home": {}, "att_away": {}, "def_home": {}, "def_away": {}}
        for venue in ("home", "away"):
            for t in teams:
                n = float(games[venue].get(t, 0))
                gf = float(scored[venue].get(t, 0.0))
                ga = float(conceded[venue].get(t, 0.0))
                p_gf, p_ga = prior.for_team(t)
                # Empirical-Bayes shrink toward the prior: k pseudo-matches.
                # With no matches and no shrinkage there is nothing to average,
                # so fall back to the prior rather than dividing by zero.
                denom = n + shrink_k
                if denom <= 0:
                    att_rate, def_rate = p_gf, p_ga
                else:
                    att_rate = (gf + shrink_k * p_gf) / denom
                    def_rate = (ga + shrink_k * p_ga) / denom
                out[f"att_{venue}"][t] = 1000.0 * att_rate / max(prior.league_gf, 1e-6)
                # Inverted: conceding less = a HIGHER (stronger) defence rating.
                out[f"def_{venue}"][t] = 1000.0 * prior.league_ga / max(def_rate, 1e-6)
        return out

    def prior_rates(self) -> PriorRates:
        """Season-opening beliefs, from the previous season where available."""
        return _prior_rates(self.season, [int(t) for t in self.teams["id"]],
                            dict(zip(self.teams["id"], self.teams["name"], strict=False)))

    def team_xgc_to_date(self, before_gw: int) -> dict[int, float]:
        """Per-team goals-conceded-per-90 proxy from strictly earlier gameweeks.

        Mirrors ``TeamContext.build``'s minutes-weighted xGC over keepers and
        defenders, but only over matches already played — so it is leak-free.
        """
        df = self.frame
        prior = df[(df["GW"] < before_gw) & df["pos"].isin(("GKP", "DEF"))]
        prior = prior[prior["minutes"] > 0]
        if prior.empty or "expected_goals_conceded" not in prior:
            return {}
        # Minutes-weighted mean of per-match xGC, matching TeamContext.build's
        # weighting over keepers and defenders.
        w = prior["expected_goals_conceded"] * prior["minutes"]
        wsum = w.groupby(prior["team_id"]).sum()
        msum = prior["minutes"].groupby(prior["team_id"]).sum()
        out: dict[int, float] = {}
        for tid, m in msum.items():
            if m and m > 0:
                out[int(tid)] = float(wsum.loc[tid]) / float(m)
        return out


def _require(path: Path, season: str) -> Path:
    if not path.exists():
        raise MissingHistoryError(
            f"historical dataset for {season} not found at {path}. "
            "Run `python scripts/fetch_history.py` to download it. The backtest "
            "will not substitute current-season data."
        )
    return path


def _prior_season(season: str) -> str:
    """'2024-25' -> '2023-24'."""
    start = int(season.split("-")[0])
    return f"{start - 1}-{str(start)[-2:]}"


def _season_to_date(df: pd.DataFrame) -> pd.DataFrame:
    """Attach shift(1) season-to-date aggregates. Never sees the current row."""
    df = df.sort_values(["element", "GW", "fixture"]).copy()
    g = df.groupby("element")

    def cum(col: str) -> pd.Series:
        if col not in df:
            return pd.Series(0.0, index=df.index)
        return g[col].transform(lambda x: x.shift(1).cumsum())

    df["min_td"] = cum("minutes").fillna(0.0)
    df["starts_td"] = cum("starts").fillna(0.0)
    df["pts_td"] = cum("total_points").fillna(0.0)
    df["games_td"] = g.cumcount()
    per90 = np.where(df["min_td"] > 0, 90.0 / df["min_td"].replace(0, np.nan), 0.0)
    df["xg90_td"] = (cum("expected_goals") * per90).fillna(0.0)
    df["xa90_td"] = (cum("expected_assists") * per90).fillna(0.0)
    if "defensive_contribution" in df:
        df["defcon90_td"] = (cum("defensive_contribution") * per90).fillna(0.0)
    else:
        df["defcon90_td"] = 0.0
    return df


def _prior_season_baseline(season: str) -> pd.DataFrame:
    """Per-player prior-season totals -> the model's ``base_*`` inputs.

    Exercises the production last-season prior path instead of feeding it zeros.
    Absent for the earliest season available; callers get an empty frame and the
    model falls back to its price prior, exactly as it does live.
    """
    prev = _prior_season(season)
    path = config.HISTORY_DIR / f"merged_gw_{prev}.csv"
    if not path.exists():
        return pd.DataFrame(
            columns=["name", "base_minutes", "base_starts", "base_xg90",
                     "base_xa90", "base_defcon90", "base_season"]
        )
    df = pd.read_csv(path)
    agg = {"minutes": "sum", "total_points": "sum"}
    for c in ("starts", "expected_goals", "expected_assists", "defensive_contribution"):
        if c in df:
            agg[c] = "sum"
    tot = df.groupby("name", as_index=False).agg(agg)
    mins = tot["minutes"].replace(0, np.nan)
    per90 = 90.0 / mins
    out = pd.DataFrame({"name": tot["name"]})
    out["base_minutes"] = tot["minutes"].fillna(0)
    out["base_starts"] = tot.get("starts", pd.Series(0, index=tot.index)).fillna(0)
    out["base_xg90"] = (tot.get("expected_goals", 0) * per90).fillna(0.0)
    out["base_xa90"] = (tot.get("expected_assists", 0) * per90).fillna(0.0)
    out["base_defcon90"] = (
        tot.get("defensive_contribution", pd.Series(0, index=tot.index)) * per90
    ).fillna(0.0)
    # Prior seasons with <300 minutes are not a usable baseline (same rule the
    # live enrichment applies in ingest.enrich_history).
    short = out["base_minutes"] < config.BASE_SAMPLE_MINUTES
    out.loc[short, ["base_xg90", "base_xa90", "base_defcon90"]] = 0.0
    out.loc[short, ["base_minutes", "base_starts"]] = 0
    # Which season the baseline came from, in FPL's own '2023/24' notation. The
    # archive has the same gap the live API does — merged_gw files before 2022-23
    # carry no `starts` or `expected_*` columns, so `tot.get(...)` above yields
    # zeros that mean "not recorded". Passing the season through lets the
    # projection apply exactly the test it applies live, rather than the backtest
    # silently exercising a different branch from the one that ships.
    start = prev.split("-")[0]
    out["base_season"] = f"{start}/{prev.split('-')[1]}" if "-" in prev else prev
    return out


def load_season(season: str) -> SeasonHistory:
    """Load one season with leak-free features attached."""
    gw_path = _require(config.HISTORY_DIR / f"merged_gw_{season}.csv", season)
    team_path = _require(config.HISTORY_DIR / f"teams_{season}.csv", season)

    df = pd.read_csv(gw_path)
    teams = pd.read_csv(team_path)
    name_to_id = dict(zip(teams["name"], teams["id"], strict=False))

    df["pos"] = df["position"].map(lambda p: POSITION_MAP.get(str(p), "MID"))
    df["team_id"] = df["team"].map(name_to_id)
    if "fixture" not in df:
        df["fixture"] = df.groupby(["element", "GW"]).cumcount()
    df = _season_to_date(df)

    base = _prior_season_baseline(season)
    if not base.empty:
        df = df.merge(base, on="name", how="left")
    for c in ("base_minutes", "base_starts", "base_xg90", "base_xa90", "base_defcon90"):
        if c not in df:
            df[c] = 0.0
        df[c] = df[c].fillna(0.0)
    # Unmatched players get "", which the projection reads as unrecorded rather
    # than as a season that could not report the stat.
    if "base_season" not in df:
        df["base_season"] = ""
    df["base_season"] = df["base_season"].fillna("")

    return SeasonHistory(season=season, frame=df, teams=teams, name_to_id=name_to_id)


def assert_features_leak_free(columns) -> None:
    """Enforce the leakage contract on whatever the backtest calls a feature."""
    leakage.assert_no_leakage(columns, context="backtest features")
