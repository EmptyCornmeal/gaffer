"""Gameweek resolution — two distinct concepts that were previously conflated.

The pipeline used one number for both "the event I am projecting" and "the event
whose picks I can read". Those are never the same event during normal play: FPL
keeps an entry's picks private until that event's deadline passes, so asking for
the upcoming event's picks is guaranteed to 404 before every deadline.

Pure functions over the bootstrap ``events`` list — no HTTP, no clock reads
except the injectable ``now``, so every boundary is testable.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

# --- squad status vocabulary (machine-readable, stored in meta) -------------
# Downstream must be able to tell "no squad exists yet" from "we failed to fetch
# one" from "we have one". A single free-text 'unavailable' string could not.
STATUS_LOADED = "loaded"
STATUS_NO_PUBLIC_SQUAD_YET = "no_public_squad_yet"
STATUS_NOT_FOUND = "not_found"
STATUS_FETCH_FAILED = "fetch_failed"
STATUS_MALFORMED = "malformed"
STATUS_NO_ENTRY_ID = "no_entry_id"
STATUS_STALE = "stale"

#: Statuses where a stored squad is present and usable as the holdings baseline.
STATUSES_WITH_SQUAD = frozenset({STATUS_LOADED, STATUS_STALE})
#: Statuses where no squad is stored. Consumers must NOT read these as
#: "the user owns nothing" — they mean "we do not know what the user owns".
STATUSES_WITHOUT_SQUAD = frozenset({
    STATUS_NO_PUBLIC_SQUAD_YET, STATUS_NOT_FOUND,
    STATUS_FETCH_FAILED, STATUS_MALFORMED, STATUS_NO_ENTRY_ID,
})

ALL_STATUSES = STATUSES_WITH_SQUAD | STATUSES_WITHOUT_SQUAD


def parse_deadline(raw: Any) -> datetime | None:
    """Parse an FPL ``deadline_time``. Returns None when absent/unparseable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _sorted_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((e for e in events if e.get("id") is not None),
                  key=lambda e: int(e["id"]))


def projection_event(events: Iterable[dict[str, Any]], now: datetime | None = None) -> int:
    """The event to project and make decisions for.

    The first event whose deadline has NOT yet passed — i.e. the next one you can
    still act on. Falls back to the API's own ``is_next`` / first-unfinished
    flags, then to the last event. Before the season starts this is GW1.
    """
    evs = _sorted_events(events)
    if not evs:
        return 1
    now = now or datetime.now(UTC)
    for ev in evs:
        dl = parse_deadline(ev.get("deadline_time"))
        if dl is not None and dl > now:
            return int(ev["id"])
    # Deadlines missing or all passed — fall back to the API's flags.
    for ev in evs:
        if ev.get("is_next"):
            return int(ev["id"])
    for ev in evs:
        if ev.get("is_current"):
            return int(ev["id"])
    for ev in evs:
        if not ev.get("finished"):
            return int(ev["id"])
    return int(evs[-1]["id"])


def live_event(
    events: Iterable[dict[str, Any]], now: datetime | None = None
) -> int | None:
    """The event whose football is actually being played - the one to track live.

    The highest event whose deadline has passed and which the API has not yet
    flagged ``finished``. This is deliberately NOT :func:`projection_event`: the
    instant GW1's deadline passes, decisions move to GW2 while GW1's ten matches
    are still to be played. Tracking the projection event would blank the live
    view for the entire gameweek.

    Returns ``None`` before the season's first deadline, when no event has been
    played and a live view would be a fiction.
    """
    evs = _sorted_events(events)
    if not evs:
        return None
    now = now or datetime.now(UTC)
    passed = [
        ev for ev in evs
        if (dl := parse_deadline(ev.get("deadline_time"))) is not None and dl <= now
    ]
    if not passed:
        # No usable deadlines: fall back to the API's own notion of in-flight.
        for ev in evs:
            if ev.get("is_current"):
                return int(ev["id"])
        return None
    for ev in reversed(passed):
        if not ev.get("finished"):
            return int(ev["id"])
    # Everything played out: the most recent finished event is still the one
    # whose scores a reader means by "live".
    return int(passed[-1]["id"])


def readable_squad_event(
    events: Iterable[dict[str, Any]], now: datetime | None = None
) -> int | None:
    """The latest event whose entry picks are publicly readable.

    FPL reveals an entry's picks once that event's deadline has passed, so this
    is the highest event id with ``deadline_time <= now``.

    Returns ``None`` before the first deadline of the season — a public entry
    genuinely has no readable squad then, and that must be represented as
    "unknown", never as "owns nothing".
    """
    evs = _sorted_events(events)
    if not evs:
        return None
    now = now or datetime.now(UTC)
    readable = [
        int(ev["id"]) for ev in evs
        if (dl := parse_deadline(ev.get("deadline_time"))) is not None and dl <= now
    ]
    if readable:
        return max(readable)
    # No usable deadlines: fall back to the API's own notion of a played event.
    finished = [int(ev["id"]) for ev in evs if ev.get("finished")]
    return max(finished) if finished else None


def last_finished_event(events: Iterable[dict[str, Any]]) -> int | None:
    """Highest event flagged ``finished`` by the API (all matches played)."""
    finished = [int(e["id"]) for e in _sorted_events(events) if e.get("finished")]
    return max(finished) if finished else None


def describe(events: Iterable[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    """Every resolution at once, for logging and metadata."""
    now = now or datetime.now(UTC)
    return {
        "projection_event": projection_event(events, now),
        "squad_source_event": readable_squad_event(events, now),
        "live_event": live_event(events, now),
        "last_finished_event": last_finished_event(events),
        "resolved_at": now.isoformat(timespec="seconds"),
    }
