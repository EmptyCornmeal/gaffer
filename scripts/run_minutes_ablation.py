"""Phase 2, Release A -- the minutes ablation ladder.

PRE-REGISTERED. Written and committed before any result was read.

The shipped gate is `fixtures_played >= 3` (projection.py:586). Teams have
played two fixtures at GW3, so at GW1-GW3 EVERY player in the game falls
through to `base_starts / 38` and is graded on last season. On 2026-09-01 that
published `p_start 0.90` and a NAILED badge for a player with 0 starts and 11
minutes, while six ever-presents were flagged as rotation risks -- a ranking
anti-correlated with the only evidence the season had produced.

Two variants have already been measured for this gate and both were RATES:
the shipped `>= 3`, and a `>= 1` that scored better on Brier in all three
seasons and was refused for reading `starts / 1` as a probability. Neither was
scored inside the regime the gate actually binds in, which is about 8% of a
season -- so the metric that decided it averaged over 35 gameweeks where the
gate is irrelevant.

THE LADDER. Each rung adds ONE intervention to the one above it, so a win can
be attributed rather than merely observed. "Release A improved things" is not
an acceptable finding.

    R0  shipped          the live gate, unchanged
    R1  + shrinkage      blend season-to-date starts toward the prior-season
                         rate with weight played/(played+k) -- no hard switch,
                         so no GW4 cliff
    R2  + recency        blend R1 toward the last-3 start share
    R3  + last-match     blend R2 toward "did he start his last fixture"

SCORING. Brier on `started`, at h=1, reported BOTH:
    * restricted to the GW1-3 regime (decision_gw <= 3), the window the gate
      binds in and the reason this exists;
    * across the whole season, so a rung that helps early and hurts later
      cannot hide.

Also reported: the two naive baselines the shipped model already loses to at
h=1, so every rung is judged against the one-line rule as well as against R0.

DECISION RULE, fixed before any result:
    Ship the HIGHEST rung that (a) improves GW1-3 Brier against R0 on the test
    season, and (b) does not worsen full-season Brier against R0 on the test
    season by more than 0.002. Ties go to the LOWER rung -- fewer moving parts
    wins. If no rung satisfies both, ship nothing and record the refusal.

Research code. Lives in scripts/, imported by nothing.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

from gaffer import backtest as BT

SEASONS = ("2023-24", "2024-25", "2025-26")

#: Shrinkage half-life, in team fixtures. At k=3 a team with three completed
#: fixtures weights this season and last equally, which is exactly where the
#: shipped gate flips from one to the other in a single step. Chosen to make
#: the comparison about the SHAPE (smooth vs cliff) rather than about a tuned
#: constant, and deliberately not swept.
SHRINK_K = 3.0

#: Weight on the recency terms once they exist. Also not swept.
W_R3 = 0.35
W_LAG = 0.25


def _clip01(x):
    return np.clip(x, 0.0, 0.98)


def rungs(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Each rung's p_start, computed from the gate's own inputs."""
    played = df["fixtures_played"].astype(float)
    td_rate = np.where(played > 0, df["starts_td"] / played.replace(0, np.nan), np.nan)
    prior_rate = df["base_starts"] / 38.0

    # R1 -- shrinkage. No gate: the current season enters from the first
    # fixture, weighted by how much of it there is.
    w = played / (played + SHRINK_K)
    r1 = np.where(np.isnan(td_rate), prior_rate, w * td_rate + (1 - w) * prior_rate)
    r1 = _clip01(np.where(np.isnan(r1), df["p_start"], r1))

    # R2 -- + the last-three start share, where it exists.
    r3c = df["start_rate_r3"]
    r2 = np.where(r3c.notna(), (1 - W_R3) * r1 + W_R3 * r3c, r1)
    r2 = _clip01(r2)

    # R3 -- + did he start his last fixture.
    lag = df["started_lag"]
    r3 = np.where(lag.notna(), (1 - W_LAG) * r2 + W_LAG * lag, r2)
    r3 = _clip01(r3)

    return {
        "R0_shipped": df["p_start"].astype(float),
        "R1_shrinkage": pd.Series(r1, index=df.index),
        "R2_plus_recency": pd.Series(r2, index=df.index),
        "R3_plus_last_match": pd.Series(r3, index=df.index),
        "naive_start_rate_td": df["start_rate_td"].fillna(df["p_start"]),
        "naive_started_lag": df["started_lag"].fillna(df["p_start"]),
        "naive_start_rate_r3": df["start_rate_r3"].fillna(df["p_start"]),
    }


def brier(y, p) -> float:
    m = ~(np.isnan(y) | np.isnan(p))
    if m.sum() == 0:
        return float("nan")
    return float(np.mean((np.asarray(p)[m] - np.asarray(y)[m]) ** 2))


def score_season(season: str) -> dict:
    ev, _cov = BT.build_minutes_evaluation(season=season, horizons=(1,))
    ev = ev[ev["horizon"] == 1].copy()
    cols = rungs(ev)
    y = ev["started"].astype(float).to_numpy()
    early = (ev["decision_gw"] <= 3).to_numpy()
    out = {"n_all": int(len(ev)), "n_early": int(early.sum()), "rungs": {}}
    for name, p in cols.items():
        pv = np.asarray(p, dtype=float)
        out["rungs"][name] = {
            "brier_gw1_3": round(brier(y[early], pv[early]), 5),
            "brier_full": round(brier(y, pv), 5),
        }
    base = out["rungs"]["R0_shipped"]
    for r in out["rungs"].values():
        r["d_early_vs_shipped"] = round(r["brier_gw1_3"] - base["brier_gw1_3"], 5)
        r["d_full_vs_shipped"] = round(r["brier_full"] - base["brier_full"], 5)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="*", default=list(SEASONS))
    args = ap.parse_args()
    results = {"shrink_k": SHRINK_K, "w_r3": W_R3, "w_lag": W_LAG, "seasons": {}}
    for season in args.seasons:
        try:
            results["seasons"][season] = score_season(season)
        except Exception as exc:  # noqa: BLE001 - research script
            results["seasons"][season] = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
