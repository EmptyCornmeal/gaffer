"""Chronological fitting and evaluation for fixture-strength parameters (T-12).

Selection never touches the reporting period. The three seasons are **derived
from** ``backtest.SEASON_SPLIT`` rather than restated here — a second copy is
exactly how this module and the backtest drifted apart (G19), and how it came to
name 2022-23 as its training season while also excluding it.

``STRENGTH_GAMMA`` / ``STRENGTH_CLAMP`` are module globals read at call time, so
a sweep patches them for the duration of one evaluation and restores them. This
module is evaluation-only; it never writes production parameters.

    python -m gaffer.fitting sweep      # train + validation
    python -m gaffer.fitting report     # final, on the untouched test season
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from contextlib import contextmanager
from typing import Any

import numpy as np

from gaffer import backtest, histdata
from gaffer.model import features as F

#: G19 — derived, never restated. `backtest.SEASON_SPLIT` is the single source
#: of truth for which season plays which role; these three names exist only so
#: the sweep reads legibly.
TRAIN_SEASON = backtest.SEASON_SPLIT["train"][0]
VALIDATION_SEASON = backtest.SEASON_SPLIT["select"]
TEST_SEASON = backtest.SEASON_SPLIT["test"]

#: The audit's diagnostic target for multiplier dispersion. A guide, not a goal
#: to optimise directly — the selection criterion is out-of-sample rank corr.
TARGET_SD_LOG = 0.24


@contextmanager
def strength_params(gamma: float | None = None, clamp: tuple[float, float] | None = None):
    """Temporarily override the shipped fixture-strength parameters."""
    old_g, old_c = F.STRENGTH_GAMMA, F.STRENGTH_CLAMP
    try:
        if gamma is not None:
            F.STRENGTH_GAMMA = gamma
        if clamp is not None:
            F.STRENGTH_CLAMP = clamp
        yield
    finally:
        F.STRENGTH_GAMMA, F.STRENGTH_CLAMP = old_g, old_c


def multiplier_diagnostics(season: str, decision_gw: int = 20) -> dict[str, Any]:
    """Distribution of the attack multipliers a context actually produces."""
    hist = histdata.load_season(season)
    ctx = backtest._context_for(hist, decision_gw)
    teams = list(ctx.def_home)
    vals = [ctx.attack_multiplier(t, home) for t in teams for home in (True, False)]
    lo, hi = F.STRENGTH_CLAMP
    at_lo = sum(1 for v in vals if abs(v - lo) < 1e-9)
    at_hi = sum(1 for v in vals if abs(v - hi) < 1e-9)
    logs = [math.log(v) for v in vals if v > 0]
    return {
        "n": len(vals),
        "min": round(min(vals), 3), "max": round(max(vals), 3),
        "spread": round(max(vals) / min(vals), 3),
        "sd_log": round(statistics.pstdev(logs), 4),
        "at_low_clamp": at_lo, "at_high_clamp": at_hi,
        "clamped_pct": round(100.0 * (at_lo + at_hi) / max(len(vals), 1), 1),
    }


def evaluate(season: str, horizons: tuple[int, ...] = (1,),
             season_end_ratings: bool = False) -> dict[str, Any]:
    """Player-level and decision-level metrics for one configuration."""
    ev, _ = backtest.build_evaluation(
        season, horizons, season_end_ratings=season_end_ratings)
    h1 = ev[ev["horizon"] == horizons[0]]
    out = {
        "n": int(len(h1)),
        "rank_corr": round(backtest._rank_corr(h1, "pred"), 4),
        "mae": round(backtest._mae(h1["pred"], h1["actual"]), 4),
    }
    dec = backtest._decision_metrics(h1, "pred")
    if dec:
        out["xi_points_per_gw"] = dec["xi_points_per_gw"]
        out["captain_accuracy_pct"] = dec["captain_accuracy_pct"]
    return out


def bootstrap_ci(season: str, col: str = "pred", n_boot: int = 200,
                 seed: int = 7) -> dict[str, float]:
    """Bootstrap the gameweek-level rank correlation to size the noise floor."""
    ev, _ = backtest.build_evaluation(season, (1,))
    h1 = ev[ev["horizon"] == 1]
    per_gw = []
    for _, grp in h1.groupby("target_gw"):
        if len(grp) >= 10 and grp[col].std() > 0:
            c = grp[col].rank().corr(grp["actual"].rank())
            if c == c:
                per_gw.append(float(c))
    if len(per_gw) < 3:
        return {}
    rng = np.random.default_rng(seed)
    means = [float(np.mean(rng.choice(per_gw, len(per_gw), replace=True)))
             for _ in range(n_boot)]
    return {
        "mean": round(float(np.mean(per_gw)), 4),
        "ci_lo": round(float(np.percentile(means, 2.5)), 4),
        "ci_hi": round(float(np.percentile(means, 97.5)), 4),
        "gameweeks": len(per_gw),
    }


CLAMP_GRID = ((0.5, 1.85), (0.55, 1.60), (0.60, 1.50), (0.45, 2.20), (0.40, 2.50))

#: G19 — taken from `backtest.SEASON_SPLIT["excluded"]`, reasons included.
#:
#: The previous value here was wrong twice over. It justified excluding 2022-23
#: with *"the dataset has no 2021-22 file"* — `data/history/merged_gw_2021-22.csv`
#: has been on disk since 2026-08-15, so that was simply false; the real reason is
#: G-Q, that 2022-23's `expected_goals` and `expected_assists` are identically
#: zero for GW1-15. And because it was keyed on `TRAIN_SEASON`, it excluded
#: whichever season this module was training on — a contradiction that survived
#: only because nothing imports this module at runtime.
EXCLUDED_SEASONS = dict(backtest.SEASON_SPLIT.get("excluded") or {})


def split_evaluate(season: str, first_gw: int, last_gw: int) -> dict[str, Any]:
    """Metrics restricted to a chronological slice of one season."""
    ev, _ = backtest.build_evaluation(season, (1,))
    h = ev[(ev["horizon"] == 1) & (ev["decision_gw"] >= first_gw)
           & (ev["decision_gw"] <= last_gw)]
    if h.empty:
        return {}
    return {
        "n": int(len(h)),
        "gameweeks": f"GW{first_gw}-GW{last_gw}",
        "rank_corr": round(backtest._rank_corr(h, "pred"), 4),
        "mae": round(backtest._mae(h["pred"], h["actual"]), 4),
    }


def sweep(horizons: tuple[int, ...] = (1,)) -> dict[str, Any]:
    """Clamp sweep with a rolling origin inside the validation season.

    ``STRENGTH_GAMMA`` is deliberately NOT swept: it is applied only in the
    coarse pre-season regime (``TeamContext._spread``), and every historical
    gameweek is in the fine regime, so no amount of backtest evidence can move
    it. Verified rather than assumed — see ``gamma_is_inert``.
    """
    results = []
    for clamp in CLAMP_GRID:
        with strength_params(clamp=clamp):
            row: dict[str, Any] = {"clamp": list(clamp)}
            row["diagnostics"] = multiplier_diagnostics(VALIDATION_SEASON)
            row["fit"] = split_evaluate(VALIDATION_SEASON, 2, 19)
            row["select"] = split_evaluate(VALIDATION_SEASON, 20, 38)
            row["mean_rank_corr"] = round(
                (row["fit"].get("rank_corr", 0) + row["select"].get("rank_corr", 0)) / 2, 4)
            results.append(row)
            print(
                f"  clamp={clamp}  fit={row['fit'].get('rank_corr'):.4f} "
                f"select={row['select'].get('rank_corr'):.4f} "
                f"sd_log={row['diagnostics']['sd_log']:.3f} "
                f"clamped={row['diagnostics']['clamped_pct']}%",
                file=sys.stderr, flush=True)
    results.sort(key=lambda r: -r["mean_rank_corr"])
    return {
        "validation_season": VALIDATION_SEASON,
        "fit_window": "GW2-19", "selection_window": "GW20-38",
        "test_season_untouched": TEST_SEASON,
        "excluded_seasons": EXCLUDED_SEASONS,
        "gamma": {
            "swept": False,
            "value": F.STRENGTH_GAMMA,
            "reason": "inert in the in-season fine-rating regime; only applies "
                      "pre-season, which no historical gameweek occupies",
        },
        "criterion": "mean rank correlation over the fit and selection windows",
        "target_sd_log": TARGET_SD_LOG,
        "results": results,
        "selected": results[0] if results else None,
    }


def gamma_is_inert(season: str = VALIDATION_SEASON, decision_gw: int = 20) -> bool:
    """Prove that gamma cannot change an in-season multiplier."""
    hist = histdata.load_season(season)
    ctx = backtest._context_for(hist, decision_gw)
    ref = None
    for g in (0.5, 1.7, 3.0):
        with strength_params(gamma=g):
            v = ctx.attack_multiplier(next(iter(ctx.def_home)), True)
        if ref is None:
            ref = v
        elif abs(v - ref) > 1e-12:
            return False
    return True


def rating_ablation(season: str = TEST_SEASON) -> dict[str, Any]:
    """Pre-deadline rolling ratings vs the leaky season-end snapshot."""
    return {
        "season": season,
        "pre_deadline_rolling": evaluate(season, (1,), season_end_ratings=False),
        "season_end_leaky": evaluate(season, (1,), season_end_ratings=True),
        "note": "the season-end variant knows how the season finished; it is "
                "reported only to size the optimism it introduced",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fit fixture-strength parameters")
    ap.add_argument("mode", choices=["sweep", "report", "diagnostics", "ablation"])
    ap.add_argument("--gamma", type=float, default=None)
    ap.add_argument("--clamp", type=float, nargs=2, default=None)
    ap.add_argument("--season", default=None)
    args = ap.parse_args(argv)

    clamp = tuple(args.clamp) if args.clamp else None
    if args.mode == "sweep":
        print(json.dumps(sweep(), indent=2))
    elif args.mode == "ablation":
        print(json.dumps(rating_ablation(args.season or TEST_SEASON), indent=2))
    elif args.mode == "diagnostics":
        with strength_params(args.gamma, clamp):
            print(json.dumps(
                multiplier_diagnostics(args.season or VALIDATION_SEASON), indent=2))
    else:
        season = args.season or TEST_SEASON
        with strength_params(args.gamma, clamp):
            out = {
                "season": season,
                "gamma": F.STRENGTH_GAMMA, "clamp": list(F.STRENGTH_CLAMP),
                "metrics": evaluate(season),
                "diagnostics": multiplier_diagnostics(season),
                "bootstrap": bootstrap_ci(season),
            }
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# T-15's blend fitters lived here, and T-26 removed them.
#
# `blend_eval`, `blend_by_position` and `ep_next_availability` swept the weight
# in `(1-w) * gaffer + w * ep_next` against the archive's `xP` column. That
# column carries same-gameweek information (backtest.WITHDRAWN_BASELINES, and
# `python -m gaffer.backtest --xp-diagnostic`), so every weight they returned
# described a forecast nobody can make. They also no longer run: the corrected
# harness stopped carrying `xP` through at all.
#
# The weight is not fittable offline, because the archive holds no faithful copy
# of the live `ep_next`. It becomes fittable in-season, from `projection_snapshots`
# (which stores `exp_points_ep_next` beside the model's own number, before each
# deadline) joined to `player_gw`. Until then EP_NEXT_BLEND_WEIGHT is a labelled
# policy choice — see config.EP_NEXT_BLEND_IS_FITTED.
# ---------------------------------------------------------------------------
