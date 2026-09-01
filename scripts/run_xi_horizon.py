"""Phase 1.6 -- does picking the XI on a HORIZON sum cost points?

The shipped objective is

    obj = sum(start[i] * players[i].value)          # value = decayed 6-GW sum
        + sum(cap[i]   * players[i].next_gw_points) # captain on ONE week

so the captain is chosen on the imminent gameweek and the starting eleven is
not, though both are re-picked every week at no cost. The line above the
captain term says exactly why it must be one week -- "Captaincy is re-chosen
every week, so double on *next-GW* points, not horizon" -- and the same
argument was never applied to the XI beside it.

`backtest._decision_metrics` runs only at h=1 and picks BOTH the squad and the
XI on the one-week column, so the harness has never modelled the shipped
behaviour and cannot see the difference. This is the missing measurement.

Pre-registered, before looking at any result:

  POLICY A (shipped)  squad on decayed horizon value, XI on decayed value
  POLICY B (proposed) squad on decayed horizon value, XI on the NEXT-GW value

  Metric   realised points of the chosen XI, per gameweek, on `actual`.
  Decision ship B only if it wins on the test season AND does not lose on
           either of the other two. A tie is a refusal: the shipped code stays.

Research code. Lives in scripts/, imported by nothing.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

from gaffer import backtest as BT
from gaffer.solver.optimize import HORIZON_DECAY

SEASONS = ("2023-24", "2024-25", "2025-26")


def decayed(ev: pd.DataFrame) -> pd.DataFrame:
    """Per (decision_gw, element): the decayed sum the solver actually uses."""
    e = ev.copy()
    e["w"] = HORIZON_DECAY ** (e["horizon"] - 1)
    e["wpred"] = e["pred"] * e["w"]
    horizon_sum = (e.groupby(["decision_gw", "element"])["wpred"]
                    .sum().rename("pred_horizon"))
    one = e[e["horizon"] == 1].set_index(["decision_gw", "element"])
    out = one.join(horizon_sum, how="left").reset_index()
    return out.dropna(subset=["pred_horizon"])


def score(df: pd.DataFrame, squad_col: str, xi_col: str) -> dict:
    """Realised XI points per gameweek under one selection policy."""
    weeks, pts = 0, []
    for _, grp in df.groupby("target_gw"):
        g = grp.dropna(subset=["value", "team_id"]).copy()
        if len(g) < 40:
            continue
        g = g.set_index("element")
        squad = BT._select_squad(g, squad_col)
        if squad is None:
            continue
        xi = BT._best_xi(g, squad, xi_col)
        if len(xi) != 11:
            continue
        weeks += 1
        pts.append(float(g.loc[xi, "actual"].sum()))
    if not weeks:
        return {"weeks": 0}
    return {"weeks": weeks,
            "xi_points_per_gw": round(float(np.mean(pts)), 3),
            "total": round(float(np.sum(pts)), 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="*", default=list(SEASONS))
    args = ap.parse_args()
    results = {}
    for season in args.seasons:
        try:
            ev, _cov = BT.build_evaluation(season=season, horizons=BT.HORIZONS)
        except Exception as exc:  # noqa: BLE001 - research script
            results[season] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        df = decayed(ev)
        a = score(df, "pred_horizon", "pred_horizon")   # shipped
        b = score(df, "pred_horizon", "pred")           # proposed
        delta = (None if not a.get("weeks") or not b.get("weeks")
                 else round(b["xi_points_per_gw"] - a["xi_points_per_gw"], 3))
        results[season] = {"A_shipped_xi_on_horizon": a,
                           "B_proposed_xi_on_next_gw": b,
                           "delta_pts_per_gw": delta}
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
