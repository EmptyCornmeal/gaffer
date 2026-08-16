"""Longitudinal state, carried between runs as NDJSON in the repository.

GitHub Actions runners are ephemeral and `data/*.db` is gitignored, so every
scheduled run starts from an empty database. Most of what Gaffer knows is
re-derivable from the FPL API on the next run — but three tables are not:

* ``decision_snapshots`` — what Gaffer advised *before* a deadline. Once that
  deadline passes the advice can never be reconstructed, because `projections`
  is wiped and rewritten every run and the artifacts always describe *now*.
* ``projection_snapshots`` — the per-player numbers behind that advice.
* ``gw_reviews`` — how a finished gameweek scored against them.

Lose those and `gaffer.review` can never grade a decision, `what_changed` never
has a predecessor, and the plan to fit the blend weight after ~6 gameweeks
cannot begin. **The window closes at each deadline and does not reopen.**

Why NDJSON in git rather than a database artifact: it is text, so it diffs and
reviews like everything else; it is backed up wherever the repo is; the
`refresh.yml` commit step already runs `git add data`, so a run persists itself
with no new plumbing; and it keeps the "no persistent host" property the project
deliberately has. `data/*.db` is gitignored and `ci.yml` fails the build on any
tracked database file, so a binary store was never an option here anyway.

The rules this module keeps:

* **Restore is idempotent.** Re-running it changes nothing. Rows arrive by
  primary key, so a re-import cannot duplicate or reorder history.
* **A damaged store degrades, it does not stop the run.** A line that will not
  parse is skipped and counted. Publishing a gameweek's data matters more than
  the completeness of the archive, and a corrupt archive that halts the pipeline
  turns a bad day into a bad season.
* **Output is deterministic.** Rows are sorted by primary key and keys are
  sorted within each row, so an unchanged fact produces an unchanged line and
  git records only what actually moved.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from gaffer import config
from gaffer.store import db


class Spec:
    """One persisted table: where it lives and what identifies a row."""

    def __init__(self, table: str, filename: str, key: tuple[str, ...],
                 compact_by: tuple[str, ...] | None = None,
                 newest: str | None = None,
                 horizon_col: str | None = None) -> None:
        self.table = table
        self.filename = filename
        self.key = key
        #: When set, only the newest row per this narrower key is persisted.
        self.compact_by = compact_by
        #: The column that decides "newest" during compaction.
        self.newest = newest
        #: When set, rows whose value in this column is beyond the current
        #: gameweek are dropped — they are forecasts, not records. See SPECS.
        self.horizon_col = horizon_col


#: `projection_snapshots` is the one table that would not fit. It writes a row
#: per (player x horizon) on EVERY run — roughly 3,500 rows, about a megabyte of
#: NDJSON, several times a day. Committing that in full would add hundreds of
#: megabytes across a season for information nothing reads.
#:
#: Nothing does read it: `projection.latest_pre_deadline_snapshot` — the only
#: consumer, and the function that defines what a fair evaluation may use —
#: takes the LATEST `as_of` per player for a target event and ignores the rest.
#: So the intermediate re-runs inside one gameweek are already invisible to
#: every reader. Compaction keeps the newest row per
#: (season, target_gw, player_id, is_pre_deadline), retaining both sides of the
#: deadline rather than only the pre-deadline one, because `is_pre_deadline` is
#: a fact about the row that a future reader may legitimately want to split on.
SPECS: tuple[Spec, ...] = (
    Spec("decision_snapshots", "decisions.ndjson",
         ("season", "entry_id", "target_event", "as_of")),
    Spec("gw_reviews", "reviews.ndjson",
         ("season", "entry_id", "event")),
    Spec("projection_snapshots", "projections.ndjson",
         ("season", "target_gw", "player_id", "as_of"),
         compact_by=("season", "target_gw", "player_id", "is_pre_deadline"),
         newest="as_of", horizon_col="target_gw"),
)


def state_dir(base: Path | None = None) -> Path:
    """Where the NDJSON lives. Under `data/` so `git add data` catches it."""
    return (base or config.DATA_DIR) / "state"


def _current_gw(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='current_gw'").fetchone()
    except sqlite3.Error:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError, IndexError):
        return None


def _sort_key(row: Mapping[str, Any], spec: Spec) -> tuple:
    # Mixed types across a column would raise on comparison; str() is stable and
    # only ever affects ordering, never content.
    return tuple(str(row.get(c, "")) for c in spec.key)


def _drop_forecasts(rows: list[dict], spec: Spec, current_gw: int | None) -> list[dict]:
    """Drop rows describing a gameweek that has not been decided yet.

    Each run projects six gameweeks ahead, so a full dump is 587 players x 6
    targets — and every one of those rows carries this run's `as_of`, so the
    whole 1.7 MB file rewrites on every refresh. Several times a day, forever.

    The far horizons are not worth that. Compaction keeps the newest row per
    target, and the run where gameweek 6 becomes *imminent* writes a newer row
    for it than the run six weeks earlier did — so today's h=6 forecast is
    already guaranteed to be overwritten by its own h=1 version before anyone
    reads it. It is churn that cannot survive to be evidence.

    What must survive is the record of a decision as it stood: the imminent
    target, and everything behind it. That is `target_gw <= current_gw`.
    Both readers want exactly that — `latest_pre_deadline_snapshot` asks about a
    specific event, and the blend-weight fit (see `fitting.py`) joins the
    before-the-deadline row for the gameweek being decided to what actually
    happened.

    With no `current_gw` to compare against, keep everything: guessing in the
    lossy direction is how evidence disappears.
    """
    if not spec.horizon_col or current_gw is None:
        return rows
    out = []
    for r in rows:
        v = r.get(spec.horizon_col)
        try:
            if int(v) <= current_gw:
                out.append(r)
        except (TypeError, ValueError):
            out.append(r)          # unparseable: keep, do not silently discard
    return out


def _compact(rows: Iterable[Mapping[str, Any]], spec: Spec) -> list[dict]:
    rows = [dict(r) for r in rows]
    if not spec.compact_by or not spec.newest:
        return rows
    best: dict[tuple, dict] = {}
    for r in rows:
        k = tuple(r.get(c) for c in spec.compact_by)
        cur = best.get(k)
        if cur is None or str(r.get(spec.newest, "")) >= str(cur.get(spec.newest, "")):
            best[k] = r
    return list(best.values())


def dump(conn: sqlite3.Connection, base: Path | None = None) -> dict[str, int]:
    """Write the persisted tables out. Returns rows written per file."""
    out_dir = state_dir(base)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}
    current_gw = _current_gw(conn)
    for spec in SPECS:
        try:
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {spec.table}")]
        except sqlite3.Error:
            # A table that does not exist yet is not an error: a fresh database
            # on a first run has nothing to say.
            written[spec.filename] = 0
            continue
        rows = _drop_forecasts(rows, spec, current_gw)
        rows = _compact(rows, spec)
        rows.sort(key=lambda r: _sort_key(r, spec))
        body = "".join(
            json.dumps(r, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n"
            for r in rows
        )
        path = out_dir / spec.filename
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
        written[spec.filename] = len(rows)
    return written


def restore(conn: sqlite3.Connection, base: Path | None = None) -> dict[str, int]:
    """Load persisted state into an empty (or partial) database.

    Idempotent: rows are keyed, so importing twice is importing once. Damaged
    lines are skipped and reported rather than raised — see the module docstring.
    """
    in_dir = state_dir(base)
    loaded: dict[str, int] = {}
    for spec in SPECS:
        path = in_dir / spec.filename
        if not path.exists():
            loaded[spec.filename] = 0
            continue
        rows: list[dict] = []
        skipped = 0
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            loaded[spec.filename] = 0
            loaded[spec.filename + ":unreadable"] = 1
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(obj, dict) or any(k not in obj for k in spec.key):
                skipped += 1
                continue
            rows.append(obj)
        if rows:
            # Group by column signature: `upsert` takes its column list from the
            # first row, so a file written before a schema column was added must
            # not silently drop or mis-bind the newer rows' columns.
            by_shape: dict[tuple, list[dict]] = {}
            for r in rows:
                by_shape.setdefault(tuple(sorted(r)), []).append(r)
            n = 0
            for group in by_shape.values():
                try:
                    n += db.upsert(conn, spec.table, group, list(spec.key))
                except sqlite3.Error:
                    skipped += len(group)
            conn.commit()
            loaded[spec.filename] = n
        else:
            loaded[spec.filename] = 0
        if skipped:
            loaded[spec.filename + ":skipped"] = skipped
    return loaded
