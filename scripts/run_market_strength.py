"""E2 -- do market prices beat played results as Gaffer's team-strength input?

PRE-REGISTERED, before any comparison was run
=============================================

**A correction to the crossover audit, made here rather than buried.** The audit
said Gaffer's fixture adjustment reads FPL's hand-set ``strength_*`` integers.
That is true of the LIVE pipeline (``TeamContext.build``). It is NOT true of the
backtest, which calls ``histdata.team_form_ratings`` and rebuilds attack and
defence every gameweek from goals already scored, shrunk toward last season.
The control here is therefore stronger than the audit implied, and the question
is sharper: **does the market's forward view beat a shrunk goals-based form
rating?**

**Variants.** Everything is identical except the team-strength input -- same
projection function, same players, same fixtures, same shrinkage constant, same
horizons, same seasons.

``G0``  control. ``histdata.team_form_ratings(decision_gw)``: goals scored and
        conceded in fixtures STRICTLY BEFORE the decision, shrunk k=5.
``G1``  market, every quote published by the deadline INCLUDING the round being
        projected. What a manager actually has. The only variant that can say
        anything about gameweek 1.
``G2``  market, completed fixtures only. Matches G0's information set exactly,
        and so isolates "are prices a better summary of the past" from "can the
        market see the upcoming fixture".

**Primary endpoint.** Legal-XI points per gameweek at h=1, the end-task metric,
on the TEST season with the direction agreeing on train and select.

**Secondary.** MAE, rank correlation and captain accuracy at every horizon;
and the GW1 block reported separately, because that is where the control has no
information at all and where the effect should be largest if it exists anywhere.

**Success standard, fixed in advance.** Improvement on the test season AND the
same direction on train and select. Train/select up with test down is
INCONCLUSIVE or REJECT on magnitude. One season is never enough: Gaffer's own
backtest artifact records that its results INVERTED between 2024-25 and 2025-26
with only the season changed.

**Leakage.** Opening odds only; closing odds are not in the export at any
version. The provenance risk -- opening quotes carry no timestamp -- is recorded
in the producer manifest and repeated in the result.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from gaffer import histdata, leakage
from gaffer.backtest import (
    HORIZONS,
    _decision_metrics,
    _mae,
    _rank_corr,
    project_rows,
)
from gaffer.model import features as F
from scripts import market_expectations as market

EXPORT_VERSION = "2026-08-23"
FIRST_DECISION_GW = 1

CARRY = ["min_td", "starts_td", "xg90_td", "xa90_td", "defcon90_td",
         "base_minutes", "base_starts", "base_xg90", "base_xa90",
         "base_defcon90", "base_season", "value", "pos", "team_id"]


def evaluate(season: str, variant: str, horizons=HORIZONS) -> pd.DataFrame:
    """One evaluation frame. Only the TeamContext differs between variants."""
    hist = histdata.load_season(season)
    df = hist.frame
    leakage.assert_no_leakage(histdata.FEATURE_COLUMNS, context="adapter features")

    fixtures: list = []
    if variant in ("G1", "G2"):
        fixtures, _ = market.load(EXPORT_VERSION, season)
        if not fixtures:
            raise market.ExpectationsUnavailable(
                f"market export carries no rows for {season}")

    max_gw = int(df["GW"].max())
    records: list[pd.DataFrame] = []

    for decision_gw in range(FIRST_DECISION_GW, max_gw + 1):
        snap = df[df["GW"] == decision_gw]
        if snap.empty:
            continue
        feat = snap.drop_duplicates("element").set_index("element")

        if variant == "G0":
            r = hist.team_form_ratings(decision_gw)
            ctx = F.TeamContext.from_ratings(
                att_home=r["att_home"], att_away=r["att_away"],
                def_home=r["def_home"], def_away=r["def_away"],
                team_xgc=hist.team_xgc_to_date(decision_gw),
            )
        else:
            ctx = market.market_context(
                hist, decision_gw, fixtures,
                include_current_round=(variant == "G1"),
            )

        fixtures_played = hist.team_fixtures_played(decision_gw)

        for h in horizons:
            target_gw = decision_gw + h - 1
            if target_gw > max_gw:
                continue
            tgt = df[df["GW"] == target_gw].copy()
            if tgt.empty:
                continue
            for c in CARRY:
                tgt[c] = tgt["element"].map(feat[c])
            tgt = tgt.dropna(subset=["pos", "value", "opponent_team", "team_id"])
            if tgt.empty:
                continue
            tgt["pred"] = project_rows(tgt, ctx, fixtures_played)
            agg = tgt.groupby("element").agg(
                pred=("pred", "sum"),
                actual=("total_points", "sum"),
                minutes=("minutes", "sum"),
                pos=("pos", "first"),
                value=("value", "first"),
                team_id=("team_id", "first"),
            ).reset_index()
            agg["decision_gw"] = decision_gw
            agg["target_gw"] = target_gw
            agg["horizon"] = h
            agg["naive"] = agg["element"].map(
                feat["pts_td"] / feat["games_td"].replace(0, np.nan)
            ).fillna(0.0)
            records.append(agg)

    return pd.concat(records, ignore_index=True)


def summarise(ev: pd.DataFrame, label: str) -> dict:
    out: dict = {"variant": label, "rows": int(len(ev)), "per_horizon": {}}
    for h in sorted(ev["horizon"].unique()):
        sub = ev[ev["horizon"] == h]
        decisions = _decision_metrics(sub, "pred")
        out["per_horizon"][int(h)] = {
            "mae": round(_mae(sub["pred"], sub["actual"]), 4),
            "rank_corr": round(_rank_corr(sub, "pred"), 4),
            "xi_points_per_gw": decisions.get("xi_points_per_gw"),
            "captain_accuracy_pct": decisions.get("captain_accuracy_pct"),
        }
    gw1 = ev[(ev["decision_gw"] == 1) & (ev["horizon"] == 1)]
    if len(gw1):
        d = _decision_metrics(gw1, "pred")
        out["gw1"] = {
            "rows": int(len(gw1)),
            "mae": round(_mae(gw1["pred"], gw1["actual"]), 4),
            "rank_corr": round(_rank_corr(gw1, "pred"), 4),
            "xi_points": d.get("xi_points_per_gw"),
        }
    for name, upper in (("gw1_3", 3), ("gw1_6", 6)):
        block = ev[(ev["decision_gw"] <= upper) & (ev["horizon"] == 1)]
        if len(block):
            d = _decision_metrics(block, "pred")
            out[name] = {
                "mae": round(_mae(block["pred"], block["actual"]), 4),
                "rank_corr": round(_rank_corr(block, "pred"), 4),
                "xi_points_per_gw": d.get("xi_points_per_gw"),
            }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2023-24,2024-25,2025-26")
    ap.add_argument("--variants", default="G0,G1,G2")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    print(__doc__)
    print("=" * 100)

    results: dict[str, dict[str, dict]] = {}
    for season in args.seasons.split(","):
        results[season] = {}
        for variant in args.variants.split(","):
            began = time.time()
            try:
                ev = evaluate(season, variant)
            except market.ExpectationsUnavailable as exc:
                print(f"  {season} {variant}: BLOCKED — {exc}")
                continue
            results[season][variant] = summarise(ev, variant)
            print(f"  {season} {variant}: {len(ev):,} rows "
                  f"in {time.time() - began:.0f}s")

    print("\n" + "=" * 100)
    print("PRIMARY — legal-XI points per gameweek at h=1")
    print("=" * 100)
    print(f"  {'season':<10}{'G0 control':>13}{'G1 market':>12}{'G2 market-past':>16}"
          f"{'G1-G0':>10}{'G2-G0':>10}")
    print("  " + "-" * 86)
    for season, variants in results.items():
        def xi(v: str, variants=variants) -> float | None:
            # `variants` is bound as a default, not captured: a closure over a
            # loop variable reads the LAST season's numbers for every row.
            row = variants.get(v, {}).get("per_horizon", {}).get(1, {})
            return row.get("xi_points_per_gw")
        g0, g1, g2 = xi("G0"), xi("G1"), xi("G2")
        d1 = f"{g1 - g0:>+10.2f}" if (g0 and g1) else f"{'-':>10}"
        d2 = f"{g2 - g0:>+10.2f}" if (g0 and g2) else f"{'-':>10}"
        print(f"  {season:<10}{g0 or 0:>13.2f}{g1 or 0:>12.2f}"
              f"{g2 or 0:>16.2f}{d1}{d2}")

    print("\n" + "=" * 100)
    print("SECONDARY — h=1 overall, then the early-season blocks")
    print("=" * 100)
    for season, variants in results.items():
        print(f"\n  {season}")
        print(f"    {'variant':<8}{'mae':>9}{'rho':>9}{'captain %':>12}"
              f"{'GW1 xi':>10}{'GW1 rho':>10}{'GW1-3 xi':>11}{'GW1-6 xi':>11}")
        for variant, row in variants.items():
            h1 = row["per_horizon"].get(1, {})
            gw1 = row.get("gw1", {})
            print(f"    {variant:<8}{h1.get('mae') or 0:>9.4f}"
                  f"{h1.get('rank_corr') or 0:>9.4f}"
                  f"{h1.get('captain_accuracy_pct') or 0:>12.1f}"
                  f"{gw1.get('xi_points') or 0:>10.1f}"
                  f"{gw1.get('rank_corr') or 0:>10.4f}"
                  f"{row.get('gw1_3', {}).get('xi_points_per_gw') or 0:>11.1f}"
                  f"{row.get('gw1_6', {}).get('xi_points_per_gw') or 0:>11.1f}")

    print("\n" + "=" * 100)
    print("PER-HORIZON legal-XI points per gameweek")
    print("=" * 100)
    for season, variants in results.items():
        print(f"\n  {season}")
        print(f"    {'variant':<8}" + "".join(f"{f'h={h}':>10}" for h in HORIZONS))
        for variant, row in variants.items():
            cells = "".join(
                f"{row['per_horizon'].get(h, {}).get('xi_points_per_gw') or 0:>10.1f}"
                for h in HORIZONS
            )
            print(f"    {variant:<8}{cells}")

    if args.out:
        payload = {
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "export_version": EXPORT_VERSION,
            "results": results,
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
