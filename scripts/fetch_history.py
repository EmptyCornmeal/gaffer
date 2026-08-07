"""Fetch the historical gameweek data the ML model trains/backtests on.

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
#: 2021-22 exists only to supply 2022-23's priors. It predates FPL's
#: `expected_goals` / `expected_assists` / `starts` columns, so it contributes a
#: minutes and team-strength prior and nothing else — which is exactly why
#: 2022-23 was previously unusable. See docs/MODEL-EVALUATION.md.
SEASONS = ("2021-22", "2022-23", "2023-24", "2024-25")


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
