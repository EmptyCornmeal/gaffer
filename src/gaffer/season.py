"""Season identity and rollover (T-29).

FPL reuses element ids every summer. Element 328 was one player last season and
is somebody else this one, and most of Gaffer's working tables are keyed on that
id alone. Run the pipeline on the day the API flips over and last season's
players, prices, fixtures and projections silently become this season's — with
no error, because every row still parses.

So this module does two things:

**Identify.** Derive the season the API is describing, compare it with what the
database and the published artifacts believe, and name the situation exactly:
first run, same season, a legitimate new season, missing metadata, an attempted
downgrade, or an API state too ambiguous to act on. There is no seventh case
where two seasons are quietly mixed.

**Roll over.** Archive the current-season working tables under a season-suffixed
name, recreate them empty, and leave every season-keyed historical table exactly
where it is — reviews, snapshots and per-gameweek results are the point of
keeping a database at all. Transactional: a failure halfway through leaves the
prior database usable. Preview first, back up before writing, verify the backup,
refuse a downgrade, and never delete a prior season.

    python -m gaffer.season                 # identity + what a rollover would do
    python -m gaffer.season --rollover      # still a preview; --confirm writes
    python -m gaffer.season --rollover --confirm
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from gaffer import config
from gaffer.store import db

SEASON_RE = re.compile(r"^(\d{4})-(\d{2})$")

#: A season's deadlines all fall inside [start_year-06-01, start_year+1-07-31].
#: An event outside that window means the payload is describing two seasons at
#: once, which is the one thing this module must never resolve by guessing.
SEASON_WINDOW_START_MONTH = 6
SEASON_WINDOW_END_MONTH = 7

# --- what a rollover does to each table -------------------------------------

#: Rebuilt from the API every run and keyed on ids FPL reuses. Archived under a
#: season suffix, then recreated empty.
CURRENT_SEASON_TABLES = ("teams", "players", "fixtures", "projections", "my_squad")

#: Already keyed by season. Untouched — this is the history worth having.
PRESERVED_TABLES = ("player_gw", "projection_snapshots", "decision_snapshots",
                    "gw_reviews", "notifications")

#: Meta keys describing the season that just ended. Cleared, so nothing reads a
#: stale deadline or a squad status from a campaign that is over.
RESET_META_KEYS = (
    "current_gw", "gw_name", "deadline", "last_finished_gw", "projection_event",
    "squad_status", "squad_status_reason", "squad_source_event",
    "squad_retrieved_at", "bank", "team_value", "free_transfers",
    "overall_rank", "notify_last_decision",
)

#: Meta keys that survive: they describe the manager or the rules, not the year.
#: `rule_*` is refreshed from the API on the next run either way.
PRESERVED_META_PREFIXES = ("rule_",)
PRESERVED_META_KEYS = ("entry_name", "manager_name", "total_players")

#: Configuration that may no longer be valid in a new season and cannot be
#: checked from the database alone.
REVALIDATE = (
    "entry_id — a new season does not guarantee the same entry is active",
    "league_ids — mini-league membership and ids change between seasons",
    "free_transfers / bank overrides in gaffer.local.toml — reset by FPL",
    "chips — the 2026/27 two-half chip set is read from bootstrap.chips each "
    "run, so nothing is stored to reset, but confirm the API agrees",
)

STATE_FIRST_RUN = "first_run"
STATE_SAME = "same_season"
STATE_NEW = "new_season"
STATE_MISSING = "missing_metadata"
STATE_DOWNGRADE = "downgrade_refused"
STATE_AMBIGUOUS = "ambiguous_api"
ALL_STATES = frozenset({STATE_FIRST_RUN, STATE_SAME, STATE_NEW, STATE_MISSING,
                        STATE_DOWNGRADE, STATE_AMBIGUOUS})

#: States in which it is safe to ingest and publish without operator action.
SAFE_TO_RUN = frozenset({STATE_FIRST_RUN, STATE_SAME})


class SeasonError(ValueError):
    """Raised for an unparseable or self-contradictory season label."""


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def parse(label: Any) -> int:
    """'2026-27' -> 2026. Raises on anything else.

    Deliberately strict. A label that half-parses is worse than one that fails:
    it produces a season identity nobody intended and every comparison downstream
    silently agrees with it.
    """
    if not isinstance(label, str):
        raise SeasonError(f"season must be a string, got {type(label).__name__}")
    m = SEASON_RE.match(label.strip())
    if not m:
        raise SeasonError(f"malformed season label {label!r} — expected 'YYYY-YY'")
    start = int(m.group(1))
    end2 = int(m.group(2))
    if (start + 1) % 100 != end2:
        raise SeasonError(
            f"season label {label!r} is self-contradictory: {start} is followed "
            f"by {(start + 1) % 100:02d}, not {end2:02d}")
    return start


def label_for(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def next_label(label: str) -> str:
    return label_for(parse(label) + 1)


def is_valid(label: Any) -> bool:
    try:
        parse(label)
    except SeasonError:
        return False
    return True


def slug(label: str) -> str:
    """'2026-27' -> '2026_27', safe as a SQL identifier suffix."""
    return f"{parse(label)}_{(parse(label) + 1) % 100:02d}"


# ---------------------------------------------------------------------------
# Where each party thinks we are
# ---------------------------------------------------------------------------

def _deadline(event: Any) -> datetime | None:
    raw = event.get("deadline_time") if isinstance(event, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt


def derive_from_bootstrap(bootstrap: Any) -> tuple[str | None, str]:
    """The season the API is describing, from event deadlines alone.

    Returns ``(label, reason)``; ``label`` is None when the payload cannot
    identify a season. FPL publishes no season string anywhere in
    ``bootstrap-static``, so this is derived — and derived deterministically,
    never from today's date, which would flip the answer at midnight on some
    arbitrary summer evening.
    """
    if not isinstance(bootstrap, dict):
        return None, "bootstrap is not an object"
    events = bootstrap.get("events")
    if not isinstance(events, list) or not events:
        return None, "bootstrap carries no events"
    stamped = [(e.get("id"), _deadline(e)) for e in events if isinstance(e, dict)]
    stamped = [(i, d) for i, d in stamped if d is not None and isinstance(i, int)]
    if not stamped:
        return None, "no event carries a parseable deadline_time"

    # Gameweek 1 if it is there, otherwise the earliest deadline present. Either
    # way the answer comes from the payload, never from today's date — which
    # would flip the season at midnight on some arbitrary summer evening and
    # make the same payload mean two different things.
    stamped.sort(key=lambda t: (t[0], t[1]))
    first = next((d for i, d in stamped if i == 1), min(d for _, d in stamped))
    start_year = (first.year if first.month >= SEASON_WINDOW_START_MONTH
                  else first.year - 1)

    lo = datetime(start_year, SEASON_WINDOW_START_MONTH, 1, tzinfo=first.tzinfo)
    hi = datetime(start_year + 1, SEASON_WINDOW_END_MONTH, 31, tzinfo=first.tzinfo)
    outside = [i for i, d in stamped if not (lo <= d <= hi)]
    if outside:
        return None, (
            f"events {sorted(outside)[:5]} have deadlines outside "
            f"{lo.date()}..{hi.date()}, so this payload spans more than one "
            f"season and cannot identify a single one")

    last = max(d for _, d in stamped)
    return label_for(start_year), (
        f"first deadline {first.date().isoformat()}, last "
        f"{last.date().isoformat()}, {len(stamped)} event(s)")


def stored(conn: sqlite3.Connection) -> str | None:
    return db.get_meta(conn, "season")


def current(conn: sqlite3.Connection) -> str:
    """The season this database is holding, for stamping new rows.

    Every module that writes a season-keyed row used to default to
    ``config.SEASON`` — a constant edited by hand once a year. After a rollover
    the database is on 2027-28 while the constant still says 2026-27, so the
    first run of the new season would file its snapshots, reviews and alerts
    under the old one. The database is the authority; the constant is the
    fallback for a database that has not been stamped yet.
    """
    return stored(conn) or config.SEASON


def artifact_season(data_dir: Path | None = None) -> str | None:
    path = (data_dir or config.DATA_DIR) / "meta.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("season")
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def database_is_empty(conn: sqlite3.Connection) -> bool:
    """True when no season-bearing table holds a row."""
    for table in (*CURRENT_SEASON_TABLES, *PRESERVED_TABLES):
        try:
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return False
        except sqlite3.OperationalError:
            continue
    return True


@dataclass
class Identity:
    """What each party believes, and what that combination means."""

    api: str | None
    database: str | None
    artifacts: str | None
    state: str
    detail: str
    empty_database: bool = False

    @property
    def safe_to_run(self) -> bool:
        return self.state in SAFE_TO_RUN

    def as_dict(self) -> dict[str, Any]:
        return {"api": self.api, "database": self.database,
                "artifacts": self.artifacts, "state": self.state,
                "detail": self.detail, "safe_to_run": self.safe_to_run}

    def render(self) -> str:
        lines = [
            f"season identity: {self.state}",
            f"  API       : {self.api or '—'}",
            f"  database  : {self.database or '—'}",
            f"  artifacts : {self.artifacts or '—'}",
            f"  {self.detail}",
        ]
        if not self.safe_to_run:
            lines.append("  -> the pipeline must NOT publish in this state.")
        return "\n".join(lines)


def identify(*, api: str | None, database: str | None,
             artifacts: str | None = None, empty_database: bool = False,
             api_detail: str = "") -> Identity:
    """Classify the (api, database, artifacts) triple. Never guesses."""
    def out(state: str, detail: str) -> Identity:
        return Identity(api, database, artifacts, state, detail, empty_database)

    if api is None:
        return out(STATE_AMBIGUOUS,
                   f"the API did not identify a season ({api_detail or 'no reason given'})")
    if not is_valid(api):
        return out(STATE_AMBIGUOUS, f"the API season {api!r} is not a valid label")

    if database is None:
        if empty_database:
            return out(STATE_FIRST_RUN,
                       f"empty database, first run of {api} ({api_detail})")
        return out(STATE_MISSING,
                   "the database holds data but no season stamp, so its rows "
                   "cannot be attributed to a season. Set it deliberately with "
                   "`--adopt <season>` after checking what is in there.")
    if not is_valid(database):
        return out(STATE_MISSING,
                   f"the stored season {database!r} is not a valid label")

    api_y, db_y = parse(api), parse(database)
    if api_y == db_y:
        if artifacts is not None and is_valid(artifacts) and parse(artifacts) != api_y:
            return out(STATE_MISSING,
                       f"database and API agree on {api}, but the published "
                       f"artifacts say {artifacts} — republish before trusting them")
        return out(STATE_SAME, f"same season ({api}); {api_detail}")
    if api_y < db_y:
        return out(STATE_DOWNGRADE,
                   f"the API is describing {api} but the database already holds "
                   f"{database}. Refusing: this is either a cached or mirrored "
                   f"payload, or a restored backup. Nothing is modified.")
    if api_y == db_y + 1:
        return out(STATE_NEW,
                   f"{database} -> {api}. Run `python -m gaffer.season "
                   f"--rollover` to preview, then `--confirm` to apply.")
    return out(STATE_AMBIGUOUS,
               f"the API jumped from {database} to {api}, {api_y - db_y} seasons "
               f"ahead. That is not a rollover; check the payload.")


# ---------------------------------------------------------------------------
# Rollover
# ---------------------------------------------------------------------------

@dataclass
class TableMove:
    table: str
    archive: str
    rows: int


@dataclass
class Plan:
    from_season: str
    to_season: str
    archive: list[TableMove] = field(default_factory=list)
    preserved: list[tuple[str, int]] = field(default_factory=list)
    meta_reset: list[str] = field(default_factory=list)
    meta_kept: list[str] = field(default_factory=list)
    caches: list[str] = field(default_factory=list)
    revalidate: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    already_done: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_season": self.from_season, "to_season": self.to_season,
            "already_done": self.already_done,
            "archive": [{"table": m.table, "archive": m.archive, "rows": m.rows}
                        for m in self.archive],
            "preserved": [{"table": t, "rows": n} for t, n in self.preserved],
            "meta_reset": self.meta_reset, "meta_kept": self.meta_kept,
            "caches": self.caches, "revalidate": self.revalidate,
            "warnings": self.warnings,
        }

    def render(self) -> str:
        if self.already_done:
            return (f"rollover {self.from_season} -> {self.to_season}: already "
                    f"applied. Nothing to do; re-running is safe.")
        lines = [f"rollover preview: {self.from_season} -> {self.to_season}", ""]
        lines.append("  ARCHIVE (renamed, kept, then recreated empty)")
        for m in self.archive:
            lines.append(f"    {m.table:<22} -> {m.archive:<32} {m.rows:>7} rows")
        lines.append("")
        lines.append("  PRESERVE (season-keyed; not touched)")
        for t, n in self.preserved:
            lines.append(f"    {t:<22} {n:>7} rows")
        lines.append("")
        lines.append(f"  META reset : {', '.join(self.meta_reset) or '—'}")
        lines.append(f"  META kept  : {', '.join(self.meta_kept) or '—'}")
        lines.append(f"  CACHES     : {', '.join(self.caches) or '—'}")
        lines.append("")
        lines.append("  REVALIDATE BY HAND")
        for r in self.revalidate:
            lines.append(f"    - {r}")
        if self.warnings:
            lines.append("")
            lines.append("  WARNINGS")
            for w in self.warnings:
                lines.append(f"    ! {w}")
        lines.append("")
        lines.append("  NOTHING IS DELETED. Prior-season rows stay in the archive "
                     "tables above.")
        return "\n".join(lines)


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.OperationalError:
        return -1


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def plan(conn: sqlite3.Connection, to_season: str,
         *, from_season: str | None = None,
         data_dir: Path | None = None) -> Plan:
    """What a rollover would do. Reads only."""
    src = from_season or stored(conn)
    if src is None:
        raise SeasonError("no stored season to roll over from")
    parse(src), parse(to_season)
    suffix = slug(src)
    p = Plan(from_season=src, to_season=to_season)

    # Idempotent in both directions: the stored season already being the target
    # is the common case (someone re-runs the command), and the archive tables
    # already existing covers a re-run before the stamp was written. Without the
    # first test a second call would archive the NEW season into a table named
    # after it and leave the database empty.
    p.already_done = src == to_season or all(
        _table_exists(conn, f"{t}_{suffix}") for t in CURRENT_SEASON_TABLES)
    for table in CURRENT_SEASON_TABLES:
        p.archive.append(TableMove(table, f"{table}_{suffix}", _count(conn, table)))
    for table in PRESERVED_TABLES:
        n = _count(conn, table)
        p.preserved.append((table, n))
        if n > 0:
            rows = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE season = ?", (to_season,)
            ).fetchone()[0]
            if rows:
                p.warnings.append(
                    f"{table} already holds {rows} row(s) stamped {to_season} — "
                    f"a partial run of the new season happened before this "
                    f"rollover. They are kept, not overwritten.")

    have_meta = {r[0] for r in conn.execute("SELECT key FROM meta")}
    p.meta_reset = sorted(k for k in RESET_META_KEYS if k in have_meta)
    p.meta_kept = sorted(
        k for k in have_meta
        if k in PRESERVED_META_KEYS or k.startswith(PRESERVED_META_PREFIXES))
    cache = (data_dir or config.DATA_DIR) / ".cache"
    if cache.is_dir():
        n = sum(1 for _ in cache.glob("*"))
        p.caches.append(f"{cache} ({n} file(s)) — cleared; every entry describes "
                        f"{src}")
    p.revalidate = list(REVALIDATE)
    return p


def _statements(sql: str) -> list[str]:
    """Split a schema file into executable statements.

    `schema.sql` is CREATE TABLE / CREATE INDEX only, with `--` comments and no
    semicolons inside literals, so stripping comments and splitting on `;` is
    exact. It exists because `executescript` commits, and a rollover that
    commits halfway is not a rollover.
    """
    stripped = [line.split("--")[0] for line in sql.splitlines()]
    return [s.strip() for s in "\n".join(stripped).split(";") if s.strip()]


def backup(db_path: Path, dest_dir: Path, *, stamp: str) -> Path:
    """Copy the database aside before touching it. Returns the backup path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{db_path.stem}-{stamp}{db_path.suffix}"
    # `sqlite3`'s own backup API, so a live WAL is captured consistently — a
    # file copy can catch a database mid-checkpoint.
    src = sqlite3.connect(db_path)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def verify_backup(original: Path, copy: Path,
                  tables: tuple[str, ...] = (*CURRENT_SEASON_TABLES,
                                             *PRESERVED_TABLES)) -> dict[str, Any]:
    """Integrity-check the backup and compare row counts table by table.

    A backup nobody read is a hope, not a safety net.
    """
    out: dict[str, Any] = {"path": str(copy), "bytes": copy.stat().st_size}
    conn = sqlite3.connect(copy)
    try:
        out["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        src = sqlite3.connect(original)
        try:
            counts, mismatched = {}, []
            for t in tables:
                a, b = _count(src, t), _count(conn, t)
                counts[t] = b
                if a != b:
                    mismatched.append(f"{t}: source {a}, backup {b}")
            out["row_counts"] = counts
            out["mismatched"] = mismatched
        finally:
            src.close()
    finally:
        conn.close()
    out["ok"] = out["integrity_check"] == "ok" and not out["mismatched"]
    return out


@dataclass
class Result:
    applied: bool
    plan: Plan
    backup: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"applied": self.applied, "plan": self.plan.as_dict(),
                "backup": self.backup, "error": self.error}

    def render(self) -> str:
        head = self.plan.render()
        if self.error:
            return f"{head}\n\nROLLOVER FAILED: {self.error}\nThe database was " \
                   f"rolled back and is unchanged."
        if not self.applied:
            return f"{head}\n\nPREVIEW ONLY — nothing was written. Re-run with " \
                   f"--confirm to apply."
        b = self.backup or {}
        return (f"{head}\n\nAPPLIED. Backup: {b.get('path')} "
                f"({b.get('integrity_check')}, {b.get('bytes', 0) // 1024} KiB).")


def rollover(
    conn: sqlite3.Connection, to_season: str, *, confirm: bool = False,
    db_path: Path | None = None, backup_dir: Path | None = None,
    data_dir: Path | None = None, stamp: str | None = None,
    schema_path: Path | None = None,
) -> Result:
    """Archive the outgoing season and start the new one clean.

    Transactional. DDL in SQLite participates in a transaction, but Python's
    sqlite3 only auto-opens one before DML — so the isolation level is dropped
    and BEGIN/COMMIT/ROLLBACK are issued explicitly. A failure at any point
    leaves the database exactly as it was.

    ``confirm=False`` (the default) previews and writes nothing. This is the
    only destructive-shaped operation in Gaffer and it does not run by accident.
    """
    p = plan(conn, to_season, data_dir=data_dir)
    if p.already_done:
        return Result(applied=False, plan=p)
    if not confirm:
        return Result(applied=False, plan=p)

    src_path = Path(db_path or config.DB_PATH)
    stamp = stamp or "rollover"
    b = None
    if src_path.exists():
        dest = backup(src_path, Path(backup_dir or src_path.parent / "backups"),
                      stamp=f"{slug(p.from_season)}-{stamp}")
        b = verify_backup(src_path, dest)
        if not b["ok"]:
            return Result(applied=False, plan=p, backup=b,
                          error=f"backup verification failed: "
                                f"{b['integrity_check']}, {b['mismatched']}")

    prior_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        # FK enforcement and reference-rewriting are both no-ops inside a
        # transaction, so they are set first. `legacy_alter_table` stops SQLite
        # repointing player_gw/projections/my_squad at the ARCHIVE table when
        # `players` is renamed — which would leave the recreated `players`
        # referenced by nothing.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute("BEGIN IMMEDIATE")

        for move in p.archive:
            if not _table_exists(conn, move.table):
                continue
            # CREATE ... AS SELECT, not ALTER ... RENAME. A rename carries the
            # table's constraints with it, so the archived `projections` and
            # `my_squad` would keep a foreign key into the *live* `players` —
            # and every archived row would violate it the moment `players` is
            # recreated empty. Those references are meaningless across a season
            # boundary anyway: FPL reuses element ids, so last season's 328 does
            # not refer to this season's 328. The copy keeps the data and drops
            # the constraints, which is exactly what cold storage should be.
            conn.execute(
                f"CREATE TABLE {move.archive} AS SELECT * FROM {move.table}")
            conn.execute(f"DROP TABLE {move.table}")

        # NOT executescript: Python's sqlite3 issues an implicit COMMIT before
        # running one, which would end the transaction that makes this safe.
        for stmt in _statements(
                (schema_path or config.SCHEMA_PATH).read_text(encoding="utf-8")):
            conn.execute(stmt)

        for key in p.meta_reset:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('season', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (to_season,))
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('season_rolled_from', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (p.from_season,))
        conn.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - already rolled back
            pass
        return Result(applied=False, plan=p, backup=b, error=f"{type(exc).__name__}: {exc}")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = prior_isolation

    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if bad:  # pragma: no cover - defensive
        return Result(applied=False, plan=p, backup=b,
                      error=f"foreign key check failed after rollover: {bad[:5]}")

    cache = (data_dir or config.DATA_DIR) / ".cache"
    if cache.is_dir():
        shutil.rmtree(cache, ignore_errors=True)
    return Result(applied=True, plan=p, backup=b)


def adopt(conn: sqlite3.Connection, label: str) -> None:
    """Stamp a season onto a database that has none. Deliberate, never implicit."""
    parse(label)
    db.set_meta(conn, "season", label)


def archived_seasons(conn: sqlite3.Connection) -> list[str]:
    """Seasons whose working tables are in cold storage, newest first."""
    found: set[str] = set()
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        m = re.match(r"^players_(\d{4})_(\d{2})$", name)
        if m:
            found.add(f"{m.group(1)}-{m.group(2)}")
    return sorted(found, key=parse, reverse=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _bootstrap_from_cache(data_dir: Path | None = None) -> Any:
    path = (data_dir or config.DATA_DIR) / ".cache" / "bootstrap-static.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Season identity and rollover")
    ap.add_argument("--rollover", action="store_true",
                    help="preview a rollover to the API's season")
    ap.add_argument("--confirm", action="store_true",
                    help="actually apply the rollover previewed by --rollover")
    ap.add_argument("--adopt", metavar="SEASON", default=None,
                    help="stamp a season onto a database that has none")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    conn = db.connect()
    try:
        db.init_schema(conn)
        if args.adopt:
            adopt(conn, args.adopt)
            print(f"database season stamped as {args.adopt}")
            return 0

        boot = _bootstrap_from_cache()
        api, why = derive_from_bootstrap(boot) if boot is not None else (
            None, "no cached bootstrap-static.json; run the pipeline first")
        ident = identify(api=api, database=stored(conn),
                         artifacts=artifact_season(),
                         empty_database=database_is_empty(conn),
                         api_detail=why)
        archived = archived_seasons(conn)

        if not args.rollover:
            if args.json:
                print(json.dumps({"identity": ident.as_dict(),
                                  "archived_seasons": archived}, indent=2))
            else:
                print(ident.render())
                print(f"  archived  : {', '.join(archived) or 'none'}")
            return 0 if ident.safe_to_run or ident.state == STATE_NEW else 1

        if ident.state != STATE_NEW:
            print(f"not rolling over: {ident.state}\n{ident.render()}",
                  file=sys.stderr)
            return 1
        res = rollover(conn, ident.api, confirm=args.confirm,
                       stamp=datetime.now().strftime("%Y%m%dT%H%M%S")
                       if args.confirm else "preview")
        print(json.dumps(res.as_dict(), indent=2) if args.json else res.render())
        return 0 if res.error is None else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
