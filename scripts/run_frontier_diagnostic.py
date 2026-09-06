"""Does Gaffer order the players a decision could actually reach?

Research code. It has NO pipeline caller, writes nothing the product reads, and
is deliberately not a gate. Its result is a scientific finding about where the
projection's ordering lives; whether anything ships is a separate decision made
by a person.

**The question.** Whole-population rank correlation says the naive baseline
orders players better than Gaffer, at every horizon, in every arm. Legal-XI
points say the opposite. Both are computed correctly. 61.3% of the h=1 rows are
player-gameweeks with zero minutes, and no legal squad can contain most of the
population, so ordering the whole archive is not the same question as ordering
the handful of players a squad could actually be built from.

Raised by external review, 2026-09-06 (GPT-6 Astra), whose first-pass numbers
this reproduces.

**Pre-registration.** These comparison sets were fixed before the results below
were looked at, and are listed here rather than in a commit message so a later
reader can see there was no search over cutoffs:

    1. all evaluated players                     (the published metric)
    2. union of each model's top 30
    3. union of each model's top 50
    4. union of each model's top 100
    5. position x price band                     (4 positions x 3 bands)
    6. players the two policies select DIFFERENTLY into a legal XI

Set membership in 2-4 and 6 depends only on the two FORECASTS, never on the
outcome, so none of them is an outcome-selected population. Set 5 depends only
on price and position, both known before kickoff.

"Players in or near the current squad" is in the review's list and is NOT
implemented: this harness re-selects a free squad every gameweek and has no
held squad to be near. Answering it needs the trajectory harness, and
inventing a squad here would make the set a property of whichever policy
built it. Recorded as not-done rather than approximated.

**Arms.** Shipped Gaffer, DEFCON-disabled Gaffer, and the fixture-aware naive
baseline (`naive_fx`), which is the only like-for-like one: `pred` is summed
across a double gameweek and the unscaled `naive` is not.

Run:  PYTHONPATH=src python scripts/run_frontier_diagnostic.py [out.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from gaffer import backtest as bt
from gaffer import config

#: Pre-registered. Do not add a cutoff after seeing a result.
TOP_N = (30, 50, 100)
PRICE_BANDS = ((0, 50), (50, 70), (70, 1000))   # tenths of a million
BLOCK = 4                                       # gameweeks per bootstrap block
BOOTSTRAPS = 10_000
SEED = 20260906


def _within_gw_spearman(grp: pd.DataFrame, col: str) -> float | None:
    if len(grp) < 10 or grp[col].std() <= 0:
        return None
    v = grp[col].rank().corr(grp["actual"].rank())
    return None if pd.isna(v) else float(v)


def _mean(vals: list[float]) -> float | None:
    return round(float(np.mean(vals)), 4) if vals else None


def frontier(df: pd.DataFrame, arms: tuple[str, ...]) -> dict:
    """Within-gameweek Spearman on each pre-registered set, averaged over weeks."""
    acc: dict[str, dict[str, list[float]]] = {
        name: {a: [] for a in arms}
        for name in ("all", *(f"top{n}" for n in TOP_N))
    }
    for pos in ("GKP", "DEF", "MID", "FWD"):
        for lo, hi in PRICE_BANDS:
            acc[f"{pos}_{lo}-{hi}"] = {a: [] for a in arms}

    for _, grp in df.groupby("target_gw"):
        for a in arms:
            v = _within_gw_spearman(grp, a)
            if v is not None:
                acc["all"][a].append(v)
        for n in TOP_N:
            idx: set = set()
            for a in arms:
                idx |= set(grp.nlargest(n, a).index)
            sub = grp.loc[sorted(idx)]
            for a in arms:
                v = _within_gw_spearman(sub, a)
                if v is not None:
                    acc[f"top{n}"][a].append(v)
        for pos in ("GKP", "DEF", "MID", "FWD"):
            for lo, hi in PRICE_BANDS:
                sub = grp[(grp["pos"] == pos) & (grp["value"] >= lo)
                          & (grp["value"] < hi)]
                for a in arms:
                    v = _within_gw_spearman(sub, a)
                    if v is not None:
                        acc[f"{pos}_{lo}-{hi}"][a].append(v)
    return {k: {a: _mean(v) for a, v in d.items()} for k, d in acc.items()}


def disagreement(df: pd.DataFrame, a: str, b: str) -> dict:
    """Set 6: only the players the two policies field differently.

    The realised points of the players ONE policy starts and the other does
    not. This is the narrowest set on which a projection difference can change
    a score at all: everywhere else the two policies field the same player and
    the outcome cancels.
    """
    gains, weeks = [], 0
    for gw, grp in df.groupby("target_gw"):
        g = grp.dropna(subset=["value", "team_id"]).copy()
        if len(g) < 40:
            continue
        xis = {}
        for col in (a, b):
            sq = bt._select_squad(g, col)
            if sq is None:
                break
            xi = bt._best_xi(g, sq, col)
            if len(xi) != 11:
                break
            xis[col] = set(xi)
        if len(xis) != 2:
            continue
        only_a, only_b = xis[a] - xis[b], xis[b] - xis[a]
        weeks += 1
        gains.append({
            "gw": int(gw), "n_differ": len(only_a),
            "a_points": float(g.loc[sorted(only_a), "actual"].sum()),
            "b_points": float(g.loc[sorted(only_b), "actual"].sum()),
        })
    d = np.array([x["a_points"] - x["b_points"] for x in gains], float)
    rng = np.random.default_rng(SEED)
    n, nb = len(d), int(np.ceil(len(d) / BLOCK))
    boots = []
    for _ in range(BOOTSTRAPS):
        starts = rng.integers(0, n, nb)
        samp = np.concatenate(
            [[d[(s + j) % n] for j in range(BLOCK)] for s in starts])[:n]
        boots.append(samp.mean())
    return {
        "weeks": weeks,
        "mean_players_differing": round(float(np.mean([x["n_differ"] for x in gains])), 2),
        "mean_paired_gain": round(float(d.mean()), 3),
        "block_bootstrap_ci95": [round(float(np.percentile(boots, 2.5)), 3),
                                 round(float(np.percentile(boots, 97.5)), 3)],
        "weeks_a_better": int((d > 0).sum()), "weeks_b_better": int((d < 0).sum()),
        "per_gw": gains,
    }


def main(argv: list[str]) -> int:
    out_path = Path(argv[1]) if len(argv) > 1 else None
    original = dict(config.DEFCON_THRESHOLD)
    result: dict = {"pre_registered_sets": ["all", *[f"top{n}" for n in TOP_N],
                                            "position_x_price_band",
                                            "policy_disagreement"],
                    "not_implemented": {
                        "near_current_squad":
                            "this harness re-selects a free squad every "
                            "gameweek and holds no squad to be near; answering "
                            "it needs the trajectory harness"},
                    "season": bt.TEST_SEASON,
                    "arms": {}}
    for arm, thr in (("shipped", original),
                     ("defcon_off", {k: 999 for k in original})):
        config.DEFCON_THRESHOLD = thr
        ev, cov = bt.build_evaluation(bt.TEST_SEASON, (1,))
        df = ev[ev.horizon == 1].copy()
        # GW1's naive baseline is identically zero, so it cannot order anything.
        df = df[df.target_gw > bt.FIRST_DECISION_GW]
        result["arms"][arm] = {
            "rows": int(len(df)),
            "gameweeks": int(df.target_gw.nunique()),
            "zero_minute_share": round(float((df["minutes"] == 0).mean()), 4),
            "rank_correlation": frontier(df, ("pred", "naive_fx")),
            "policy_disagreement_pred_minus_naive_fx":
                disagreement(df, "pred", "naive_fx"),
        }
    config.DEFCON_THRESHOLD = original

    if out_path:
        out_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    for arm, r in result["arms"].items():
        print(f"\n=== {arm}  rows={r['rows']}  gws={r['gameweeks']}  "
              f"zero-minute={r['zero_minute_share']:.1%}")
        print(f"  {'set':<18}{'pred':>9}{'naive_fx':>10}")
        for k, v in r["rank_correlation"].items():
            if v["pred"] is None and v["naive_fx"] is None:
                continue
            print(f"  {k:<18}{str(v['pred']):>9}{str(v['naive_fx']):>10}")
        d = r["policy_disagreement_pred_minus_naive_fx"]
        print(f"  disagreement: {d['mean_players_differing']} players/wk, "
              f"paired gain {d['mean_paired_gain']:+.2f} "
              f"CI {d['block_bootstrap_ci95']} "
              f"({d['weeks_a_better']}W/{d['weeks_b_better']}L)")
    print("\nA scientific result. Not a gate, and not a reason to ship anything.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
