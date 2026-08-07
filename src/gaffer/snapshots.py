"""Immutable pre-deadline decision snapshots (T-21).

Without this, "what did Gaffer recommend?" has no answer. `projections` is wiped
and rewritten every run, and the exported artifacts always describe *now* — so
after a gameweek there is no record of what was advised before the deadline, and
any review built on top would be scoring the model against its own hindsight.

The rule this module enforces is narrow and absolute:

    **Once a target event's deadline has passed, nothing about that event's
    pre-deadline record may change — ever.**

Not "should not". A later run cannot rewrite it, cannot append to it, and cannot
delete it. That is what makes T-23's decision-quality analysis honest: the
baseline it compares against is the advice as it actually stood, not a
retrospectively improved version of it.

Snapshots are season-aware (FPL reuses element ids every year), content-hashed so
repeated refreshes inside one gameweek do not pile up identical rows, and stored
as versioned JSON so the shape can evolve without invalidating history.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gaffer import season as season_mod

SNAPSHOT_SCHEMA_VERSION = 1

#: Raised paths refuse rather than corrupt. A caller that ignores these has
#: written a bug, not a warning.
class SnapshotError(RuntimeError):
    """A write that would violate the immutability rule."""


class DeadlinePassedError(SnapshotError):
    """A pre-deadline snapshot was attempted after the deadline."""


@dataclass
class Snapshot:
    season: str
    entry_id: int
    target_event: int
    as_of: str            # ISO 8601 UTC, the run timestamp
    deadline: str         # ISO 8601 UTC, the target event's deadline
    is_pre_deadline: bool
    payload: dict[str, Any]
    content_hash: str
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "season": self.season,
            "entry_id": self.entry_id,
            "target_event": self.target_event,
            "as_of": self.as_of,
            "deadline": self.deadline,
            "is_pre_deadline": self.is_pre_deadline,
            "content_hash": self.content_hash,
            **self.payload,
        }


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_time(raw: Any) -> datetime | None:
    """ISO 8601 with an explicit offset. A naive stamp is rejected, not assumed."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return None if dt.tzinfo is None else dt.astimezone(UTC)


#: Keys whose value changes every run without the decision changing. Stripped at
#: EVERY level, not just the top: the freshness block carries its own nested
#: ``generated_at``, and stripping only the outer one made every single refresh
#: look like a brand-new recommendation.
VOLATILE_KEYS = frozenset({
    "as_of", "generated_at", "data_age_seconds", "content_hash",
    "squad_retrieved_at", "retrieved_at",
})


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in value.items()
                if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def content_hash(payload: dict[str, Any]) -> str:
    """Stable hash of the decision itself, ignoring when it was computed.

    Timestamps move every run; the *decision* usually does not. Hashing the
    decision means three refreshes an hour apart that all say "roll" store one
    row, not three.
    """
    blob = json.dumps(_strip_volatile(payload), sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def record(
    conn: sqlite3.Connection, *, entry_id: int, target_event: int,
    deadline: str, payload: dict[str, Any], now: datetime,
    season: str | None = None,
) -> tuple[Snapshot | None, str]:
    """Persist a decision snapshot. Returns ``(snapshot_or_None, outcome)``.

    Outcomes:
      ``written``     a new pre-deadline snapshot was stored
      ``unchanged``   an identical decision is already stored for this event
      ``locked``      the deadline has passed; the record is now immutable
      ``no_deadline`` the target event has no parseable deadline

    A post-deadline call is not an error — the pipeline runs on a schedule and
    will cross a deadline mid-gameweek every week. It is simply refused.
    """
    season = season or season_mod.current(conn)
    dl = parse_time(deadline)
    if dl is None:
        return None, "no_deadline"

    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    if now >= dl:
        return None, "locked"

    h = content_hash(payload)
    existing = conn.execute(
        "SELECT content_hash FROM decision_snapshots "
        "WHERE season=? AND entry_id=? AND target_event=? "
        "ORDER BY as_of DESC LIMIT 1",
        (season, entry_id, target_event),
    ).fetchone()
    if existing is not None and existing["content_hash"] == h:
        return None, "unchanged"

    snap = Snapshot(
        season=season, entry_id=entry_id, target_event=target_event,
        as_of=_iso(now), deadline=_iso(dl), is_pre_deadline=True,
        payload=payload, content_hash=h,
    )
    conn.execute(
        "INSERT OR REPLACE INTO decision_snapshots "
        "(season, entry_id, target_event, as_of, deadline, is_pre_deadline, "
        " schema_version, content_hash, payload) "
        "VALUES (?,?,?,?,?,1,?,?,?)",
        (season, entry_id, target_event, snap.as_of, snap.deadline,
         SNAPSHOT_SCHEMA_VERSION, h, json.dumps(payload, default=str)),
    )
    conn.commit()
    return snap, "written"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _row_to_snapshot(r: sqlite3.Row) -> Snapshot:
    return Snapshot(
        season=r["season"], entry_id=r["entry_id"],
        target_event=r["target_event"], as_of=r["as_of"], deadline=r["deadline"],
        is_pre_deadline=bool(r["is_pre_deadline"]),
        payload=json.loads(r["payload"]), content_hash=r["content_hash"],
        schema_version=r["schema_version"],
    )


def final_pre_deadline(
    conn: sqlite3.Connection, entry_id: int, target_event: int,
    season: str | None = None,
) -> Snapshot | None:
    """The last thing Gaffer said before the deadline — the reviewable record.

    This, and only this, is what T-23 may compare against. Earlier snapshots in
    the same week are history, not advice.
    """
    season = season or season_mod.current(conn)
    r = conn.execute(
        "SELECT * FROM decision_snapshots WHERE season=? AND entry_id=? "
        "AND target_event=? AND is_pre_deadline=1 ORDER BY as_of DESC LIMIT 1",
        (season, entry_id, target_event),
    ).fetchone()
    return _row_to_snapshot(r) if r else None


def history(
    conn: sqlite3.Connection, entry_id: int, season: str | None = None,
    limit: int = 60,
) -> list[Snapshot]:
    """Every event's final pre-deadline snapshot, most recent event first."""
    season = season or season_mod.current(conn)
    rows = conn.execute(
        "SELECT * FROM decision_snapshots d WHERE season=? AND entry_id=? "
        "AND is_pre_deadline=1 AND as_of = ("
        "  SELECT MAX(as_of) FROM decision_snapshots "
        "  WHERE season=d.season AND entry_id=d.entry_id "
        "  AND target_event=d.target_event AND is_pre_deadline=1) "
        "ORDER BY target_event DESC LIMIT ?",
        (season, entry_id, limit),
    ).fetchall()
    return [_row_to_snapshot(r) for r in rows]


def is_locked(
    conn: sqlite3.Connection, target_event: int, now: datetime,
    entry_id: int | None = None, season: str | None = None,
) -> bool:
    """True once this event's deadline has passed, per the stored deadline."""
    season = season or season_mod.current(conn)
    q = ("SELECT deadline FROM decision_snapshots WHERE season=? AND target_event=?"
         + (" AND entry_id=?" if entry_id is not None else "")
         + " ORDER BY as_of DESC LIMIT 1")
    args = (season, target_event) + ((entry_id,) if entry_id is not None else ())
    r = conn.execute(q, args).fetchone()
    if r is None:
        return False
    dl = parse_time(r["deadline"])
    if dl is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC) >= dl


def assert_immutable(
    conn: sqlite3.Connection, entry_id: int, target_event: int,
    now: datetime, season: str | None = None,
) -> None:
    """Raise if a write to a locked event is being attempted.

    Callers that persist derived records (reviews) use this to prove they are not
    editing the pre-deadline history they are supposed to be scoring.
    """
    if is_locked(conn, target_event, now, entry_id, season):
        raise DeadlinePassedError(
            f"GW{target_event}'s deadline has passed; its pre-deadline snapshot "
            "is immutable and must not be rewritten"
        )
