"""Congestion in `p_start`, measured with the European fixtures included.

PRE-REGISTERED. Written and committed before any rung was scored.

WHY THIS EXISTS, AND WHY IT IS A SECOND EXPERIMENT.

`scripts/run_congestion_ablation.py` measured congestion on the Premier League
archive alone and refused it: turnaround made Brier WORSE by +0.003 to +0.012 in
all three seasons. That refusal came with a stated limitation, and this script
is that limitation being tested rather than lived with:

    a club playing Tuesday in Europe and Saturday in the league shows a
    SEVEN-day gap in a Premier-League-only archive

So the first experiment falsified a PROXY. It could not speak to the hypothesis
that motivated the work -- a Manchester City fixture sandwiched between PSG and
AEK Athens -- because the fixtures that create the sandwich were invisible to it.

`gaffer.competitions` makes them visible. This re-runs the same ladder against
a timeline that includes them.

THE POPULATION IS THE POINT. Only five or six English clubs play in Europe in a
given season, so roughly a quarter of player-fixtures can possibly differ from
the first experiment. Diluting those into the whole league would hide any
effect that exists. The primary analysis is therefore EUROPEAN CLUBS ONLY, with
the whole-league figure reported beside it so the dilution is visible rather
than assumed.

THE LADDER, unchanged in shape from the first experiment so the two are
comparable:

    E0  shipped p_start
    E1  + true turnaround     days since the club's last match IN ANY COMPETITION
    E2  + true forward density days to its next match in any competition
    E3  + true 14-day load     matches the club played in the prior fortnight

DECISION RULE, FIXED BEFORE ANY RESULT:

    Ship the HIGHEST rung that
      (a) improves h=1 Brier on European-club rows on the TEST season
          (2025-26) by at least 0.002, and
      (b) does not worsen European-club Brier on EITHER hold-out season.
    Ties go to the lower rung. If no rung satisfies both, ship nothing and
    record the refusal with its numbers.

DATA LIMITATION, stated before the result. 2025-26 carries the Champions League
only -- openfootball has no Europa or Conference file for it yet -- so a
2025-26 Europa club is treated as having no European football. That biases the
test TOWARD the null for those clubs, and the coverage report prints it.

Research code. Lives in scripts/, imported by nothing.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta

import numpy as np
import pandas as pd

from gaffer import backtest as BT
from gaffer import competitions as COMP
from gaffer import histdata

SEASONS = ("2023-24", "2024-25", "2025-26")
TEST_SEASON = "2025-26"
HOLDOUTS = ("2023-24", "2024-25")
MIN_GAIN = 0.002
TIGHT_DAYS = 3.5

#: Same weights as the first experiment, unchanged and unfitted, so the two
#: results differ only in the DATA and never in the knobs.
W_TURNAROUND = 0.10
W_FORWARD = 0.10
W_LOAD = 0.10


def _clip(x):
    return np.clip(x, 0.0, 0.98)


def euro_index(season: str) -> tuple[dict[str, list], dict]:
    fx, cov = COMP.load_season(season)
    eng = COMP.english_fixtures(fx)
    unmapped = sorted({r["unmapped_name"] for r in eng if r["unmapped_name"]})
    if unmapped:
        # Loud, not silent: an unmapped English club is a club that looks like
        # it has no European football, which is the exact error being hunted.
        print(f"  !! UNMAPPED ENGLISH CLUBS in {season}: {unmapped}", file=sys.stderr)
    cov["unmapped"] = unmapped
    cov["clubs"] = sorted({r["team"] for r in eng if r["team"]})
    return COMP.congestion_index(eng), cov


def frame(season: str) -> tuple[pd.DataFrame, dict]:
    idx, cov = euro_index(season)
    h = histdata.load_season(season)
    df = h.frame.copy()
    df["ko"] = pd.to_datetime(df["kickoff_time"], format="mixed", utc=True)

    teams = df.drop_duplicates(["team", "ko"])[["team", "ko"]].copy()
    teams["day"] = teams["ko"].dt.date
    rows = []
    for team, g in teams.groupby("team", sort=False):
        league_days = sorted(g["day"].tolist())
        euro_days = idx.get(team, [])
        allm = sorted(set(league_days) | set(euro_days))
        n_euro = len(euro_days)
        for d in league_days:
            prior = [x for x in allm if x < d]
            later = [x for x in allm if x > d]
            rows.append({
                "team": team, "day": d,
                "true_days_since": (d - max(prior)).days if prior else np.nan,
                "true_days_to_next": (min(later) - d).days if later else np.nan,
                "true_matches_14d": sum(1 for x in allm if d - timedelta(days=14) <= x < d),
                "is_euro_club": n_euro > 0,
            })
    tf = pd.DataFrame(rows)
    df["day"] = df["ko"].dt.date
    df = df.merge(tf, on=["team", "day"], how="left")
    keep = ["true_days_since", "true_days_to_next", "true_matches_14d", "is_euro_club"]
    return df.groupby(["element", "GW"], as_index=False)[keep].first(), cov


def rungs(ev: pd.DataFrame) -> dict[str, np.ndarray]:
    base = ev["p_start"].astype(float).to_numpy()
    ds = ev["true_days_since"].to_numpy(dtype=float)
    turn = np.where(np.isnan(ds), 0.0, np.clip((ds - TIGHT_DAYS) / 4.0, -1.0, 1.0))
    e1 = _clip(base + W_TURNAROUND * turn * np.where(turn > 0, 1 - base, base))
    dn = ev["true_days_to_next"].to_numpy(dtype=float)
    fwd = np.where(np.isnan(dn), 0.0, np.clip((dn - TIGHT_DAYS) / 4.0, -1.0, 1.0))
    e2 = _clip(e1 + W_FORWARD * fwd * np.where(fwd > 0, 1 - e1, e1))
    mm = ev["true_matches_14d"].to_numpy(dtype=float)
    load = np.where(np.isnan(mm), 0.0, np.clip((mm - 2.5) / 2.5, -1.0, 1.0))
    e3 = _clip(e2 - W_LOAD * load * np.where(load > 0, e2, 0.0))
    return {"E0_shipped": base, "E1_true_turnaround": e1,
            "E2_true_forward": e2, "E3_true_load": e3}


def brier(y, p) -> float:
    m = ~(np.isnan(y) | np.isnan(p))
    return float(np.mean((np.asarray(p)[m] - np.asarray(y)[m]) ** 2)) if m.sum() else float("nan")


def score(season: str) -> dict:
    ev, _ = BT.build_minutes_evaluation(season=season, horizons=(1,))
    ev = ev[ev["horizon"] == 1].copy()
    cg, cov = frame(season)
    ev = ev.merge(cg, left_on=["decision_gw", "element"],
                  right_on=["GW", "element"], how="left")
    euro = ev["is_euro_club"].fillna(False).astype(bool).to_numpy()
    y = ev["started"].astype(float).to_numpy()
    r = rungs(ev)
    out = {"season": season, "coverage": cov, "n_all": int(len(ev)),
           "n_euro": int(euro.sum()), "rungs": {}}
    for name, p in r.items():
        out["rungs"][name] = {
            "brier_euro": round(brier(y[euro], p[euro]), 5),
            "brier_all": round(brier(y, p), 5),
        }
    b = out["rungs"]["E0_shipped"]
    for v in out["rungs"].values():
        v["d_euro"] = round(v["brier_euro"] - b["brier_euro"], 5)
        v["d_all"] = round(v["brier_all"] - b["brier_all"], 5)
    # How different is the timeline at all? If Europe never moves the number,
    # the experiment has no exposure and the result means nothing.
    changed = int((ev.loc[euro, "true_days_since"] < 6).sum())
    out["euro_rows_with_tight_turnaround"] = changed
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.parse_args()
    res = {}
    for s in SEASONS:
        try:
            res[s] = score(s)
        except Exception as e:
            print(f"{s}: FAILED {type(e).__name__}: {e}", file=sys.stderr)
    if not res:
        return 1

    print("\nCOVERAGE")
    for s, r in res.items():
        c = r["coverage"]
        print(f"  {s}: found {c['found']}  missing {list(c['missing'])}  "
              f"clubs {c['clubs']}")
        print(f"      euro rows {r['n_euro']} of {r['n_all']}  "
              f"(tight turnaround among them: {r['euro_rows_with_tight_turnaround']})")

    print("\nBrier at h=1 on EUROPEAN-CLUB rows (delta vs shipped in brackets)")
    print("-" * 78)
    names = list(next(iter(res.values()))["rungs"])
    header = "  ".join(f"{s:<17}" for s in res)
    print(f"{'rung':<22} {header}")
    for n in names:
        cells = [f"{res[s]['rungs'][n]['brier_euro']:.5f} "
                 f"({res[s]['rungs'][n]['d_euro']:+.5f})" for s in res]
        row = "  ".join(f"{c:<17}" for c in cells)
        print(f"{n:<22} {row}")

    print("\nSame rungs on ALL rows, to show the dilution")
    for n in names:
        cells = [f"{res[s]['rungs'][n]['brier_all']:.5f} "
                 f"({res[s]['rungs'][n]['d_all']:+.5f})" for s in res]
        row = "  ".join(f"{c:<17}" for c in cells)
        print(f"{n:<22} {row}")

    print("\nDECISION (rule fixed before running):")
    winner = None
    for n in reversed(names):
        if n == "E0_shipped":
            continue
        test = res[TEST_SEASON]["rungs"][n]["d_euro"]
        holds = [res[h]["rungs"][n]["d_euro"] for h in HOLDOUTS if h in res]
        ok = test <= -MIN_GAIN and all(d <= 0 for d in holds)
        hs = " ".join(f"{d:+.5f}" for d in holds)
        print(f"  {n:<22} test {test:+.5f}  holdouts {hs}  -> "
              f"{'PASS' if ok else 'fail'}")
        if ok and winner is None:
            winner = n
    verdict = ("SHIP " + winner) if winner else \
        "SHIP NOTHING -- no rung met the pre-registered bar"
    print(f"\n  RESULT: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
