"""Backtest the projection methods on a held-out season (vaastav dataset).

Compares four methods on 2024-25 (never seen during training):
  * gaffer   — the transparent heuristic-0.1-style projection
  * ml       — the trained gradient-boosted model (trained on 2022-24)
  * fpl_xp   — FPL's own expected points (a strong benchmark)
  * naive    — the player's recent average (baseline)

Reports MAE (lower better), rank correlation (higher = orders players better),
and lift (avg actual points of the top-20% vs bottom-20% projected).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from gaffer import config, ml

TEST_SEASON = "2024-25"
MIN_GW = ml.ROLL + 1
_GOAL_ISH = 5.0
_CS_PTS = {"GKP": 4, "GK": 4, "DEF": 4, "MID": 1, "AM": 1, "FWD": 0}


def _mae(pred: pd.Series, actual: pd.Series) -> float:
    return float((pred - actual).abs().mean())


def _avg_rank_corr(df: pd.DataFrame, col: str) -> float:
    corrs = []
    for _, grp in df.groupby("GW"):
        if len(grp) >= 10 and grp[col].std() > 0:
            corr = grp[col].rank().corr(grp["total_points"].rank())
            if pd.notna(corr):
                corrs.append(corr)
    return float(pd.Series(corrs).mean()) if corrs else 0.0


def _lift(df: pd.DataFrame, col: str) -> dict[str, float]:
    tops, bots = [], []
    for _, grp in df.groupby("GW"):
        if len(grp) < 20:
            continue
        q_hi, q_lo = grp[col].quantile(0.8), grp[col].quantile(0.2)
        tops.append(grp.loc[grp[col] >= q_hi, "total_points"].mean())
        bots.append(grp.loc[grp[col] <= q_lo, "total_points"].mean())
    return {"top": round(float(pd.Series(tops).mean()), 2),
            "bottom": round(float(pd.Series(bots).mean()), 2)}


def _heuristic(df: pd.DataFrame) -> pd.Series:
    exp_min = df["r_min"].clip(0, 90).fillna(0)
    appearance = (exp_min >= 60).astype(float) * 2 + ((exp_min > 0) & (exp_min < 60)).astype(float)
    attack = df["xgi90"] * (exp_min / 90.0) * _GOAL_ISH * df["att_mult"]
    p_cs = np.exp(-np.clip(df["cs_lambda"], 0.15, 4.0))
    cs_pts = df["position"].map(lambda p: _CS_PTS.get(str(p), 0)).astype(float)
    cs = p_cs * cs_pts * (exp_min >= 60).astype(float)
    return appearance + attack + cs


def run(data_dir: Path | None = None) -> dict[str, Any]:
    data_dir = data_dir or config.DATA_DIR
    if not ml.MODEL_PATH.exists():
        ml.train()

    df = ml.build_features(TEST_SEASON)
    df["gaffer"] = _heuristic(df)
    df["ml"] = ml.predict(df)

    ev = df[(df["GW"] >= MIN_GW) & (df["minutes"] > 0) & df["r_minsum"].notna()].copy()
    ev = ev.dropna(subset=["total_points"])
    ev["fpl"] = ev["xP"].fillna(0) if "xP" in ev else ev["r_pts"]
    ev["naive"] = ev["r_pts"].fillna(0)

    methods = {"gaffer": "gaffer", "ml": "ml", "fpl_xp": "fpl", "naive": "naive"}
    out = {
        "season": TEST_SEASON,
        "n_predictions": int(len(ev)),
        "gameweeks": f"GW{MIN_GW}–GW{int(ev['GW'].max())}",
        "trained_on": "2022-23 + 2023-24",
        "mae": {k: round(_mae(ev[c], ev["total_points"]), 3) for k, c in methods.items()},
        "rank_corr": {k: round(_avg_rank_corr(ev, c), 3) for k, c in methods.items()},
        "lift": {
            "ml": _lift(ev, "ml"),
            "gaffer": _lift(ev, "gaffer"),
            "fpl_xp": _lift(ev, "fpl"),
        },
        "note": (
            "Trained gradient-boosted model vs the transparent heuristic vs FPL's "
            "own xP vs a naive recent-form baseline, on a season the model never "
            "trained on. Rank correlation = ordering quality (higher better); "
            "MAE = points error (lower better); lift = avg actual points of the "
            "top-20% vs bottom-20% projected."
        ),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (data_dir / "backtest.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
