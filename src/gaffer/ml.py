"""Phase 2 — a trained gradient-boosted points model.

Feature engineering is leak-free (every feature uses only prior gameweeks or
static/known-before-kickoff info) and shared between training and backtest, so
the backtest is a genuine out-of-sample test. Train on older seasons, evaluate
on the most recent — see ``backtest.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gaffer import config

MODEL_PATH = config.DATA_DIR / "model" / "gaffer_gbm.joblib"
ROLL = 5
_POS = {"GK": 0, "GKP": 0, "DEF": 1, "MID": 2, "AM": 2, "FWD": 3}

FEATURES = [
    "xgi90", "r_min", "r_pts", "r_bps", "r_cs", "r_goals", "r_assists",
    "pos_code", "home", "value", "opp_def", "opp_att", "own_def", "own_att",
    "att_mult", "cs_lambda",
]
TARGET = "total_points"


def _strengths(season: str) -> dict:
    t = pd.read_csv(config.HISTORY_DIR / f"teams_{season}.csv")
    d = {c: dict(zip(t["id"], t[c], strict=False)) for c in (
        "strength_attack_home", "strength_attack_away",
        "strength_defence_home", "strength_defence_away")}
    d["name2id"] = dict(zip(t["name"], t["id"], strict=False))
    d["lg_att"] = float(t[["strength_attack_home", "strength_attack_away"]].to_numpy().mean())
    d["lg_def"] = float(t[["strength_defence_home", "strength_defence_away"]].to_numpy().mean())
    return d


def build_features(season: str) -> pd.DataFrame:
    """Load one season and attach leak-free features + target."""
    df = pd.read_csv(config.HISTORY_DIR / f"merged_gw_{season}.csv")
    df = df.sort_values(["element", "GW"]).copy()
    g = df.groupby("element")

    def rsum(s):
        return s.shift(1).rolling(ROLL, min_periods=1).sum()

    def rmean(s):
        return s.shift(1).rolling(ROLL, min_periods=1).mean()

    df["r_minsum"] = g["minutes"].transform(rsum)
    df["r_min"] = g["minutes"].transform(rmean)
    df["r_pts"] = g["total_points"].transform(rmean)
    df["r_bps"] = g["bps"].transform(rmean) if "bps" in df else 0.0
    df["r_cs"] = g["clean_sheets"].transform(rmean) if "clean_sheets" in df else 0.0
    df["r_goals"] = g["goals_scored"].transform(rmean) if "goals_scored" in df else 0.0
    df["r_assists"] = g["assists"].transform(rmean) if "assists" in df else 0.0
    xgi = g["expected_goal_involvements"].transform(rsum)
    df["xgi90"] = (xgi / (df["r_minsum"] / 90.0)).where(df["r_minsum"] > 0, 0.0)

    df["pos_code"] = df["position"].map(lambda p: _POS.get(str(p), 2)).astype(float)
    df["home"] = df["was_home"].astype(float)
    df["value"] = df.get("value", pd.Series(50, index=df.index)).astype(float)

    st = _strengths(season)
    own = df["team"].map(st["name2id"])
    opp = df["opponent_team"]
    home = df["was_home"].astype(bool)

    def look(ids, key_home, key_away):
        return np.where(home, ids.map(st[key_away]).astype(float),
                        ids.map(st[key_home]).astype(float))

    df["opp_def"] = look(opp, "strength_defence_home", "strength_defence_away")
    df["opp_att"] = look(opp, "strength_attack_home", "strength_attack_away")
    df["own_def"] = np.where(home, own.map(st["strength_defence_home"]).astype(float),
                             own.map(st["strength_defence_away"]).astype(float))
    df["own_att"] = np.where(home, own.map(st["strength_attack_home"]).astype(float),
                             own.map(st["strength_attack_away"]).astype(float))
    df["att_mult"] = np.clip(st["lg_def"] / df["opp_def"], 0.6, 1.7) * np.where(home, 1.08, 0.94)
    df["cs_lambda"] = 1.3 * (df["opp_att"] / st["lg_att"]) / (df["own_def"] / st["lg_def"])
    df["season"] = season
    return df


def train(train_seasons: tuple[str, ...] = ("2022-23", "2023-24")) -> dict:
    from joblib import dump
    from sklearn.ensemble import HistGradientBoostingRegressor

    frames = [build_features(s) for s in train_seasons]
    df = pd.concat(frames, ignore_index=True)
    df = df[(df["GW"] >= ROLL + 1) & df["r_minsum"].notna()].dropna(subset=[TARGET])

    X, y = df[FEATURES], df[TARGET]
    model = HistGradientBoostingRegressor(
        max_iter=400, learning_rate=0.05, max_depth=6,
        min_samples_leaf=40, l2_regularization=1.0, random_state=7,
    )
    model.fit(X, y)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    dump({"model": model, "features": FEATURES}, MODEL_PATH)
    return {"trained_on": list(train_seasons), "rows": int(len(df))}


def predict(df: pd.DataFrame) -> np.ndarray:
    from joblib import load

    bundle = load(MODEL_PATH)
    return bundle["model"].predict(df[bundle["features"]])


if __name__ == "__main__":
    print(train())
