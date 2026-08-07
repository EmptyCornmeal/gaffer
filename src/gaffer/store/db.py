"""Thin SQLite helpers. Raw SQL by design — no ORM."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from gaffer import config


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _schema_columns(schema_sql: str, table: str) -> list[tuple[str, str]]:
    """Column name and DDL for one ``CREATE TABLE`` block in the schema file."""
    block = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", schema_sql, re.S)
    if not block:
        return []
    out: list[tuple[str, str]] = []
    for raw in block.group(1).splitlines():
        line = raw.split("--")[0].strip().rstrip(",")
        if not line:
            continue
        m = re.match(r"^(\w+)\s+(.+)$", line)
        if not m or m.group(1).upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK"):
            continue
        out.append((m.group(1), m.group(2).strip()))
    return out


#: Tables whose new columns can be added in place. Every column these batches
#: introduced is nullable or defaulted, so an ALTER is lossless.
ADDITIVE_TABLES = ("players", "projections", "projection_snapshots", "my_squad",
                   "fixtures", "teams", "player_gw")


def _add_missing_columns(
    conn: sqlite3.Connection, schema_sql: str, applied: list[str],
) -> None:
    """Add columns the schema declares but an existing database lacks.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op against an existing table, so every
    column added after a user first ran Gaffer is simply absent — and the next
    ingest dies with ``table players has no column named cost_change_start``.
    CI never sees it (fresh checkout, fresh database); the person who has been
    running it since July sees it on the first run after upgrading.

    Only ever adds. A column that exists is left exactly as it is, and no data is
    moved, rewritten or dropped.
    """
    for table in ADDITIVE_TABLES:
        if not _table_exists(conn, table):
            continue
        have = _columns(conn, table)
        for name, ddl in _schema_columns(schema_sql, table):
            if name in have:
                continue
            # SQLite cannot ALTER-ADD a column with a non-constant default; every
            # column we add is a literal default or nullable, so this is safe.
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            applied.append(f"{table} += {name}")
    conn.commit()


def migrate(conn: sqlite3.Connection, schema_path: Path | None = None) -> list[str]:
    """Bring an existing database up to the current schema. Returns what ran.

    ``CREATE TABLE IF NOT EXISTS`` silently leaves an older table shape in place,
    so an existing local DB would keep the season-less ``player_gw`` forever.
    Each migration is idempotent and safe to run on a fresh database.
    """
    applied: list[str] = []

    # player_gw gained `season` + a fixture-level primary key. The old table was
    # never written to by any code path, so there is nothing to preserve — but
    # check rather than assume, and refuse to silently discard real rows.
    if _table_exists(conn, "player_gw") and "season" not in _columns(conn, "player_gw"):
        n = conn.execute("SELECT COUNT(*) AS n FROM player_gw").fetchone()["n"]
        if n:  # pragma: no cover - defensive; no code path ever populated it
            conn.execute("ALTER TABLE player_gw RENAME TO player_gw_legacy")
            applied.append(f"player_gw -> player_gw_legacy ({n} rows preserved)")
        else:
            conn.execute("DROP TABLE player_gw")
            applied.append("player_gw rebuilt (was empty, pre-season schema)")
        conn.commit()

    # my_squad gained price provenance columns (T-11). ALTER is safe and keeps
    # any stored squad; the new columns are simply NULL until the next ingest.
    if _table_exists(conn, "my_squad"):
        cols = _columns(conn, "my_squad")
        for name, ddl in (("price_source", "TEXT"),
                          ("price_exact", "INTEGER DEFAULT 0")):
            if name not in cols:
                conn.execute(f"ALTER TABLE my_squad ADD COLUMN {name} {ddl}")
                applied.append(f"my_squad += {name}")
        conn.commit()

    # Additive column migrations, driven by the schema file itself so a column
    # added there can never be forgotten here.
    sql = (schema_path or config.SCHEMA_PATH).read_text(encoding="utf-8")
    _add_missing_columns(conn, sql, applied)

    # T-29: a database written before season identity existed carries no stamp,
    # and `season.identify` correctly refuses to run against one. Gaffer has only
    # ever ingested a single season, and `config.SEASON` names it, so adopting it
    # here is an inference from the code that wrote the rows — not a guess about
    # what they contain. Recorded under its own key so the adoption is visible
    # rather than indistinguishable from a season set deliberately.
    if _table_exists(conn, "meta") and get_meta(conn, "season") is None:
        set_meta(conn, "season", config.SEASON)
        set_meta(conn, "season_adopted_by_migration", config.SEASON)
        applied.append(f"meta.season adopted as {config.SEASON}")

    return applied


def init_schema(conn: sqlite3.Connection, schema_path: Path | None = None) -> None:
    path = schema_path or config.SCHEMA_PATH
    sql = path.read_text(encoding="utf-8")
    # Create anything missing FIRST, then reconcile columns on tables that
    # already existed: a brand-new database has nothing to migrate, and an old
    # one needs its tables present before they can be altered.
    conn.executescript(sql)
    conn.commit()
    migrate(conn, path)


def upsert(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[Mapping[str, Any]],
    key_cols: Sequence[str],
) -> int:
    """Bulk INSERT ... ON CONFLICT(key_cols) DO UPDATE. Returns row count.

    All rows must share the same columns (taken from the first row).
    """
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    update_cols = [c for c in cols if c not in key_cols]
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
    conflict = ", ".join(key_cols)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    if update_cols:
        sql += f" ON CONFLICT({conflict}) DO UPDATE SET {set_clause}"
    else:
        sql += f" ON CONFLICT({conflict}) DO NOTHING"
    # Atomic: executemany aborts mid-batch on a constraint violation, and
    # without an explicit rollback the rows written before the failure stay
    # pending on the connection and become visible to the next read.
    try:
        conn.executemany(sql, rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
