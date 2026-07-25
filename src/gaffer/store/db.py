"""Thin SQLite helpers. Raw SQL by design — no ORM."""

from __future__ import annotations

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


def init_schema(conn: sqlite3.Connection, schema_path: Path | None = None) -> None:
    sql = (schema_path or config.SCHEMA_PATH).read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()


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
    conn.executemany(sql, rows)
    conn.commit()
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


def rows_to_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]
