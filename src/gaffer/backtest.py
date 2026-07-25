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
from gaffer.model import features as F
from gaffer.model import projection

TEST_SEASON = "2024-25"
MIN_GW = ml.ROLL + 1
# CSV position label -> the model's position code
_POS = {"GK": "GKP", "GKP": "GKP", "DEF": "DEF", "MID": "MID", "AM": "MID", "FWD": "FWD"}


class _RowCtx:
    """Stand-in for TeamContext: returns this fixture's precomputed multipliers so
    the *shipped* projection maths runs unchanged on historical rows."""

    __slots__ = ("att", "lam")

    def __init__(self, att: float, lam: float) -> None:
        self.att = att
        self.lam = lam

    def attack_multiplier(self, opponent_id: int, at_home: bool) -> float:
        return self.att

    def expected_conceded(self, team_id: int, opponent_id: int, at_home: bool) -> float:
        return self.lam


def _season_to_date(df: pd.DataFrame) -> pd.DataFrame:
    """Cumulative, leak-free (shift(1)) season-to-date inputs the shipped
    projection expects — mirrors what the live pipeline feeds it in production."""
    df = df.sort_values(["element", "GW"]).copy()
    g = df.groupby("element")

    def cum(col: str) -> pd.Series:
        if col not in df:
            return pd.Series(0.0, index=df.index)
        return g[col].transform(lambda x: x.shift(1).cumsum())

    df["min_td"] = cum("minutes")
    df["starts_td"] = cum("starts")
    per90 = np.where(df["min_td"] > 0, 90.0 / df["min_td"].replace(0, np.nan), 0.0)
    df["xg90_td"] = (cum("expected_goals") * per90).fillna(0.0)
    df["xa90_td"] = (cum("expected_assists") * per90).fillna(0.0)
    return df


def _shipped(df: pd.DataFrame) -> pd.Series:
    """Run the REAL ``projection._project_one_fixture`` on each historical row, so
    the backtest scores the shipped model (per-position goal points, appearance
    model, CS Poisson, bonus proxy, minutes gating) — not a reimplementation.

    DEFCON is 0 here: it did not score in 2024-25, so a faithful backtest of that
    season omits it. Last-season ``base_*`` are 0 (no prior-season join in this
    harness); once games_played>=3 the season-to-date rate dominates anyway.
    """
    out = np.zeros(len(df))
    for pos_i, row in enumerate(df.itertuples(index=False)):
        player = {
            "position": _POS.get(str(row.position), "MID"),
            "minutes": float(row.min_td),
            "base_minutes": 0,
            "starts": float(row.starts_td),
            "base_starts": 0,
            "price": float(row.value),
            "base_xg90": 0.0,
            "base_xa90": 0.0,
            "xg_per_90": float(row.xg90_td),
            "xa_per_90": float(row.xa90_td),
            "defcon_per_90": 0.0,
            "team_id": 0,
        }
        fx = F.Fixture(gw=int(row.GW), opponent_id=0, at_home=bool(row.home), fdr=3)
        ctx = _RowCtx(float(row.att_mult), float(row.cs_lambda))
        games_played = int(row.GW) - 1
        parts = projection._project_one_fixture(player, fx, ctx, 1.0, games_played)
        out[pos_i] = parts["exp_points"]
    return pd.Series(out, index=df.index)


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


def run(data_dir: Path | None = None) -> dict[str, Any]:
    data_dir = data_dir or config.DATA_DIR
    if not ml.MODEL_PATH.exists():
        ml.train()

    df = ml.build_features(TEST_SEASON)
    df = _season_to_date(df)
    df["ml"] = ml.predict(df)
    df["gaffer"] = _shipped(df)  # the actual shipped projection, not a stand-in

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
            "The trained gradient-boosted model vs Gaffer's *shipped* component "
            "projection (the exact code the site runs, incl. per-position goal "
            "points, appearance model, clean-sheet Poisson and bonus proxy) vs "
            "FPL's own xP vs a naive recent-form baseline, on a season the model "
            "never trained on. DEFCON is excluded here — it did not score in "
            "2024-25. Rank correlation = ordering quality (higher better); MAE = "
            "points error (lower better); lift = avg actual points of the top-20% "
            "vs bottom-20% projected."
        ),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (data_dir / "backtest.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
