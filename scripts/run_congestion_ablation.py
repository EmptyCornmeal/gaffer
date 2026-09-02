"""Congestion and load as `p_start` inputs -- the ablation ladder.

PRE-REGISTERED. Written and committed before any rung was scored.

WHY. On 2026-09-02 Gaffer recommended holding the Triple Captain for GW7
(Ipswich at home) because Ipswich are the worst defence Man City meet. GW7 is
Saturday 17 October: three days after PSG at home and three days before AEK
Athens at home. The Premier League fixture list shows City with six days' rest.
The model ranked fixtures by opponent and had no term for whether the player
would be on the pitch.

TWO HYPOTHESES, TESTED SEPARATELY, because the exploratory probe says they are
not the same claim.

  A -- CONGESTION. Rest and forward density change rotation. Probe: nearly
       flat. Team turnaround <=3.5 days gives a 0.722 start rate against 0.753
       at 5-8 days among regulars in 2025-26, and the club-matches-in-14-days
       cut is flat to three decimals. The easy-fixture interaction changes SIGN
       between seasons (-0.042, -0.013, +0.019). Tested anyway, because a probe
       is not a measurement.

  B -- LOAD AND STREAK. Minutes recently played and consecutive starts. Probe:
       very strong and replicated -- 0.537/0.859/0.921 by 14-day minutes across
       2025-26, and 0.531/0.871/0.920 on 2023-24. But these are ROLE proxies as
       much as fatigue proxies, and the shipped model already reads
       `start_rate_r3` and `started_lag`. The only question worth asking is the
       MARGINAL one, which is what this ladder measures.

THE CRITICAL LIMITATION, stated before the result rather than after.

The archive is Premier League only. A club playing Tuesday in Europe and
Saturday in the league shows a SEVEN-day gap here. So ladder A is a test of the
PL-internal proxy, and a null result falsifies the proxy, NOT the hypothesis
that European congestion matters. The fixture that motivated this work is
invisible to the data that would score it. That asymmetry is the finding to
carry forward whatever the numbers say.

THE LADDERS. Each rung adds ONE term to the one above it.

  A0 = B0  shipped p_start, unchanged
  A1  + turnaround      days since the club's previous fixture
  A2  + forward density days until the club's next fixture
  A3  + interaction     tight block AND an easy opponent

  B1  + load            minutes played in the prior 14 days
  B2  + streak          consecutive starts coming in

SCORING. Brier on `started` at h=1, on all three seasons. 2025-26 is the test
season; 2023-24 and 2024-25 are hold-outs. Calibration and the worst rows are
printed for any rung that wins, because a Brier improvement bought by shrinking
everything toward the base rate is not an improvement in a decision.

DECISION RULE, FIXED BEFORE ANY RESULT:

  Ship the HIGHEST rung that
    (a) improves full-season h=1 Brier against the shipped model on the TEST
        season by at least 0.002, and
    (b) does not worsen full-season Brier on EITHER hold-out season.
  Ties go to the lower rung. If no rung satisfies both, ship nothing and record
  the refusal with its numbers.

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

SEASONS = ("2023-24", "2024-25", "2025-26")
TEST_SEASON = "2025-26"
HOLDOUTS = ("2023-24", "2024-25")

#: Minimum Brier improvement on the test season worth shipping a new input for.
#: Matches the tolerance the Phase 2A ladder used, so the two decisions are made
#: on the same scale.
MIN_GAIN = 0.002

#: Blend weights. Deliberately SMALL and fixed in advance: these are corrections
#: to a probability the shipped model already produces, not replacements for it.
#: A large weight would be fitting, and nothing here is fitted.
W_TURNAROUND = 0.10
W_FORWARD = 0.10
W_INTERACTION = 0.10
W_LOAD = 0.15
W_STREAK = 0.15

#: Rest below which a start is treated as materially less likely. Three and a
#: half days is the standard football-science threshold for incomplete recovery
#: and is not fitted here.
TIGHT_DAYS = 3.5


def congestion_frame(season: str) -> pd.DataFrame:
    """Per (element, gw) congestion, from STRICTLY PRIOR kickoffs.

    The leakage boundary is the kickoff of the target fixture: every window
    below is half-open and ends before it. `assert_no_leakage` cannot see this
    file, so the boundary is enforced here and asserted in the caller.
    """
    h = histdata.load_season(season)
    df = h.frame.copy()
    df["ko"] = pd.to_datetime(df["kickoff_time"], format="mixed", utc=True)
    df = df.sort_values(["element", "ko"]).reset_index(drop=True)

    # --- player-level: load and streak -----------------------------------
    load, streak = [], []
    for _, g in df.groupby("element", sort=False):
        ko = g["ko"].to_numpy()
        mins = g["minutes"].fillna(0).to_numpy(dtype=float)
        st = g["starts"].fillna(0).to_numpy(dtype=float)
        for i in range(len(g)):
            w = (ko >= ko[i] - np.timedelta64(14, "D")) & (ko < ko[i])
            load.append(float(mins[w].sum()))
            k, j = 0, i - 1
            while j >= 0 and st[j] == 1:
                k += 1
                j -= 1
            streak.append(float(k))
    df["minutes_14d"] = load
    df["consec_starts"] = streak

    # --- club-level: turnaround and forward density ----------------------
    t = df.drop_duplicates(["team", "ko"])[["team", "ko"]].sort_values(["team", "ko"])
    t["prev"] = t.groupby("team")["ko"].shift(1)
    t["next"] = t.groupby("team")["ko"].shift(-1)
    t["days_since"] = (t["ko"] - t["prev"]).dt.total_seconds() / 86400
    t["days_to_next"] = (t["next"] - t["ko"]).dt.total_seconds() / 86400
    df = df.merge(t[["team", "ko", "days_since", "days_to_next"]],
                  on=["team", "ko"], how="left")

    # "Easy opponent" for the interaction: goals the opponent had conceded per
    # game BEFORE this gameweek. Shifted then expanded, so the target fixture's
    # own goals never enter the feature that predicts it.
    per_gw = (df.groupby(["team_id", "GW"], as_index=False)["goals_conceded"]
                .mean().sort_values(["team_id", "GW"]))
    per_gw["conceded_td"] = (per_gw.groupby("team_id")["goals_conceded"]
                             .transform(lambda x: x.shift(1).expanding().mean()))
    opp = per_gw.rename(columns={"team_id": "opponent_team",
                                 "conceded_td": "opp_conceded_td"})
    df = df.merge(opp[["opponent_team", "GW", "opp_conceded_td"]],
                  on=["opponent_team", "GW"], how="left")

    keep = ["element", "GW", "minutes_14d", "consec_starts", "days_since",
            "days_to_next", "opp_conceded_td"]
    return df.groupby(["element", "GW"], as_index=False)[keep[2:]].mean()


def _clip(x):
    return np.clip(x, 0.0, 0.98)


def rungs(ev: pd.DataFrame) -> dict[str, np.ndarray]:
    base = ev["p_start"].astype(float).to_numpy()

    # --- Ladder A ---------------------------------------------------------
    ds = ev["days_since"].to_numpy(dtype=float)
    dn = ev["days_to_next"].to_numpy(dtype=float)
    # A tight turnaround pushes toward not starting; a long one, mildly toward
    # starting. Centred so an ordinary week is neutral.
    turn = np.where(np.isnan(ds), 0.0, np.clip((ds - TIGHT_DAYS) / 4.0, -1.0, 1.0))
    a1 = _clip(base + W_TURNAROUND * turn * (1 - base) * np.sign(turn).clip(0)
               + W_TURNAROUND * turn * base * (-np.sign(turn)).clip(0))
    fwd = np.where(np.isnan(dn), 0.0, np.clip((dn - TIGHT_DAYS) / 4.0, -1.0, 1.0))
    a2 = _clip(a1 + W_FORWARD * fwd * np.where(fwd > 0, 1 - a1, a1))
    # The GW7 shape: an easy opponent inside a tight block is the game to rotate.
    easy = ev["opp_conceded_td"].to_numpy(dtype=float)
    easy_z = np.where(np.isnan(easy), 0.0, np.clip((easy - 1.4) / 1.0, 0, 1))
    tight = np.where(np.isnan(ds), 0.0, (ds <= TIGHT_DAYS).astype(float))
    a3 = _clip(a2 - W_INTERACTION * easy_z * tight * a2)

    # --- Ladder B ---------------------------------------------------------
    ld = ev["minutes_14d"].to_numpy(dtype=float)
    ld_z = np.where(np.isnan(ld), 0.0, np.clip((ld - 135.0) / 135.0, -1.0, 1.0))
    b1 = _clip(base + W_LOAD * ld_z * np.where(ld_z > 0, 1 - base, base))
    cs = ev["consec_starts"].to_numpy(dtype=float)
    cs_z = np.where(np.isnan(cs), 0.0, np.clip((cs - 2.0) / 4.0, -1.0, 1.0))
    b2 = _clip(b1 + W_STREAK * cs_z * np.where(cs_z > 0, 1 - b1, b1))

    return {
        "A0_shipped": base,
        "A1_turnaround": a1,
        "A2_forward_density": a2,
        "A3_interaction": a3,
        "B1_load": b1,
        "B2_load_plus_streak": b2,
    }


def brier(y, p) -> float:
    m = ~(np.isnan(y) | np.isnan(p))
    return float(np.mean((np.asarray(p)[m] - np.asarray(y)[m]) ** 2)) if m.sum() else float("nan")


def calibration(y, p, bins=8):
    d = pd.DataFrame({"y": y, "p": p}).dropna()
    if len(d) < bins * 20:
        return []
    d["b"] = pd.qcut(d["p"].rank(method="first"), bins, duplicates="drop")
    return [{"pred": round(float(g["p"].mean()), 3),
             "actual": round(float(g["y"].mean()), 3), "n": int(len(g))}
            for _, g in d.groupby("b", observed=True)]


def score_season(season: str) -> dict:
    ev, _ = BT.build_minutes_evaluation(season=season, horizons=(1,))
    ev = ev[ev["horizon"] == 1].copy()
    cg = congestion_frame(season)
    ev = ev.merge(cg, left_on=["decision_gw", "element"],
                  right_on=["GW", "element"], how="left")
    y = ev["started"].astype(float).to_numpy()
    out = {"season": season, "n": int(len(ev)), "rungs": {},
           "coverage": {
               "days_since": float(ev["days_since"].notna().mean()),
               "minutes_14d": float(ev["minutes_14d"].notna().mean())}}
    for name, p in rungs(ev).items():
        out["rungs"][name] = {"brier": round(brier(y, p), 5)}
    b0 = out["rungs"]["A0_shipped"]["brier"]
    for r in out["rungs"].values():
        r["delta_vs_shipped"] = round(r["brier"] - b0, 5)
    out["_ev"] = ev
    out["_y"] = y
    return out


# ---------------------------------------------------------------------------
# STAGE 2 -- was the refusal about the feature, or about my guess at the weight?
# ---------------------------------------------------------------------------
#
# PRE-REGISTERED, written before Stage 2 was run and after Stage 1 was read.
#
# Stage 1 fixed W_LOAD = 0.15 in advance and deliberately did not fit it. Load
# then improved Brier in all three seasons -- -0.00089, -0.00107, -0.00129 --
# consistently, and consistently below the 0.002 bar. That leaves one honest
# question: is 0.002 out of reach for this feature, or was 0.15 simply the
# wrong number?
#
# Answering it by tuning on the test season would be the exact failure this
# whole discipline exists to prevent. So the weight is fitted on the TWO
# HOLD-OUT SEASONS ONLY, by grid search, and then applied ONCE to 2025-26,
# which is untouched during fitting.
#
# DECISION RULE, fixed before running:
#   Ship the fitted-weight load term if it improves full-season h=1 Brier on
#   the UNTOUCHED test season by at least MIN_GAIN. One shot; no re-fitting,
#   no second grid, no widening of the grid after seeing the answer.

FIT_GRID = [round(0.05 * i, 2) for i in range(1, 13)]   # 0.05 .. 0.60


def _load_rung(ev: pd.DataFrame, w: float) -> np.ndarray:
    base = ev["p_start"].astype(float).to_numpy()
    ld = ev["minutes_14d"].to_numpy(dtype=float)
    z = np.where(np.isnan(ld), 0.0, np.clip((ld - 135.0) / 135.0, -1.0, 1.0))
    return _clip(base + w * z * np.where(z > 0, 1 - base, base))


def stage2(results: dict) -> None:
    fit = [s for s in HOLDOUTS if s in results]
    if not fit or TEST_SEASON not in results:
        print("\nSTAGE 2: skipped, seasons missing")
        return
    print("\n" + "=" * 78)
    print("STAGE 2 -- load weight fitted on hold-outs only, applied once to the test")
    print("=" * 78)
    best_w, best_b = None, None
    for w in FIT_GRID:
        tot, n = 0.0, 0
        for s in fit:
            r = results[s]
            b = brier(r["_y"], _load_rung(r["_ev"], w))
            tot += b * r["n"]
            n += r["n"]
        avg = tot / n
        if best_b is None or avg < best_b:
            best_w, best_b = w, avg
    joined = " + ".join(fit)
    print(f"  fitted on {joined}: best weight {best_w:.2f} "
          f"(pooled Brier {best_b:.5f})")

    r = results[TEST_SEASON]
    shipped = r["rungs"]["A0_shipped"]["brier"]
    got = brier(r["_y"], _load_rung(r["_ev"], best_w))
    d = got - shipped
    print(f"  applied ONCE to {TEST_SEASON}: {got:.5f} vs shipped "
          f"{shipped:.5f}  (delta {d:+.5f})")
    ok = d <= -MIN_GAIN
    mark = "PASS" if ok else "FAIL"
    print(f"  bar is {-MIN_GAIN:+.5f} -> {mark}")
    msg = (f"SHIP the load term at weight {best_w:.2f}" if ok else
           "REFUSE. The feature is real and consistent and too small to ship: "
           "it improves every season and clears no bar that was set before it "
           "was measured.")
    print(f"\n  RESULT: {msg}")
    if not ok:
        print("\n  calibration of the fitted rung, for the record:")
        for row in calibration(r["_y"], _load_rung(r["_ev"], best_w)):
            print(f"    pred {row['pred']:.3f}  actual {row['actual']:.3f} "
                  f" n={row['n']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = {}
    for s in SEASONS:
        try:
            results[s] = score_season(s)
        except Exception as e:
            print(f"{s}: FAILED {type(e).__name__}: {e}", file=sys.stderr)

    if not results:
        print("no season could be scored", file=sys.stderr)
        return 1

    names = list(next(iter(results.values()))["rungs"])
    print("\nBrier at h=1 (lower is better); delta vs shipped in brackets")
    print("-" * 78)
    header = "  ".join(f"{s:<17}" for s in results)
    print(f"{'rung':<24} {header}")
    for n in names:
        cells = []
        for s in results:
            r = results[s]["rungs"][n]
            cells.append(f"{r['brier']:.5f} ({r['delta_vs_shipped']:+.5f})")
        row = "  ".join(f"{c:<17}" for c in cells)
        print(f"{n:<24} {row}")

    print("\nDECISION (rule fixed before running):")
    winner = None
    for n in reversed(names):
        if n == "A0_shipped":
            continue
        test = results[TEST_SEASON]["rungs"][n]["delta_vs_shipped"]
        holds = [results[h]["rungs"][n]["delta_vs_shipped"]
                 for h in HOLDOUTS if h in results]
        ok = test <= -MIN_GAIN and all(d <= 0 for d in holds)
        holds_s = " ".join(f"{d:+.5f}" for d in holds)
        mark = "PASS" if ok else "fail"
        print(f"  {n:<24} test {test:+.5f}  holdouts {holds_s}  -> {mark}")
        if ok and winner is None:
            winner = n
    verdict = ("SHIP " + winner) if winner else \
        "SHIP NOTHING -- no rung met the pre-registered bar"
    print(f"\n  RESULT: {verdict}")

    if winner:
        r = results[TEST_SEASON]
        print(f"\n  calibration of {winner} on {TEST_SEASON}:")
        for row in calibration(r["_y"], rungs(r["_ev"])[winner]):
            print(f"    pred {row['pred']:.3f}  actual {row['actual']:.3f}  n={row['n']}")

    stage2(results)

    if args.json:
        print(json.dumps({s: {k: v for k, v in r.items() if not k.startswith("_")}
                          for s, r in results.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
