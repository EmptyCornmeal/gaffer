"""Fetch the historical gameweek data the backtest scores on.

The vaastav/Fantasy-Premier-League repo mirrors every season's merged gameweek
CSV + team table. We keep these out of git (bulky), so this script makes the
model reproducible on a clean clone:

    C:\\Python314\\python.exe scripts/fetch_history.py

Writes data/history/merged_gw_<season>.csv and teams_<season>.csv — exactly the
names gaffer.histdata expects.
"""

from __future__ import annotations

import sys

import httpx

from gaffer import config

RAW = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
#: What each season actually contributes. The split that consumes them is
#: `gaffer.backtest.SEASON_SPLIT`; this list is deliberately wider than the
#: split, because two of these seasons are downloaded only to be a previous
#: season for the next one.
#:
#:   2021-22  PRIOR SOURCE ONLY, and a weak one. It predates FPL's
#:            `expected_goals` / `expected_assists` / `starts` columns, so all
#:            it can give 2022-23 is a minutes and team-strength prior. Its one
#:            remaining consumer is `load_season("2022-23")`, which since G-Q
#:            happens only inside `backtest.xp_leakage_diagnostic`. It was
#:            fetched to make 2022-23 usable as training data; 2022-23 is no
#:            longer training data, so that justification is spent. Kept because
#:            it is 3.6 MB and the diagnostic must stay reproducible — but if
#:            that diagnostic is ever re-scoped, this download goes with it.
#:   2022-23  PRIOR SOURCE ONLY. Excluded from the split by G-Q, on a defect
#:            that is worse than the "goals/xG = 1.419" it was reported as:
#:            `expected_goals` and `expected_assists` are identically ZERO for
#:            GW1-15. The first gameweek carrying any xG at all is 16, and the
#:            covered window holds only 64.2% of the season's minutes. Over
#:            GW16-38 alone the ratio is 0.913 — i.e. ordinary. So the column is
#:            not mis-scaled, it is 40% absent, and rescaling it cannot work
#:            (zero times anything is zero). Combined with a 2021-22 prior that
#:            cannot report xG at all, the model runs there in a regime it never
#:            occupies live: h=1 rank correlation -0.050, legal-XI 26.8 pts/gw
#:            against the naive baseline's 48.2, captain accuracy 8.1%. Still
#:            downloaded, because 2023-24's `base_*` priors are read from it.
#:   2023-24  TRAIN. The earliest season with a full `expected_*` history behind
#:            it as well as in it.
#:   2024-25  SELECT. Freed for this by G-N moving the reporting season forward.
#:   2025-26  TEST. The only season in the archive carrying
#:            `defensive_contribution`, and therefore the only one on which the
#:            projection's DEFCON term can be measured rather than assumed:
#:            ablated, it is worth +3.4 legal-XI pts/gw (49.3 with, 45.9
#:            without) — about 72% of the model's whole margin over the naive
#:            baseline on that season. It also drops the seven `mng_*` manager
#:            columns and gains four defensive ones (46 columns against
#:            2024-25's 49); the adapter reads none of the seven it loses.
SEASONS = ("2021-22", "2022-23", "2023-24", "2024-25", "2025-26")


def _download(client: httpx.Client, url: str, dest) -> None:
    r = client.get(url)
    r.raise_for_status()
    dest.write_bytes(r.content)
    print(f"  {dest.name}  ({len(r.content) // 1024} KiB)")


def main() -> int:
    config.ensure_dirs()
    out = config.HISTORY_DIR
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        for season in SEASONS:
            print(f"{season}:")
            _download(client, f"{RAW}/{season}/gws/merged_gw.csv",
                      out / f"merged_gw_{season}.csv")
            _download(client, f"{RAW}/{season}/teams.csv",
                      out / f"teams_{season}.csv")
    print(f"Done -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
