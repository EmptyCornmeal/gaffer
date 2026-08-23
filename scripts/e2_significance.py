"""E2 significance -- is the legal-XI difference distinguishable from noise?

``xi_points_per_gw`` is a mean over 38 gameweeks of a quantity whose
gameweek-to-gameweek standard deviation is large. A headline gap of "+3.7 points
per gameweek" is meaningless until it is put next to the spread of the thing
being averaged, and the whole point of the crossover programme is not to promote
a number that a second season would erase.

So the two variants are compared **gameweek by gameweek, paired**. Both pick
their squad from the same player pool, in the same gameweek, scored against the
same actuals; the enormous week-to-week variation in how many points a good XI
scores is common to both and cancels. That is the same correction Ledger had to
make when it discovered its unpaired test had been "hiding real effects for two
phases".
"""

from __future__ import annotations

import math

import pandas as pd

from gaffer.backtest import _best_xi, _select_squad
from scripts.run_market_strength import evaluate

SEASONS = ["2023-24", "2024-25", "2025-26"]
VARIANTS = ["G0", "G1", "G2"]


def xi_series(ev: pd.DataFrame, horizon: int = 1) -> dict[int, float]:
    """Actual points of the chosen legal XI, per target gameweek."""
    out: dict[int, float] = {}
    sub = ev[ev["horizon"] == horizon]
    for gw, grp in sub.groupby("target_gw"):
        g = grp.dropna(subset=["value", "team_id"]).copy()
        if len(g) < 40:
            continue
        squad = _select_squad(g, "pred")
        if squad is None:
            continue
        xi = _best_xi(g, squad, "pred")
        if len(xi) != 11:
            continue
        out[int(gw)] = float(g.loc[xi, "actual"].sum())
    return out


def paired(a: dict[int, float], b: dict[int, float]) -> tuple[int, float, float, float]:
    shared = sorted(set(a) & set(b))
    diffs = [a[gw] - b[gw] for gw in shared]
    n = len(diffs)
    if n < 2:
        return n, 0.0, 0.0, 0.0
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    stderr = math.sqrt(var / n)
    t = mean / stderr if stderr > 0 else 0.0
    return n, mean, t, 1.96 * stderr


def main() -> int:
    print(__doc__)
    series: dict[tuple[str, str], dict[int, float]] = {}
    for season in SEASONS:
        for variant in VARIANTS:
            ev = evaluate(season, variant)
            series[(season, variant)] = xi_series(ev)
            print(f"  {season} {variant}: {len(series[(season, variant)])} gameweeks")

    print("\n" + "=" * 96)
    print("PAIRED, per gameweek — legal-XI actual points, h=1")
    print("=" * 96)
    print(f"  {'season':<10}{'comparison':<12}{'gw':>5}{'mean diff':>12}"
          f"{'95% CI':>22}{'t':>8}  verdict")
    print("  " + "-" * 92)
    for season in SEASONS:
        for treatment in ("G1", "G2"):
            n, mean, t, half = paired(
                series[(season, treatment)], series[(season, "G0")])
            if t < -1.96:
                verdict = "worse than control"
            elif t > 1.96:
                verdict = "BEATS control"
            else:
                verdict = "not distinguishable"
            print(f"  {season:<10}{treatment + ' vs G0':<12}{n:>5}{mean:>+12.2f}"
                  f"  [{mean - half:>+8.2f},{mean + half:>+8.2f}]{t:>8.2f}  {verdict}")

    # Pooled across all three seasons: the same fixture never appears twice, so
    # the gameweeks are independent draws and pooling is legitimate.
    print("\n  pooled across all three seasons")
    for treatment in ("G1", "G2"):
        diffs: list[float] = []
        for season in SEASONS:
            a, b = series[(season, treatment)], series[(season, "G0")]
            diffs += [a[gw] - b[gw] for gw in sorted(set(a) & set(b))]
        n = len(diffs)
        mean = sum(diffs) / n
        var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
        stderr = math.sqrt(var / n)
        t = mean / stderr
        half = 1.96 * stderr
        mde = half
        if t < -1.96:
            verdict = "worse than control"
        elif t > 1.96:
            verdict = "BEATS control"
        elif abs(mean) < mde:
            verdict = "UNDERPOWERED"
        else:
            verdict = "not distinguishable"
        print(f"  {'ALL':<10}{treatment + ' vs G0':<12}{n:>5}{mean:>+12.2f}"
              f"  [{mean - half:>+8.2f},{mean + half:>+8.2f}]{t:>8.2f}  {verdict}")
        needed = int(n * (mde / abs(mean)) ** 2) if abs(mean) > 1e-9 else 0
        print(f"  {'':<10}{'':<12}      would need ~{needed:,} gameweeks "
              f"(~{needed / 38:.0f} seasons) to resolve this effect size")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
