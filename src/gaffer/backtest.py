"""Backtest the SHIPPED projection on a held-out season.

The previous harness scored a substitute: it injected ``ml.py``'s fixture
multiplier (no gamma, a different clamp) and a different clean-sheet formula,
zeroed the last-season prior, DEFCON and availability, and filtered the
evaluation population on ``minutes > 0`` — i.e. it knew who had played before
picking a team. Every published accuracy number came from that path.

This version calls ``projection._project_one_fixture`` through a real
``TeamContext``, keeps zero-minute outcomes in the population, reconstructs each
decision point from pre-deadline information only, and evaluates horizons 1-6.

It establishes a baseline. It does not tune anything.

    python -m gaffer.backtest                 # writes data/backtest.json
    python -m gaffer.backtest --horizons 1 3  # faster subset
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from gaffer import config, histdata, leakage
from gaffer.io import write_json_atomic
from gaffer.model import features as F
from gaffer.model import projection

#: Backtest artifact schema. Bump when the shape changes; the Accuracy page
#: refuses to render anything it does not recognise rather than mis-labelling it.
#:   1 = legacy (ml-vs-heuristic, minutes>0 filtered) — REJECTED, never rendered
#:   2 = corrected harness, per-horizon
#:   3 = adds `baselines` (frozen model comparisons) and `ablations`
#:   4 = withdraws the `fpl_xp` / `ensemble` columns. They were computed from the
#:       archive's `xP`, which is inadmissible (see WITHDRAWN_BASELINES). Adds
#:       `withdrawn_baselines` and `rejected_models` so the withdrawal is visible
#:       rather than silent.
#:   5 = replaces the single `rejected_models` blob with `model_candidates`: one
#:       record per candidate, each with its own decision. Version 4 reported GBM
#:       only and concluded "trained models lose every decision metric", which is
#:       false — ridge beat the heuristic at h=1. `rejected` and `inconclusive`
#:       are different findings and the artifact now carries both.
SCHEMA_VERSION = 5

TEST_SEASON = "2024-25"
#: Decision gameweeks evaluated. GW1 has no season-to-date history, so the model
#: runs on its prior/price path there — which is exactly the live GW1 regime and
#: is therefore included rather than skipped.
FIRST_DECISION_GW = 2
HORIZONS = (1, 2, 3, 4, 5, 6)
SQUAD_SIZE = 15
BUDGET = 1000  # tenths of a million, as the API reports prices
CLUB_LIMIT = 3
QUOTA = {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GKP": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}


# ---------------------------------------------------------------------------
# The production projection, driven from historical rows
# ---------------------------------------------------------------------------

def _player_inputs(row: Any) -> dict[str, Any]:
    """Map one pre-deadline feature row onto the model's player dict.

    Only season-to-date (shift(1)) aggregates and known-in-advance fields. The
    keys are exactly what the live pipeline passes.
    """
    return {
        "position": row.pos,
        "minutes": float(row.min_td),
        "starts": float(row.starts_td),
        "base_minutes": float(row.base_minutes),
        "base_starts": float(row.base_starts),
        "base_xg90": float(row.base_xg90),
        "base_xa90": float(row.base_xa90),
        "price": float(row.value),
        "xg_per_90": float(row.xg90_td),
        "xa_per_90": float(row.xa90_td),
        "defcon_per_90": float(row.defcon90_td),
        "team_id": int(row.team_id) if not pd.isna(row.team_id) else 0,
    }


def project_rows(
    frame: pd.DataFrame, ctx: F.TeamContext, games_played: int
) -> pd.Series:
    """Run the real projection over historical fixtures.

    A player with two fixtures in a gameweek (DGW) is summed; a player with none
    (BGW) never reaches here and scores zero by construction — the same shape
    ``projection.project`` produces live.
    """
    out = np.zeros(len(frame))
    for i, row in enumerate(frame.itertuples(index=False)):
        player = _player_inputs(row)
        fx = F.Fixture(
            gw=int(row.GW), opponent_id=int(row.opponent_team),
            at_home=bool(row.was_home), fdr=3,
        )
        # Availability: the historical dataset carries no status/chance column,
        # so every player resolves to the model's "available" branch. This is a
        # documented limitation, not a silent substitution — see `limitations`.
        avail = projection._availability("a", None)
        parts = projection._project_one_fixture(player, fx, ctx, avail, games_played)
        out[i] = parts["exp_points"]
    return pd.Series(out, index=frame.index)


def _context_for(
    hist: histdata.SeasonHistory, decision_gw: int, *, season_end_ratings: bool = False
) -> F.TeamContext:
    """A real TeamContext built from information available before ``decision_gw``.

    ``season_end_ratings=True`` restores the Batch 2 behaviour (the dataset's
    end-of-season snapshot applied to every gameweek) so the two can be compared
    directly. It is a leak and is never the default.
    """
    r = (hist.team_ratings() if season_end_ratings
         else hist.team_form_ratings(decision_gw))
    return F.TeamContext.from_ratings(
        att_home=r["att_home"], att_away=r["att_away"],
        def_home=r["def_home"], def_away=r["def_away"],
        team_xgc=hist.team_xgc_to_date(decision_gw),
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _mae(pred: pd.Series, actual: pd.Series) -> float:
    return float((pred - actual).abs().mean())


def _rank_corr(df: pd.DataFrame, col: str, target: str = "actual") -> float:
    corrs = []
    for _, grp in df.groupby("target_gw"):
        if len(grp) >= 10 and grp[col].std() > 0:
            c = grp[col].rank().corr(grp[target].rank())
            if pd.notna(c):
                corrs.append(c)
    return float(np.mean(corrs)) if corrs else 0.0


def _calibration(df: pd.DataFrame, col: str, bins: int = 8) -> list[dict[str, float]]:
    d = df[[col, "actual"]].dropna()
    if len(d) < bins * 5 or d[col].std() == 0:
        return []
    try:
        d = d.assign(_b=pd.qcut(d[col], bins, duplicates="drop"))
    except ValueError:
        return []
    out = []
    for _, g in d.groupby("_b", observed=True):
        out.append({
            "pred": round(float(g[col].mean()), 2),
            "actual": round(float(g["actual"].mean()), 2),
            "haul_rate": round(float((g["actual"] >= 10).mean()) * 100, 1),
            "n": int(len(g)),
        })
    return sorted(out, key=lambda x: x["pred"])


def _calibration_by_position(df: pd.DataFrame, col: str) -> dict[str, list]:
    return {
        pos: _calibration(grp, col, bins=5)
        for pos, grp in df.groupby("pos") if len(grp) >= 40
    }


def _select_squad(grp: pd.DataFrame, col: str) -> list[int] | None:
    """A legal 15 maximising ``col`` under budget, quota and the club limit.

    An exact MILP, so "the model's team" is a team you could actually have
    fielded — the previous harness picked an unconstrained top-11 leaderboard
    costing up to £101.5m with four players from one club.
    """
    import pulp

    ids = list(grp.index)
    if not ids:
        return None
    prob = pulp.LpProblem("bt_squad", pulp.LpMaximize)
    x = {i: pulp.LpVariable(f"x{i}", cat="Binary") for i in ids}
    proj = grp[col].to_dict()
    price = grp["value"].to_dict()
    pos = grp["pos"].to_dict()
    club = grp["team_id"].to_dict()

    prob += pulp.lpSum(x[i] * float(proj[i]) for i in ids)
    prob += pulp.lpSum(x[i] for i in ids) == SQUAD_SIZE
    for p, n in QUOTA.items():
        prob += pulp.lpSum(x[i] for i in ids if pos[i] == p) == n
    prob += pulp.lpSum(x[i] * float(price[i]) for i in ids) <= BUDGET
    for c in {club[i] for i in ids}:
        prob += pulp.lpSum(x[i] for i in ids if club[i] == c) <= CLUB_LIMIT
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[prob.status] != "Optimal":
        return None
    return [i for i in ids if x[i].value() and x[i].value() > 0.5]


def _best_xi(grp: pd.DataFrame, squad: list[int], col: str) -> list[int]:
    """The highest-``col`` legal XI from a 15 (formation minima and maxima)."""
    sub = grp.loc[squad].sort_values(col, ascending=False)
    chosen: list[int] = []
    counts = {p: 0 for p in XI_MIN}
    for p in XI_MIN:
        for idx in sub.index[sub["pos"] == p][: XI_MIN[p]]:
            chosen.append(idx)
            counts[p] += 1
    for idx, row in sub.iterrows():
        if len(chosen) >= 11:
            break
        if idx in chosen:
            continue
        if counts[row["pos"]] < XI_MAX[row["pos"]]:
            chosen.append(idx)
            counts[row["pos"]] += 1
    return chosen


def _decision_metrics(df: pd.DataFrame, col: str) -> dict[str, Any]:
    """Constrained squad/XI/captain performance and regret vs perfect hindsight."""
    xi_pts, xi_best, cap_pts, cap_best, cap_hits, weeks = [], [], [], [], 0, 0
    for _, grp in df.groupby("target_gw"):
        g = grp.dropna(subset=["value", "team_id"]).copy()
        if len(g) < 40:
            continue
        squad = _select_squad(g, col)
        if squad is None:
            continue
        xi = _best_xi(g, squad, col)
        if len(xi) != 11:
            continue
        weeks += 1
        xi_pts.append(float(g.loc[xi, "actual"].sum()))

        # Perfect-hindsight legal XI, for regret.
        ideal_squad = _select_squad(g, "actual")
        if ideal_squad is not None:
            ideal_xi = _best_xi(g, ideal_squad, "actual")
            xi_best.append(float(g.loc[ideal_xi, "actual"].sum()))

        cap = g.loc[xi, col].idxmax()
        cap_pts.append(float(g.loc[cap, "actual"]))
        best_cap = g.loc[xi, "actual"].idxmax()
        cap_best.append(float(g.loc[best_cap, "actual"]))
        cap_hits += int(cap == best_cap)

    if not weeks:
        return {}
    return {
        "gameweeks_scored": weeks,
        "xi_points_per_gw": round(float(np.mean(xi_pts)), 1),
        "xi_regret_per_gw": (
            round(float(np.mean(xi_best) - np.mean(xi_pts)), 1) if xi_best else None
        ),
        "captain_points_per_gw": round(float(np.mean(cap_pts)), 2),
        "captain_regret_per_gw": round(float(np.mean(cap_best) - np.mean(cap_pts)), 2),
        "captain_accuracy_pct": round(100.0 * cap_hits / weeks, 1),
    }


def _transfer_regret(df: pd.DataFrame, col: str) -> dict[str, Any]:
    """One free transfer per week, chosen on projection, vs holding the squad.

    A deliberately simple sequence: it measures whether the projection's transfer
    advice beats doing nothing, which is the cheapest honest test of squad
    continuity that this dataset supports.
    """
    gws = sorted(df["target_gw"].unique())
    if len(gws) < 3:
        return {}
    first = df[df["target_gw"] == gws[0]].dropna(subset=["value", "team_id"])
    squad = _select_squad(first, col)
    if squad is None:
        return {}
    held = {first.loc[i, "element"] for i in squad}
    active, passive = set(held), set(held)
    active_pts, passive_pts = 0.0, 0.0

    for gw in gws[1:]:
        g = df[df["target_gw"] == gw].dropna(subset=["value", "team_id"])
        by_el = g.set_index("element")
        for name, holding in (("a", active), ("p", passive)):
            avail = [e for e in holding if e in by_el.index]
            if len(avail) < 11:
                continue
            sub = by_el.loc[avail]
            xi = _best_xi(sub.reset_index().set_index("element"), avail, col)
            pts = float(by_el.loc[xi, "actual"].sum())
            if name == "a":
                active_pts += pts
            else:
                passive_pts += pts
        # One transfer for the active squad: best projected in for worst out.
        cand = by_el[~by_el.index.isin(active)]
        mine = by_el[by_el.index.isin(active)]
        if not cand.empty and not mine.empty:
            out_el = mine[col].idxmin()
            same_pos = cand[cand["pos"] == mine.loc[out_el, "pos"]]
            if not same_pos.empty:
                in_el = same_pos[col].idxmax()
                if same_pos.loc[in_el, col] > mine.loc[out_el, col]:
                    active.discard(out_el)
                    active.add(in_el)
    return {
        "with_transfers": round(active_pts, 1),
        "hold_squad": round(passive_pts, 1),
        "gain": round(active_pts - passive_pts, 1),
        "gameweeks": len(gws) - 1,
    }


# ---------------------------------------------------------------------------
# T-26 — what was withdrawn, and what was tried and rejected
# ---------------------------------------------------------------------------

#: Baselines this dataset cannot measure. Recorded rather than deleted: a number
#: that quietly disappears looks like it was never there.
WITHDRAWN_BASELINES = {
    "fpl_xp": {
        "withdrawn_in_schema": 4,
        "previously_reported": {"rank_corr_h1": 0.760, "mae_h1": 0.942,
                                "xi_points_per_gw": 84.2},
        "reason": "computed from the archive's `xP` column. The archive cannot "
                  "certify this value as the pre-deadline forecast managers saw, "
                  "and the upstream dataset explicitly warns that it may contain "
                  "post-match information. It is therefore inadmissible for this "
                  "backtest.",
        "provenance": "The upstream data dictionary states `xP` is FPL's "
                      "`ep_this`, scraped AFTER each gameweek has ended, with an "
                      "undocumented update cadence, and advises shifting or "
                      "dropping it. That is the grounds for exclusion.",
        "corroboration": "Not proof of timing, but consistent with it: within a "
                         "player, across single-fixture gameweeks he completed "
                         "60+ minutes of, `xP` deviates by sd~1.75 points and "
                         "correlates +0.40..+0.47 with the deviation in what he "
                         "then scored, against +0.09 and -0.13 for two quantities "
                         "that are pre-deadline by construction. Reproduce with "
                         "`python -m gaffer.backtest --xp-diagnostic`.",
    },
    "ensemble": {
        "withdrawn_in_schema": 4,
        "previously_reported": {"rank_corr_h1": 0.699, "mae_h1": 1.043,
                                "xi_points_per_gw": 84.6},
        "reason": "0.7 of it was `fpl_xp`, so it inherited the same inflation.",
    },
    "consequence": "EP_NEXT_BLEND_WEIGHT (0.7) was chosen against these numbers. "
                   "The weight is UNCHANGED — moving it on withdrawn evidence "
                   "would be as unfounded as setting it was — but it is now "
                   "labelled a policy choice, not a fitted one. It becomes "
                   "fittable once projection_snapshots and player_gw accumulate "
                   "live ep_next values against real outcomes.",
}

#: T-26's experiment, one record per candidate.
#:
#: The first version of this block reported GBM only and concluded "trained
#: models lose every decision metric". That is false as written: ridge beat the
#: heuristic at h=1 on legal-XI points and on captaincy. It is not a *win* — the
#: interval spans zero and it does not hold up past h=1 — but "not selected" and
#: "rejected" are different findings, and flattening them hid real evidence.
MODEL_CANDIDATES = {
    "evaluation_version": "candidates-1.0",
    "outcome": "no trained points model ships",
    "protocol": "Features from the same leakage-checked adapter; trained on "
                "2022-23 + 2023-24 (2021-22 supplies 2022-23's priors); selected "
                "on 2023-24; reported once on 2024-25, untouched until selection "
                "closed. Decision metric = legal 15 under budget, quota and the "
                "three-per-club limit, best legal XI from it, scored on what "
                "actually happened; paired by gameweek against the shipped "
                "heuristic with a 4000-sample bootstrap.",
    "heuristic_reference": {
        "xi_points_per_gw": {"1": 50.86, "2": 52.58, "3": 51.03,
                             "4": 51.15, "5": 48.91, "6": 49.47},
        "captain_accuracy_pct_h1": 29.7,
        "captain_regret_per_gw_h1": 5.59,
        "rank_corr": {"1": 0.440, "3": 0.413, "6": 0.397},
        "mae": {"1": 1.566, "3": 1.585, "6": 1.605},
    },
    "candidates": [
        {
            "candidate": "gbm",
            "label": "Gradient-boosted trees",
            "detail": "sklearn HistGradientBoostingRegressor, 300 iterations, "
                      "depth 6, leaf 60, l2 1.0, fixed seed",
            "decision": "rejected",
            "reason": "Worse than the shipped heuristic on legal-XI points at "
                      "every horizon, and far worse on captaincy (13.5% accuracy "
                      "against 29.7%). Four of six paired intervals exclude zero "
                      "against it.",
            "worse_at_every_horizon": True,
            "per_horizon": {
                "1": {"candidate_xi": 46.22, "diff": -4.65, "ci95": [-10.03, 0.84],
                      "p_better": 0.048},
                "2": {"candidate_xi": 46.36, "diff": -6.22, "ci95": [-11.92, -0.05],
                      "p_better": 0.024},
                "3": {"candidate_xi": 44.97, "diff": -6.06, "ci95": [-11.71, -0.31],
                      "p_better": 0.020},
                "4": {"candidate_xi": 42.97, "diff": -8.18, "ci95": [-13.88, -2.41],
                      "p_better": 0.002},
                "5": {"candidate_xi": 44.76, "diff": -4.15, "ci95": [-9.33, 1.12],
                      "p_better": 0.069},
                "6": {"candidate_xi": 44.78, "diff": -4.69, "ci95": [-12.0, 2.69],
                      "p_better": 0.105},
            },
            "statistical": {"rank_corr": {"1": 0.638, "3": 0.610, "6": 0.589},
                            "mae": {"1": 1.14, "3": 1.16, "6": 1.19}},
            "captain_accuracy_pct_h1": 13.5,
            "captain_regret_per_gw_h1": 6.32,
            "limitations": [
                "Wins every statistical metric and loses every decision metric. "
                "58% of rows are zero-minute, so MAE and rank correlation mostly "
                "reward predicting who does NOT play; a squad needs the top tail, "
                "and a squared-error objective on a zero-inflated target "
                "regresses hard to the mean.",
            ],
        },
        {
            "candidate": "ridge",
            "label": "Regularised linear model",
            "detail": "standardised, with position one-hots, alpha 10",
            "decision": "inconclusive",
            "reason": "Beat the heuristic at h=1 — +2.70 legal-XI points per "
                      "gameweek, 22 gameweeks to 14, and better captaincy (32.4% "
                      "accuracy against 29.7%, regret 4.43 against 5.59). But the "
                      "paired interval spans zero, and the advantage does not "
                      "survive past h=1: negative at h=2 through h=6, every one "
                      "of those intervals also spanning zero. Not selected — an "
                      "edge that is neither material nor durable does not justify "
                      "a training pipeline, a model artifact, a serialisation "
                      "format, an integrity check and a fallback path. This is "
                      "NOT the same finding as gbm.",
            "worse_at_every_horizon": False,
            "per_horizon": {
                "1": {"candidate_xi": 53.57, "diff": 2.70, "ci95": [-1.38, 6.89],
                      "p_better": 0.898, "wins": 22, "losses": 14},
                "2": {"candidate_xi": 52.00, "diff": -0.58, "ci95": [-4.22, 3.17],
                      "p_better": 0.369, "wins": 15, "losses": 18},
                "3": {"candidate_xi": 48.69, "diff": -2.34, "ci95": [-6.6, 1.83],
                      "p_better": 0.141, "wins": 13, "losses": 21},
                "4": {"candidate_xi": 50.56, "diff": -0.59, "ci95": [-5.18, 4.09],
                      "p_better": 0.406, "wins": 17, "losses": 14},
                "5": {"candidate_xi": 47.61, "diff": -1.30, "ci95": [-5.18, 2.52],
                      "p_better": 0.256, "wins": 17, "losses": 15},
                "6": {"candidate_xi": 48.53, "diff": -0.94, "ci95": [-6.5, 4.12],
                      "p_better": 0.374, "wins": 17, "losses": 14},
            },
            "statistical": {"rank_corr": {"1": 0.640, "3": 0.609, "6": 0.586},
                            "mae": {"1": 1.146, "3": 1.169, "6": 1.192}},
            "captain_accuracy_pct_h1": 32.4,
            "captain_regret_per_gw_h1": 4.43,
            "limitations": [
                "37 gameweeks is a small sample for a paired per-gameweek "
                "comparison; the h=1 interval is [-1.38, +6.89].",
                "h=1 was not pre-registered as the primary endpoint. Selecting "
                "it after the fact is how a coin flip becomes a finding.",
            ],
        },
        {
            "candidate": "xp_models",
            "label": "Models using the archive's xP column",
            "detail": "gbm and ridge with `xP` as a feature, plus per-horizon and "
                      "stacked variants",
            "decision": "invalid_experiment",
            "reason": "The only configurations that looked like a decisive win "
                      "(+10.73 legal-XI points per gameweek on the selection "
                      "season). The advantage came almost entirely from `xP`, "
                      "which is inadmissible: the archive cannot certify it as "
                      "the pre-deadline forecast managers saw, and the upstream "
                      "dataset explicitly warns it may contain post-match "
                      "information. The experiment cannot be scored — that is "
                      "different from losing.",
            "worse_at_every_horizon": None,
            "per_horizon": {},
            "statistical": {},
            "limitations": [
                "Recorded so the result is not rediscovered and mistaken for a "
                "finding.",
                "`xP` was never carried past h=1: it is a one-week number, and "
                "forwarding it would manufacture data.",
            ],
        },
    ],
    "not_ruled_out": "The trained models ARE much better at predicting "
                     "appearances — fringe-player MAE 0.285 against the shipped "
                     "0.504, rank correlation 0.462 against 0.325. A minutes-only "
                     "classifier feeding the existing `p_start` gate is the "
                     "version worth testing next; a points regressor is not. That "
                     "is a hypothesis, not a plan, and nothing here implements it.",
    "cost": {"fit_seconds": 6.95, "predict_seconds": 0.485,
             "train_rows": 279899, "predict_rows": 146073,
             "peak_train_mb": 176.8},
    "removed": ["src/gaffer/ml.py", "data/model/gaffer_gbm.joblib",
                "the `ml` extra in pyproject.toml"],
}


def candidate(name: str) -> dict[str, Any]:
    """One candidate's record. Raises rather than returning a silent default."""
    for c in MODEL_CANDIDATES["candidates"]:
        if c["candidate"] == name:
            return c
    raise KeyError(f"no model candidate named {name!r}")


def candidate_decisions() -> dict[str, str]:
    """`candidate -> decision`. These distinctions must survive summarising."""
    return {c["candidate"]: c["decision"] for c in MODEL_CANDIDATES["candidates"]}


def xp_leakage_diagnostic(seasons: tuple[str, ...] = ("2022-23", "2023-24",
                                                      "2024-25")) -> dict[str, Any]:
    """Reproduce the measurement that withdrew `fpl_xp`.

    Within one player, across gameweeks he played 60+ minutes of and his team
    played exactly once, how far does each quantity move from that player's own
    average, and how well does the move predict the move in his points?

    A pre-deadline forecast can only move with the fixture and the team news.
    Anything that moves with the *result* is not one.
    """
    out: dict[str, Any] = {"method": xp_leakage_diagnostic.__doc__, "seasons": {}}
    for season in seasons:
        hist = histdata.load_season(season)
        df = hist.frame
        if "xP" not in df:
            continue
        df = df.copy()
        df["_fx"] = df.groupby(["team_id", "GW"])["fixture"].transform("nunique")
        # `ppg_td` is the control: points-per-game so far, pre-deadline by
        # construction (shift(1)), and the closest thing the archive has to an
        # honest forecast of the same quantity.
        df["ppg_td"] = df["pts_td"] / df["games_td"].clip(lower=1)
        d = df[(df["minutes"] >= 60) & (df["_fx"] == 1)].copy()
        n = d.groupby("element")["GW"].transform("size")
        d = d[n >= 15]
        if len(d) < 500:
            continue
        d["_dy"] = d["total_points"] - d.groupby("element")["total_points"].transform("mean")
        row: dict[str, Any] = {"n_rows": int(len(d))}
        for col, kind in (("xP", "the archive's expected-points column"),
                          ("ppg_td", "CONTROL — pre-deadline by construction"),
                          ("expected_goals", "REFERENCE — post-match by definition")):
            if col not in d:
                continue
            dev = d[col] - d.groupby("element")[col].transform("mean")
            row[col] = {"kind": kind,
                        "sd_within_player": round(float(dev.std()), 2),
                        "corr_with_points_deviation": round(float(dev.corr(d["_dy"])), 3)}
        out["seasons"][season] = row
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def build_evaluation(
    season: str = TEST_SEASON, horizons: tuple[int, ...] = HORIZONS,
    *, season_end_ratings: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Every (decision_gw, target_gw, player) prediction, with its actual.

    For a decision made before gameweek ``d``, features are frozen at ``d-1`` and
    the model projects ``d .. d+5``. Horizon 6 therefore uses exactly the same
    information horizon 1 did — which is the point of measuring them separately.
    """
    hist = histdata.load_season(season)
    df = hist.frame
    leakage.assert_no_leakage(histdata.FEATURE_COLUMNS, context="adapter features")

    max_gw = int(df["GW"].max())
    records: list[pd.DataFrame] = []
    coverage = {"decision_gws": 0, "rows": 0, "skipped_no_fixture": 0}

    for decision_gw in range(FIRST_DECISION_GW, max_gw + 1):
        # Features frozen the instant before this deadline.
        snap = df[df["GW"] == decision_gw]
        if snap.empty:
            continue
        feat = snap.drop_duplicates("element").set_index("element")
        ctx = _context_for(hist, decision_gw, season_end_ratings=season_end_ratings)
        games_played = decision_gw - 1
        coverage["decision_gws"] += 1

        for h in horizons:
            target_gw = decision_gw + h - 1
            if target_gw > max_gw:
                continue
            tgt = df[df["GW"] == target_gw].copy()
            if tgt.empty:
                continue
            # Carry the frozen features onto the target fixture rows.
            cols = ["min_td", "starts_td", "xg90_td", "xa90_td", "defcon90_td",
                    "base_minutes", "base_starts", "base_xg90", "base_xa90",
                    "value", "pos", "team_id"]
            for c in cols:
                tgt[c] = tgt["element"].map(feat[c])
            tgt = tgt.dropna(subset=["pos", "value", "opponent_team", "team_id"])
            if tgt.empty:
                coverage["skipped_no_fixture"] += 1
                continue
            tgt["pred"] = project_rows(tgt, ctx, games_played)
            # A DGW is two fixture rows; sum them, as the live projection does.
            # `xP` is deliberately NOT carried through. It is the archive's own
            # expected-points column, it is not FPL's pre-deadline `ep_next`, and
            # it fails the leakage contract — see leakage.POST_MATCH_FIELDS.
            agg = tgt.groupby("element").agg(
                pred=("pred", "sum"),
                actual=("total_points", "sum"),
                minutes=("minutes", "sum"),
                pos=("pos", "first"),
                value=("value", "first"),
                team_id=("team_id", "first"),
            ).reset_index()
            agg["decision_gw"] = decision_gw
            agg["target_gw"] = target_gw
            agg["horizon"] = h
            agg["naive"] = agg["element"].map(
                feat["pts_td"] / feat["games_td"].replace(0, np.nan)
            ).fillna(0.0)
            records.append(agg)
            coverage["rows"] += len(agg)

    if not records:
        raise histdata.MissingHistoryError("no evaluable rows were produced")
    ev = pd.concat(records, ignore_index=True)
    return ev, coverage


def run(
    data_dir: Path | None = None, season: str = TEST_SEASON,
    horizons: tuple[int, ...] = HORIZONS, write: bool = False,
    out_path: Path | None = None,
    baselines: dict[str, Any] | None = None,
    ablations: list[dict[str, Any]] | None = None,
    season_end_ratings: bool = False,
) -> dict[str, Any]:
    """Evaluate and (only on request) persist.

    ``write`` defaults to False: an exploratory run must never silently rewrite
    the tracked ``data/backtest.json`` that the Accuracy page serves.
    """
    data_dir = data_dir or config.DATA_DIR
    ev, coverage = build_evaluation(
        season, horizons, season_end_ratings=season_end_ratings)

    # Only models this dataset can measure honestly. `fpl_xp` and the `ensemble`
    # that contains it were withdrawn in schema 4: both were computed from the
    # archive's `xP`, which is not FPL's pre-deadline `ep_next` and carries
    # same-gameweek information. What ships at h=1 is still the ep_next blend —
    # it is simply no longer claimed to be measured. See `withdrawn_baselines`.
    methods = {"gaffer": "pred", "naive": "naive"}
    have = {k: c for k, c in methods.items() if c in ev and ev[c].notna().any()}

    per_horizon: dict[str, Any] = {}
    for h in horizons:
        sub = ev[ev["horizon"] == h]
        if sub.empty:
            continue
        # FPL's own xP is a ONE-WEEK-AHEAD number published before each event's
        # own deadline. At h>=2 it therefore knows things the model's frozen
        # h-step forecast cannot, so comparing them there flatters it. (Its
        # near-flat rank correlation across horizons is the giveaway.) Report it
        # only where the comparison is fair.
        # Beyond h=1 neither ep_next nor the ensemble exists (ep_next is a
        # one-week-ahead number), so both are omitted rather than faked.
        usable = have
        block: dict[str, Any] = {
            "n": int(len(sub)),
            "mae": {k: round(_mae(sub[c], sub["actual"]), 3) for k, c in usable.items()},
            "rank_corr": {k: round(_rank_corr(sub, c), 3) for k, c in usable.items()},
        }
        if h == 1:  # decision metrics are only meaningful for the imminent week
            block["decisions"] = {
                k: _decision_metrics(sub, c) for k, c in usable.items()
            }
            block["transfers"] = {
                k: _transfer_regret(sub, c) for k, c in usable.items()
            }
        per_horizon[str(h)] = block

    h1 = ev[ev["horizon"] == 1]
    zero_min = int((ev["minutes"] == 0).sum())

    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "model_version": projection.MODEL_VERSION,
        "dataset": "vaastav/Fantasy-Premier-League merged_gw",
        "season": season,
        "decision_gameweeks": f"GW{FIRST_DECISION_GW}-GW{int(ev['decision_gw'].max())}",
        "horizons": list(horizons),
        "coverage": {
            **coverage,
            "rows_evaluated": int(len(ev)),
            "zero_minute_rows_retained": zero_min,
            "zero_minute_share_pct": round(100.0 * zero_min / max(len(ev), 1), 1),
            "excluded": {
                "missing_position_price_or_fixture": coverage["skipped_no_fixture"],
                "note": "no row is excluded on post-match data",
            },
        },
        "leakage_check": {
            "enforced": True,
            "post_match_fields_in_features": leakage.check_features(
                histdata.FEATURE_COLUMNS
            ),
            "policy": "features use shift(1) season-to-date aggregates only",
        },
        "per_horizon": per_horizon,
        "calibration": {
            "overall": _calibration(h1, "pred"),
            "by_position": _calibration_by_position(h1, "pred"),
        },
        "shipped_projection": {
            "model_version": projection.MODEL_VERSION,
            "next_gameweek": f"blend of Gaffer and FPL ep_next at w="
                             f"{config.EP_NEXT_BLEND_WEIGHT}, scaled by availability",
            "later_gameweeks": "Gaffer only — ep_next does not exist beyond h=1",
            "next_gameweek_status": "POLICY, NOT MEASURED. The blend weight cannot "
                                    "be fitted on this dataset because the archive "
                                    "has no faithful copy of the live ep_next.",
        },
        "withdrawn_baselines": WITHDRAWN_BASELINES,
        "model_candidates": MODEL_CANDIDATES,
        "limitations": [
            "Team strength ratings are rebuilt each gameweek from matches already "
            "played (T-12). They are NOT the same construction the live pipeline "
            "uses, which reads FPL's published strength fields — so fixture-driven "
            "numbers here are indicative rather than exactly reproducible live.",
            "The dataset carries no status/chance-of-playing column, so every "
            "player resolves to the model's available branch; the availability "
            "path is exercised but never varied.",
            "DEFCON did not score in 2024-25, so its contribution here is "
            "structural only.",
            "Transfer regret is a one-free-transfer greedy sequence, not the "
            "shipped multi-period solver.",
            "Squad selection maximises projected points under budget, quota and "
            "the club limit. It is close to, but not identical to, the shipped "
            "optimiser, which also carries a ceiling term and a goalkeeper "
            "spend penalty.",
            "The 'gaffer' column is the standalone component model. What ships "
            "for the NEXT gameweek is a blend of it with FPL's live ep_next, and "
            "that blend is unmeasurable here — see `withdrawn_baselines`. Beyond "
            "h=1 the shipped projection is exactly this column.",
            "An in-sample gain is never evidence. Parameters were selected on "
            "2023-24 and reported here on 2024-25, which selection never saw.",
        ],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    # Optional evidence blocks. `baselines` compares this model against frozen
    # earlier versions; `ablations` records which changes earned their place.
    if baselines:
        out["baselines"] = baselines
    if ablations:
        out["ablations"] = ablations
    if write:
        target = Path(out_path) if out_path else data_dir / "backtest.json"
        # Say exactly what is being overwritten, before it happens.
        print(f"[backtest] WRITING {target}", file=sys.stderr)
        write_json_atomic(target, out)
        out["_written_to"] = str(target)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Backtest the shipped projection (read-only by default)")
    ap.add_argument("--season", default=TEST_SEASON)
    ap.add_argument("--horizons", type=int, nargs="*", default=list(HORIZONS))
    ap.add_argument(
        "--write", action="store_true",
        help="persist the result. Without --out this overwrites the tracked "
             "data/backtest.json that the Accuracy page serves.")
    ap.add_argument("--out", default=None,
                    help="explicit output path (implies --write)")
    ap.add_argument("--xp-diagnostic", action="store_true",
                    help="reproduce the measurement that withdrew the fpl_xp "
                         "baseline, and exit")
    args = ap.parse_args(argv)
    if args.xp_diagnostic:
        try:
            print(json.dumps(xp_leakage_diagnostic(), indent=2))
        except histdata.MissingHistoryError as exc:
            print(f"diagnostic unavailable: {exc}", file=sys.stderr)
            return 2
        return 0
    write = args.write or args.out is not None
    try:
        out = run(season=args.season, horizons=tuple(args.horizons),
                  write=write, out_path=Path(args.out) if args.out else None)
    except histdata.MissingHistoryError as exc:
        print(f"backtest unavailable: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
