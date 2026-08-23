"""Export Gaffer's player history to the Football Exchange, once, immutably.

Gaffer holds five seasons of per-player per-fixture rows. Ledger recorded its
availability ladder rungs A2-A6 as PENDING for want of exactly this, and its
market map records player cards as having "no event data". Both statements are
false while this file sits on the same disk.

Three things this does that a `cp` would not.

**It applies the real leakage denylist**, imported from `gaffer.leakage` rather
than copied, so the classification cannot drift away from the one Gaffer
enforces on itself. Every column is stamped `post_match` true or false in the
manifest, and the consumer is told in writing that a post-match value is legal
only as a target or as an input to a fixture starting strictly later.

**It drops `xP` entirely.** Not because it is post-match -- it is a forecast,
not an outcome -- but because the upstream archive cannot certify it as the
pre-deadline number managers saw, and its own dictionary warns it may contain
post-match information. Gaffer ruled it inadmissible on provenance. Ledger has
no leakage module to catch it, so it is filtered here rather than trusted to be
ignored there.

**It distinguishes a missing column from a measured zero.** `starts` and
`expected_goals` do not exist before 2022-23 and `defensive_contribution` does
not exist before 2025-26. FPL back-fills absent statistics with 0 and still
returns the key, so a season that could not report starts looks identical to a
player who started nothing. Absent columns are written EMPTY, never 0, and the
manifest records which season could report what.

Writes nothing inside either repository. Refuses to overwrite an existing
version.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from gaffer.leakage import POST_MATCH_FIELDS

EXCHANGE = Path.home() / "Projects" / "Football Exchange"
HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"

DATASET = "player_history"
SCHEMA_VERSION = 1

#: Excluded on provenance, not on timing. See the module docstring.
INADMISSIBLE = {"xP"}

#: Identity and pre-deadline context.
PRE_DEADLINE = [
    "season", "element", "name", "position", "team",
    "GW", "fixture", "kickoff_time", "opponent_team", "was_home", "value",
]

#: Outcomes. Knowable only after this row's own kickoff.
POST_MATCH = [
    "minutes", "starts", "total_points",
    "goals_scored", "assists",
    "expected_goals", "expected_assists", "expected_goal_involvements",
    "expected_goals_conceded",
    "yellow_cards", "red_cards",
    "bps", "bonus",
    "defensive_contribution",
]

COLUMNS = PRE_DEADLINE + POST_MATCH


def _commit(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _season_files() -> dict[str, Path]:
    out = {}
    for path in sorted(HISTORY.glob("merged_gw_*.csv")):
        out[path.stem.replace("merged_gw_", "")] = path
    return out


def export(version: str, *, force: bool = False) -> int:
    target = EXCHANGE / "gaffer" / DATASET / version
    if target.exists() and not force:
        print(f"refusing to overwrite an existing export: {target}", file=sys.stderr)
        print("an export is immutable; write a new version instead", file=sys.stderr)
        return 2

    files = _season_files()
    if not files:
        print(f"no archive files under {HISTORY}", file=sys.stderr)
        return 1

    target.mkdir(parents=True, exist_ok=True)
    data_path = target / "data.csv"

    # Which season could report which statistic. A column absent from a
    # season's file is written empty, so a consumer can tell "not measured"
    # from "measured as none".
    availability: dict[str, list[str]] = {}
    per_season_rows: dict[str, int] = {}
    dropped_inadmissible: set[str] = set()
    total = 0

    with data_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()

        for season, path in files.items():
            with path.open(newline="", encoding="utf-8") as src:
                reader = csv.DictReader(src)
                present = set(reader.fieldnames or [])
                dropped_inadmissible |= present & INADMISSIBLE
                availability[season] = sorted(
                    c for c in COLUMNS if c in present or c == "season"
                )
                count = 0
                for row in reader:
                    out = {"season": season}
                    for col in COLUMNS:
                        if col == "season":
                            continue
                        # Absent column -> empty, NEVER zero.
                        out[col] = row.get(col, "") if col in present else ""
                    writer.writerow(out)
                    count += 1
                per_season_rows[season] = count
                total += count

    # The as_of of the whole export is the latest kickoff it describes.
    latest = ""
    with data_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            k = row.get("kickoff_time") or ""
            if k > latest:
                latest = k

    manifest = {
        "producer": "gaffer",
        "producer_commit": _commit(Path(__file__).resolve().parents[1]),
        "dataset": DATASET,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": latest,
        "coverage": {
            "competition": "Premier League only",
            "seasons": sorted(files),
            "rows_per_season": per_season_rows,
            "grain": "one row per player per fixture",
            "column_availability_by_season": availability,
            "structural_zero_warning": (
                "`starts` and the expected_* columns do not exist before "
                "2022-23; `defensive_contribution` does not exist before "
                "2025-26. Those cells are EMPTY here, never 0. FPL back-fills "
                "absent statistics with 0 and still returns the key, so a "
                "season that could not report a statistic is otherwise "
                "indistinguishable from a player who recorded none of it."
            ),
        },
        "row_count": total,
        "model_version": None,
        "fields": {
            col: {
                "source": "vaastav FPL archive via gaffer/data/history",
                "post_match": col in POST_MATCH_FIELDS,
            }
            for col in COLUMNS
        },
        "excluded_fields": {
            "xP": (
                "INADMISSIBLE ON PROVENANCE. The upstream archive cannot certify "
                "it as the pre-deadline forecast managers saw, and its own data "
                "dictionary warns it may contain post-match information. Not "
                "exported at any version."
            ),
        },
        "dropped_inadmissible_columns_found": sorted(dropped_inadmissible),
        "forbidden_use": [
            "A field with post_match=true is knowable only AFTER that row's own "
            "kickoff_time. It is legal as a prediction TARGET, and legal as an "
            "input to a DIFFERENT fixture whose kickoff is strictly later. It is "
            "never a feature for its own fixture.",
            "`element` is an FPL element id and is REUSED between seasons. The "
            "only stable key is (season, element).",
            "An EMPTY cell means the season could not report that statistic. It "
            "does not mean zero. Do not fill it.",
            "Consumers enforce their own cutoff. Ledger's boundary is per-fixture "
            "kickoff; Gaffer's is the FPL gameweek deadline. This export asserts "
            "neither on the consumer's behalf.",
        ],
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    print(f"wrote {total:,} rows to {data_path}")
    for season in sorted(per_season_rows):
        missing = [c for c in COLUMNS if c not in availability[season]]
        note = f"   missing: {', '.join(missing)}" if missing else ""
        print(f"  {season}  {per_season_rows[season]:>6,} rows{note}")
    print(f"as_of {latest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", default=datetime.now(UTC).strftime("%Y-%m-%d"))
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing version (breaks immutability)")
    args = ap.parse_args(argv)
    return export(args.version, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
