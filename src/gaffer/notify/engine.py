"""Alert model, dedupe, quiet hours and delivery state.

Nothing here sends. :meth:`Engine.run` resolves what *would* be sent, records the
state, and hands each alert to a sink; the default sink stores it in memory and
throws it away.

The two rules that make an alert engine tolerable rather than infuriating:

**Deduplicate on the fact, not the run.** A dedupe key is built from what
changed, never from a timestamp — so a pipeline that runs three times an hour
does not send three identical injury alerts. A *changed* fact produces a new key
and does alert.

**Quiet hours are real hours in a real timezone.** ``Europe/London``, so the
BST/GMT transition is handled by the zone database rather than by a UTC offset
someone forgot to update in October. Critical alerts still go through; a deadline
you are about to miss is worth waking up for.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from gaffer import season as season_mod

#: Dry-run is the default and must stay the default. Turning it off requires an
#: explicit, deliberate argument at the call site AND a configured provider.
DRY_RUN_DEFAULT = True

#: The user's timezone. Quiet hours are computed here, not in UTC, so the BST
#: to GMT change in late October cannot silently shift the window by an hour.
LOCAL_TZ = ZoneInfo("Europe/London")

QUIET_START = time(22, 30)
QUIET_END = time(7, 30)

# --- severity --------------------------------------------------------------
CRITICAL = "critical"     # ignores quiet hours: you are about to miss something
IMPORTANT = "important"
INFO = "info"
SEVERITY_ORDER = {CRITICAL: 3, IMPORTANT: 2, INFO: 1}
ALL_SEVERITIES = frozenset(SEVERITY_ORDER)

Severity = str

# --- delivery state --------------------------------------------------------
STATE_PENDING = "pending"
STATE_SENT = "sent"
STATE_FAILED = "failed"
STATE_SUPPRESSED = "suppressed"     # quiet hours, or already delivered
STATE_DRY_RUN = "dry_run"
ALL_STATES = frozenset({STATE_PENDING, STATE_SENT, STATE_FAILED,
                        STATE_SUPPRESSED, STATE_DRY_RUN})

MAX_ATTEMPTS = 3


@dataclass
class Alert:
    kind: str
    title: str
    body: str
    severity: Severity = INFO
    event: int | None = None
    #: What this alert is *about*. Two alerts with the same key are the same
    #: fact and must not both be delivered.
    dedupe_parts: tuple[Any, ...] = ()
    deep_link: str = "#/overview"
    created_at: str | None = None

    @property
    def dedupe_key(self) -> str:
        """Stable across runs, distinct when the underlying fact changes.

        Deliberately excludes any timestamp. Including one would make every run
        a "new" alert, which is how notification systems teach you to mute them.
        """
        blob = "|".join(str(p) for p in (self.kind, self.event, *self.dedupe_parts))
        return f"{self.kind}:{hashlib.sha256(blob.encode()).hexdigest()[:12]}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "title": self.title, "body": self.body,
            "severity": self.severity, "event": self.event,
            "deep_link": self.deep_link, "dedupe_key": self.dedupe_key,
            "created_at": self.created_at,
        }


def quiet_hours(now: datetime, *, start: time = QUIET_START,
                end: time = QUIET_END) -> bool:
    """Is ``now`` inside the user's quiet window, in Europe/London?

    Handles the window spanning midnight, and — because the conversion uses the
    zone database rather than a fixed offset — stays correct across the BST/GMT
    transition without any seasonal special-casing.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local = now.astimezone(LOCAL_TZ).time()
    if start <= end:
        return start <= local < end
    return local >= start or local < end


@dataclass
class EngineResult:
    considered: int = 0
    new: int = 0
    duplicates: int = 0
    suppressed: int = 0
    delivered: int = 0
    failed: int = 0
    dry_run: bool = True
    alerts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered, "new": self.new,
            "duplicates": self.duplicates, "suppressed": self.suppressed,
            "delivered": self.delivered, "failed": self.failed,
            "dry_run": self.dry_run, "alerts": self.alerts,
            "errors": self.errors,
        }


class Engine:
    """Resolve, deduplicate and record alerts. Sending is the sink's business."""

    def __init__(
        self, conn: sqlite3.Connection, sink: Any, *,
        dry_run: bool = DRY_RUN_DEFAULT, season: str | None = None,
        quiet: bool = True,
    ) -> None:
        # A test environment can never accidentally go live: the env flag is
        # checked here, not at the call site, so forgetting to pass dry_run=True
        # is safe rather than catastrophic.
        if os.environ.get("GAFFER_NOTIFY_FORCE_DRY_RUN", "").strip() == "1":
            dry_run = True
        self.conn = conn
        self.sink = sink
        self.dry_run = bool(dry_run)
        self.season = season or season_mod.current(conn)
        self.quiet = quiet

    # -- state ------------------------------------------------------------
    def _seen(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM notifications WHERE season=? AND dedupe_key=?",
            (self.season, key)).fetchone()

    def _record(self, alert: Alert, state: str, *, attempts: int = 0,
                error: str | None = None, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO notifications (season, dedupe_key, kind, severity, "
            "event, title, body, deep_link, created_at, state, attempts, "
            "last_attempt_at, last_error, dry_run) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(season, dedupe_key) DO UPDATE SET "
            "state=excluded.state, attempts=excluded.attempts, "
            "last_attempt_at=excluded.last_attempt_at, "
            "last_error=excluded.last_error, dry_run=excluded.dry_run",
            (self.season, alert.dedupe_key, alert.kind, alert.severity,
             alert.event, alert.title, alert.body, alert.deep_link,
             alert.created_at or now.isoformat(timespec="seconds"), state,
             attempts, now.isoformat(timespec="seconds"), error,
             1 if self.dry_run else 0))
        self.conn.commit()

    # -- run ---------------------------------------------------------------
    def run(self, alerts: list[Alert], now: datetime | None = None
            ) -> EngineResult:
        """Process a batch. Never raises on a provider failure."""
        now = (now or datetime.now(UTC))
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        res = EngineResult(considered=len(alerts), dry_run=self.dry_run)
        muted = self.quiet and quiet_hours(now)

        for alert in sorted(
                alerts, key=lambda a: -SEVERITY_ORDER.get(a.severity, 0)):
            prior = self._seen(alert.dedupe_key)
            if prior is not None and prior["state"] in (STATE_SENT, STATE_DRY_RUN):
                res.duplicates += 1
                continue
            if prior is not None and prior["attempts"] >= MAX_ATTEMPTS:
                res.failed += 1
                res.errors.append(
                    f"{alert.dedupe_key}: giving up after {prior['attempts']} "
                    "attempts")
                continue

            res.new += 1
            attempts = int(prior["attempts"]) if prior is not None else 0

            if muted and alert.severity != CRITICAL:
                self._record(alert, STATE_SUPPRESSED, attempts=attempts, now=now)
                res.suppressed += 1
                res.alerts.append({**alert.as_dict(), "state": STATE_SUPPRESSED,
                                   "reason": "quiet hours (Europe/London)"})
                continue

            if self.dry_run:
                self.sink.send(alert)
                self._record(alert, STATE_DRY_RUN, attempts=attempts, now=now)
                res.delivered += 1
                res.alerts.append({**alert.as_dict(), "state": STATE_DRY_RUN})
                continue

            try:
                self.sink.send(alert)
            except Exception as exc:  # noqa: BLE001 - a provider must not break the run
                self._record(alert, STATE_FAILED, attempts=attempts + 1,
                             error=f"{type(exc).__name__}: {exc}", now=now)
                res.failed += 1
                res.errors.append(f"{alert.dedupe_key}: {type(exc).__name__}: {exc}")
                res.alerts.append({**alert.as_dict(), "state": STATE_FAILED})
                continue
            self._record(alert, STATE_SENT, attempts=attempts + 1, now=now)
            res.delivered += 1
            res.alerts.append({**alert.as_dict(), "state": STATE_SENT})

        return res

    # -- reporting ---------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """A publishable, credential-free view of notification state."""
        rows = self.conn.execute(
            "SELECT state, COUNT(*) n FROM notifications WHERE season=? "
            "GROUP BY state", (self.season,)).fetchall()
        recent = self.conn.execute(
            "SELECT kind, severity, title, deep_link, created_at, state, event "
            "FROM notifications WHERE season=? ORDER BY created_at DESC LIMIT 20",
            (self.season,)).fetchall()
        return {
            "dry_run": self.dry_run,
            "sink": type(self.sink).__name__,
            "quiet_hours": {"timezone": "Europe/London",
                            "start": QUIET_START.isoformat(),
                            "end": QUIET_END.isoformat()},
            "by_state": {r["state"]: r["n"] for r in rows},
            "recent": [dict(r) for r in recent],
        }
