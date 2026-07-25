"""Backtest the projection approach on a past season (vaastav dataset).

Answers "how accurate is it?" honestly: for each past gameweek we build a
Gaffer-style projection from data available *before* that GW (rolling xGI, minutes,
clean sheets — no leakage), then score it against actual points. We benchmark
against two references from the same data: FPL's own ``xP`` and a naive baseline
(the player's recent average). Reports MAE (lower better) and rank correlation
(higher better — how well each method *orders* players, which is what matters for
picking a team).

Note: the historical projection omits the fixture/opponent adjustment the live
model uses (that data isn't in the merged file), so it is a conservative proxy of
``heuristic-0.1`` — the live model should do at least this well.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from gaffer import config

SEASON = "2024-25"
URL = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/"
    f"data/{SEASON}/gws/merged_gw.csv"
)
ROLL = 5  # gameweeks of history
MIN_GW = ROLL + 1  # start predicting once enough history exists

_GOAL_ISH = 5.0  # blended points per expected goal-involvement (mix of G/A)
_CS_PTS = {"GKP": 4, "GK": 4, "DEF": 4, "MID": 1, "AM": 1, "FWD": 0}


TEAMS_URL = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/"
    f"data/{SEASON}/teams.csv"
)


def _download(dest: Path, url: str = URL, min_size: int = 10_000) -> Path:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or dest.stat().st_size < min_size:
        r = httpx.get(url, timeout=60.0, follow_redirects=True)
        r.raise_for_status()
        dest.write_bytes(r.content)
    return dest


def _team_strength() -> dict[str, dict[int, float]]:
    """id -> attack/defence home/away strengths, from vaastav teams.csv."""
    csv = _download(config.HISTORY_DIR / f"teams_{SEASON}.csv", TEAMS_URL, min_size=200)
    t = pd.read_csv(csv)
    d: dict[str, dict[int, float]] = {}
    for col in ("strength_attack_home", "strength_attack_away",
                "strength_defence_home", "strength_defence_away"):
        d[col] = dict(zip(t["id"], t[col], strict=False))
    d["name2id"] = dict(zip(t["name"], t["id"], strict=False))
    league = t[["strength_attack_home", "strength_attack_away"]].to_numpy().mean()
    league_def = t[["strength_defence_home", "strength_defence_away"]].to_numpy().mean()
    d["_league"] = {"att": float(league), "def": float(league_def)}
    return d


def _mae(pred: pd.Series, actual: pd.Series) -> float:
    return float((pred - actual).abs().mean())


def _lift(df: pd.DataFrame, col: str) -> dict[str, float]:
    """Per GW, avg actual points of the top-20%-projected vs bottom-20%-projected."""
    tops, bots = [], []
    for _, grp in df.groupby("GW"):
        if len(grp) < 20:
            continue
        q_hi, q_lo = grp[col].quantile(0.8), grp[col].quantile(0.2)
        tops.append(grp.loc[grp[col] >= q_hi, "total_points"].mean())
        bots.append(grp.loc[grp[col] <= q_lo, "total_points"].mean())
    return {
        "top": round(float(pd.Series(tops).mean()), 2),
        "bottom": round(float(pd.Series(bots).mean()), 2),
    }


def _avg_rank_corr(df: pd.DataFrame, col: str) -> float:
    """Mean per-GW Spearman correlation between a projection column and actual."""
    corrs = []
    for _, grp in df.groupby("GW"):
        if len(grp) >= 10 and grp[col].std() > 0:
            # Spearman = Pearson of ranks (avoids a scipy dependency)
            corr = grp[col].rank().corr(grp["total_points"].rank())
            if pd.notna(corr):
                corrs.append(corr)
    return float(pd.Series(corrs).mean()) if corrs else 0.0


def run(data_dir: Path | None = None) -> dict[str, Any]:
    data_dir = data_dir or config.DATA_DIR
    csv = _download(config.HISTORY_DIR / f"merged_gw_{SEASON}.csv")
    df = pd.read_csv(csv)

    keep = ["element", "GW", "minutes", "total_points", "xP", "position",
            "expected_goal_involvements", "clean_sheets",
            "team", "opponent_team", "was_home"]
    df = df[[c for c in keep if c in df.columns]].copy()
    df = df.sort_values(["element", "GW"])
    g = df.groupby("element")

    def roll_sum(s):
        return s.shift(1).rolling(ROLL, min_periods=1).sum()

    def roll_mean(s):
        return s.shift(1).rolling(ROLL, min_periods=1).mean()

    df["r_minsum"] = g["minutes"].transform(roll_sum)
    df["r_min"] = g["minutes"].transform(roll_mean)
    df["r_xgi"] = g["expected_goal_involvements"].transform(roll_sum)
    df["r_cs"] = g["clean_sheets"].transform(roll_mean) if "clean_sheets" in df else 0.0
    df["r_pts"] = g["total_points"].transform(roll_mean)  # naive baseline

    # --- fixture adjustment from team strengths (mirrors the live model) ---
    st = _team_strength()
    lg_att, lg_def = st["_league"]["att"], st["_league"]["def"]
    own_id = df["team"].map(st["name2id"])
    opp = df["opponent_team"]
    home = df["was_home"].astype(bool)
    import numpy as np

    def look(ids, table):
        return ids.map(table).astype(float)

    # opponent defends at their own venue: player home -> opp away
    opp_def = np.where(home, look(opp, st["strength_defence_away"]),
                       look(opp, st["strength_defence_home"]))
    att_mult = np.clip(lg_def / opp_def, 0.6, 1.7) * np.where(home, 1.08, 0.94)
    opp_att = np.where(home, look(opp, st["strength_attack_away"]),
                       look(opp, st["strength_attack_home"]))
    own_def = np.where(home, look(own_id, st["strength_defence_home"]),
                       look(own_id, st["strength_defence_away"]))
    # expected goals conceded -> clean-sheet probability (Poisson P(0))
    lam = 1.3 * (opp_att / lg_att) / (own_def / lg_def) * np.where(home, 0.9, 1.12)
    p_cs = np.exp(-np.clip(lam, 0.15, 4.0))

    # Gaffer-style projection (no leakage: uses only prior GWs)
    xgi90 = (df["r_xgi"] / (df["r_minsum"] / 90.0)).where(df["r_minsum"] > 0, 0.0)
    exp_min = df["r_min"].clip(0, 90).fillna(0)
    appearance = (exp_min >= 60).astype(float) * 2 + ((exp_min > 0) & (exp_min < 60)).astype(float)
    attack = xgi90 * (exp_min / 90.0) * _GOAL_ISH * att_mult
    cs_pts = df["position"].map(lambda p: _CS_PTS.get(str(p), 0)).astype(float)
    cs = p_cs * cs_pts * (exp_min >= 60).astype(float)
    df["model"] = appearance + attack + cs

    # evaluate on realistic rows: enough history + the player actually featured
    ev = df[(df["GW"] >= MIN_GW) & (df["minutes"] > 0) & df["r_minsum"].notna()].copy()
    ev = ev.dropna(subset=["total_points", "model"])
    ev["fpl"] = ev["xP"].fillna(0)
    ev["naive"] = ev["r_pts"].fillna(0)

    out = {
        "season": SEASON,
        "n_predictions": int(len(ev)),
        "gameweeks": f"GW{MIN_GW}–GW{int(ev['GW'].max())}",
        "mae": {
            "gaffer": round(_mae(ev["model"], ev["total_points"]), 3),
            "fpl_xp": round(_mae(ev["fpl"], ev["total_points"]), 3),
            "naive": round(_mae(ev["naive"], ev["total_points"]), 3),
        },
        "rank_corr": {
            "gaffer": round(_avg_rank_corr(ev, "model"), 3),
            "fpl_xp": round(_avg_rank_corr(ev, "fpl"), 3),
            "naive": round(_avg_rank_corr(ev, "naive"), 3),
        },
        "lift": {
            "gaffer": _lift(ev, "model"),
            "fpl_xp": _lift(ev, "fpl"),
        },
        "note": (
            "Backtest of a heuristic-0.1-style projection (fixture-adjusted, but "
            "pre-DEFCON — that scoring is new for 2025/26). Rank correlation = how "
            "well a method orders players by points (higher better); MAE = average "
            "points error (lower better); lift = avg actual points of the top-20% "
            "vs bottom-20% projected. FPL's own xP is the benchmark to beat — the "
            "gap is why the trained model (Phase 2) is the priority."
        ),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (data_dir / "backtest.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
