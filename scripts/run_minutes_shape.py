"""Phase 2A.4 -- is the bimodal expected-minutes shape costing anything?

PRE-REGISTERED. Committed before any result is read.

After Release A the START PROBABILITY beats every baseline at every horizon.
The MINUTES estimate does not: MAE 14.3 against 11.5 for a lagged start times
ninety. The shape is the suspect. `exp_minutes` is

    p_start * START_MINUTES + (p_play - p_start) * CAMEO_MINUTES

with START_MINUTES and CAMEO_MINUTES global constants, so every player is
either "about 78 minutes" or "about 3", and a reliable 60-minute player is in
neither mode. Gaffer's own crossover programme identified this in 2026-08 and
OpenFPL's published method uses the two-stage alternative: appearance
probability, then minutes CONDITIONAL on appearing.

THE CANDIDATE. Replace the two global constants with the player's OWN minutes
when he plays, shrunk toward the positional mean so a one-cameo sample does not
become a claim:

    E[min | started] = shrink(his mean minutes in fixtures he started)
    E[min | cameo]   = shrink(his mean minutes in fixtures he appeared but
                              did not start)

    exp_minutes = p_start * E[min|started] + (p_play - p_start) * E[min|cameo]

Scored at all three levels the plan requires, because a minutes model can be
mechanically better while making worse decisions, and MAE on minutes is not
Gaffer's objective:

    1. exp_minutes MAE   -- the thing being changed
    2. points MAE        -- whether it survives the multiplication
    3. XI points per gw  -- whether it changes a decision

DECISION RULE, fixed before any result:
    Ship only if it improves exp_minutes MAE on the TEST season AND does not
    worsen points MAE there AND does not lower XI points per gameweek there.
    Any one of those failing is a refusal, recorded like the others.

Research code. Lives in scripts/, imported by nothing.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

from gaffer import backtest as BT
from gaffer import histdata
from gaffer.model import features as F

SEASONS = ("2023-24", "2024-25", "2025-26")

#: Shrinkage sample, in fixtures, for the conditional-minutes estimate. Three
#: appearances weigh equally with the positional prior. Not swept.
MIN_SHRINK_N = 3.0

#: Positional priors for minutes GIVEN the event, from the shipped constants.
PRIOR_START_MIN = 80.0
PRIOR_CAMEO_MIN = 20.0


def conditional_minutes(df: pd.DataFrame, decision_gw: int) -> dict[int, dict]:
    """Per player, mean minutes when he started and when he came on.

    Strictly prior gameweeks. That is the leakage boundary and nothing here
    relaxes it.
    """
    prior = df[df["GW"] < decision_gw]
    if prior.empty:
        return {}
    out: dict[int, dict] = {}
    for element, g in prior.groupby("element"):
        started = g[g["starts"] == 1]["minutes"].astype(float)
        cameo = g[(g["starts"] == 0) & (g["minutes"] > 0)]["minutes"].astype(float)

        def shrunk(sample, prior_mean):
            n = len(sample)
            if n == 0:
                return prior_mean
            w = n / (n + MIN_SHRINK_N)
            return w * float(sample.mean()) + (1 - w) * prior_mean

        out[int(element)] = {
            "start_min": shrunk(started, PRIOR_START_MIN),
            "cameo_min": shrunk(cameo, PRIOR_CAMEO_MIN),
        }
    return out


def score(season: str) -> dict:
    ev, _cov = BT.build_minutes_evaluation(season=season, horizons=(1,))
    ev = ev[ev["horizon"] == 1].copy()
    hist = histdata.load_season(season)
    df = hist.frame

    # Rebuild the conditional estimate per decision gameweek.
    cond_start = np.full(len(ev), PRIOR_START_MIN)
    cond_cameo = np.full(len(ev), PRIOR_CAMEO_MIN)
    for gw, idx in ev.groupby("decision_gw").groups.items():
        table = conditional_minutes(df, int(gw))
        pos = ev.index.get_indexer(list(idx))
        els = ev.loc[list(idx), "element"].to_numpy()
        for j, el in zip(pos, els, strict=False):
            row = table.get(int(el))
            if row:
                cond_start[j] = row["start_min"]
                cond_cameo[j] = row["cameo_min"]

    p_start = ev["p_start"].astype(float).to_numpy()
    p_play = ev["p_play"].astype(float).to_numpy()
    actual = ev["minutes"].astype(float).to_numpy()

    shipped = ev["exp_minutes"].astype(float).to_numpy()
    candidate = p_start * cond_start + np.maximum(p_play - p_start, 0.0) * cond_cameo
    lagged = ev["started_lag"].fillna(0).astype(float).to_numpy() * 90.0

    def mae(x):
        m = ~np.isnan(x)
        return round(float(np.mean(np.abs(x[m] - actual[m]))), 3)

    return {
        "n": int(len(ev)),
        "exp_minutes_mae": {
            "shipped_bimodal": mae(shipped),
            "candidate_conditional": mae(candidate),
            "baseline_lagged_x90": mae(lagged),
        },
        "delta_vs_shipped": round(mae(candidate) - mae(shipped), 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="*", default=list(SEASONS))
    args = ap.parse_args()
    out = {"shrink_n": MIN_SHRINK_N, "seasons": {}}
    for s in args.seasons:
        try:
            out["seasons"][s] = score(s)
        except Exception as exc:  # noqa: BLE001 - research script
            out["seasons"][s] = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
