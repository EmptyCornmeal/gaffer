"""What is worth interrupting someone for.

Every rule below is backed by data Gaffer already has and can defend. The one
deliberately absent is a price-change alert: `_price_pred` in the export layer is
a documented *estimate* built from net transfers against a guessed threshold, and
FPL's real thresholds are secret. Sending "Haaland is about to rise" on that basis
would be presenting a heuristic as a forecast, so there is no such rule and no
placeholder for one — a validated price source is a prerequisite, not a TODO.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from gaffer.notify.engine import CRITICAL, IMPORTANT, INFO, Alert

# --- alert kinds -----------------------------------------------------------
KIND_DEADLINE = "deadline"
KIND_AVAILABILITY = "availability"
KIND_RECOMMENDATION = "recommendation_changed"
KIND_CAPTAIN = "captain_changed"
KIND_SQUAD_STATE = "squad_state_failure"
KIND_STALE = "stale_data"
KIND_LEAGUE_SWING = "league_swing"
KIND_CHIP_WINDOW = "chip_window"
ALL_KINDS = frozenset({
    KIND_DEADLINE, KIND_AVAILABILITY, KIND_RECOMMENDATION, KIND_CAPTAIN,
    KIND_SQUAD_STATE, KIND_STALE, KIND_LEAGUE_SWING, KIND_CHIP_WINDOW,
})

#: Reminder thresholds before a deadline, **tightest first**. Order matters: the
#: first matching bucket wins, and with two hours left both the 3h and the 24h
#: window technically contain "now". Checking widest-first would report "the
#: deadline is tomorrow" ninety minutes before it — and, because the bucket is
#: part of the dedupe key, would suppress the urgent reminder entirely.
DEADLINE_BUCKETS = (
    (timedelta(hours=1), CRITICAL, "in under an hour"),
    (timedelta(hours=3), CRITICAL, "in under 3 hours"),
    (timedelta(hours=24), IMPORTANT, "tomorrow"),
)

#: Publishing older than this means the site is serving stale advice.
STALE_AFTER = timedelta(hours=36)

#: A chip window closing within this many gameweeks is worth a nudge.
CHIP_WARN_EVENTS = 3


def _parse(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    t = raw.strip()
    if t.endswith(("Z", "z")):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    return None if dt.tzinfo is None else dt.astimezone(UTC)


def deadline_alerts(deadline: Any, gw: int | None, now: datetime) -> list[Alert]:
    """One reminder per bucket, and never after the deadline has passed."""
    dl = _parse(deadline)
    if dl is None or gw is None:
        return []
    remaining = dl - now
    if remaining <= timedelta(0):
        return []
    for window, severity, phrase in DEADLINE_BUCKETS:
        if remaining <= window:
            hours = remaining.total_seconds() / 3600
            return [Alert(
                kind=KIND_DEADLINE, severity=severity, event=gw,
                title=f"GW{gw} deadline {phrase}",
                body=(f"{hours:.1f} hours left. Check your transfer, captain "
                      "and bench order."),
                # The bucket, not the clock: a run 10 minutes later is the same
                # reminder and must not fire again.
                dedupe_parts=(int(window.total_seconds()),),
                deep_link="#/overview")]
    return []


def availability_alerts(
    owned: list[dict[str, Any]], gw: int | None = None,
) -> list[Alert]:
    """A flagged player you actually own. Keyed on the status text, so a
    50%-doubt becoming a 25%-doubt alerts again; a re-run of the same news does
    not."""
    out = []
    for p in owned:
        status = (p.get("status") or "a")
        if status == "a":
            continue
        chance = p.get("chance_playing")
        news = (p.get("news") or "").strip()
        severity = CRITICAL if status in ("i", "s", "u") else IMPORTANT
        out.append(Alert(
            kind=KIND_AVAILABILITY, severity=severity, event=gw,
            title=f"{p.get('name', 'A player')} is flagged",
            body=(news or f"status {status}")
            + (f" ({chance}% chance to play)" if chance is not None else ""),
            dedupe_parts=(p.get("id"), status, chance, news),
            deep_link="#/my-team"))
    return out


def recommendation_alerts(
    current: dict[str, Any] | None, previous: dict[str, Any] | None,
    gw: int | None = None,
) -> list[Alert]:
    """Only when the *decision* changed — not when its numbers wobbled.

    A run that moves the expected gain from 2.31 to 2.34 has not changed the
    advice, and alerting on it is how a user learns to ignore the alerts.
    """
    if not current or not previous:
        return []
    out = []
    if current.get("action") != previous.get("action"):
        out.append(Alert(
            kind=KIND_RECOMMENDATION, severity=IMPORTANT, event=gw,
            title="The recommendation changed",
            body=(f"Was: {previous.get('action')}. Now: {current.get('action')}. "
                  f"{current.get('headline', '')}").strip(),
            dedupe_parts=(previous.get("action"), current.get("action")),
            deep_link="#/overview"))
    elif sorted(current.get("transfers_in") or []) != sorted(
            previous.get("transfers_in") or []):
        out.append(Alert(
            kind=KIND_RECOMMENDATION, severity=IMPORTANT, event=gw,
            title="The recommended transfer changed",
            body=current.get("headline", "The suggested move is different."),
            dedupe_parts=(tuple(sorted(current.get("transfers_in") or [])),),
            deep_link="#/overview"))

    if (current.get("captain") is not None
            and current.get("captain") != previous.get("captain")):
        out.append(Alert(
            kind=KIND_CAPTAIN, severity=IMPORTANT, event=gw,
            title="Captain recommendation changed",
            body=(f"{previous.get('captain_name') or previous.get('captain')} → "
                  f"{current.get('captain_name') or current.get('captain')}"),
            dedupe_parts=(previous.get("captain"), current.get("captain")),
            deep_link="#/overview"))
    return out


def squad_state_alerts(meta: dict[str, Any], gw: int | None = None) -> list[Alert]:
    """Gaffer could not read your team. Silence here is the dangerous case."""
    status = meta.get("squad_status")
    broken = {"not_found", "fetch_failed", "malformed"}
    if status not in broken:
        return []
    return [Alert(
        kind=KIND_SQUAD_STATE, severity=IMPORTANT, event=gw,
        title="Gaffer cannot read your squad",
        body=(meta.get("squad_status_reason")
              or f"squad_status is {status}; recommendations are generic until "
                 "this is fixed"),
        dedupe_parts=(status, meta.get("squad_status_reason")),
        deep_link="#/my-team")]


def stale_alerts(generated_at: Any, now: datetime) -> list[Alert]:
    """The site is serving old advice. Bucketed by day so it nags once a day."""
    ts = _parse(generated_at)
    if ts is None:
        return [Alert(
            kind=KIND_STALE, severity=IMPORTANT,
            title="Gaffer's data has no timestamp",
            body="The published artifacts do not say when they were generated.",
            dedupe_parts=("no-timestamp",), deep_link="#/meta")]
    age = now - ts
    if age <= STALE_AFTER:
        return []
    return [Alert(
        kind=KIND_STALE, severity=IMPORTANT,
        title="Gaffer's data is stale",
        body=f"The last successful publish was {age.days}d "
             f"{int(age.seconds / 3600)}h ago.",
        dedupe_parts=(age.days,), deep_link="#/meta")]


def league_swing_alerts(
    swing: dict[str, Any] | None, gw: int | None = None,
    threshold: float = 8.0,
) -> list[Alert]:
    """A differential that actually moved your mini-league."""
    if not swing or abs(float(swing.get("swing") or 0)) < threshold:
        return []
    pts = float(swing["swing"])
    good = pts > 0
    return [Alert(
        kind=KIND_LEAGUE_SWING, severity=INFO, event=gw,
        title=("A differential is winning you the week" if good else
               "A rival's differential is hurting you"),
        body=f"{swing.get('name', 'A player')}: {pts:+.0f} points against your "
             "closest rival.",
        dedupe_parts=(swing.get("player_id"), round(pts)),
        deep_link="#/live")]


def chip_window_alerts(
    chips: dict[str, Any] | None, gw: int | None, now: datetime | None = None,
    warn_within: int = CHIP_WARN_EVENTS,
) -> list[Alert]:
    """An unused chip whose window is about to close."""
    if not chips or gw is None:
        return []
    used = set(chips.get("used") or [])
    out = []
    for w in chips.get("available") or []:
        name = w.get("name")
        stop = w.get("stop_event")
        if name in used or not isinstance(stop, int):
            continue
        left = stop - gw
        if 0 <= left <= warn_within:
            out.append(Alert(
                kind=KIND_CHIP_WINDOW, severity=IMPORTANT, event=gw,
                title=f"Your {name} expires after GW{stop}",
                body=(f"{left} gameweek(s) left to use it. An unused chip is "
                      "worth nothing."),
                dedupe_parts=(name, stop), deep_link="#/strategy"))
    return out


def build_alerts(
    *, meta: dict[str, Any], now: datetime,
    owned: list[dict[str, Any]] | None = None,
    current_decision: dict[str, Any] | None = None,
    previous_decision: dict[str, Any] | None = None,
    chips: dict[str, Any] | None = None,
    swing: dict[str, Any] | None = None,
) -> list[Alert]:
    """Everything worth saying this run, in one pass over the state."""
    gw = meta.get("current_gw")
    gw = int(gw) if str(gw).isdigit() else None
    stamped = now.isoformat(timespec="seconds")
    alerts = [
        *deadline_alerts(meta.get("deadline"), gw, now),
        *availability_alerts(owned or [], gw),
        *recommendation_alerts(current_decision, previous_decision, gw),
        *squad_state_alerts(meta, gw),
        *stale_alerts(meta.get("generated_at"), now),
        *league_swing_alerts(swing, gw),
        *chip_window_alerts(chips, gw, now),
    ]
    for a in alerts:
        a.created_at = stamped
    return alerts
