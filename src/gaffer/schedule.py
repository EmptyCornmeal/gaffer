"""Schedule-continuity logic for T-08.

GitHub disables ``schedule:`` triggers on a public repository after 60 days with
no repository activity. Workflow *runs* do not count; a push does.

The primary defence is the refresh workflow itself: every valid run advances
``generated_at`` in the artifacts, so there is always a diff to commit, and that
push resets the 60-day clock. No artificial churn is needed while the pipeline
is healthy.

The failure case this module covers is the pipeline being *unhealthy* for a long
time — FPL unreachable for weeks, or a bug that fails every run. Then nothing
pushes, and after 60 days the schedule is silently disabled on top of the
outage. The keepalive is a bounded watchdog for exactly that window: it acts
only when the repository is genuinely approaching the limit.

Pure functions, so the decision is unit-testable without a GitHub API call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: GitHub's documented inactivity limit for scheduled workflows (public repos).
GITHUB_DISABLE_DAYS = 60
#: Act with this much headroom. The keepalive runs monthly, so the gap between
#: consecutive runs is at most 31 days — comfortably inside 60 - 45 = 15 days of
#: slack even if one run is missed.
KEEPALIVE_THRESHOLD_DAYS = 45


# --- refresh gating --------------------------------------------------------
# GitHub's `schedule:` trigger is best-effort and drifts badly: measured over 61
# scheduled runs of this repo, the 17:00 UTC slot started a median of 53 minutes
# late, and on 2026-08-14 it fired at 17:46 — sixteen minutes after where a GW1
# deadline would have been. `gameweek.projection_event` rolls forward the moment
# a deadline passes, so a late run does not publish stale advice, it publishes an
# answer to a different question.
#
# You cannot make one cron punctual. You can give it many attempts and let all
# but the useful ones exit in seconds, which is what this decision is for: the
# workflow fires every 15 minutes and asks here whether there is any point.
#
# The windows are expressed as "how old may the last publish be", not "how often
# to run", because that is the property that actually matters to a reader.

#: The pre-deadline window opens this far out.
PRE_DEADLINE_OPEN = timedelta(hours=6)
#: ...and shuts here. Inside this, a run started now would finish after the
#: deadline and would already be projecting the *next* gameweek.
PRE_DEADLINE_CLOSE = timedelta(minutes=20)
#: The last stretch, where team news lands and the bar tightens.
FINAL_APPROACH = timedelta(hours=2)

#: Maximum tolerated age of the published artifacts, per window.
MAX_AGE = {
    "final_approach": timedelta(minutes=20),
    "pre_deadline": timedelta(minutes=90),
    "live": timedelta(minutes=60),
    "idle": timedelta(hours=6),
}

#: Fixture states that mean football is being played right now.
LIVE_FIXTURE_STATES = frozenset({"live", "half_time", "awaiting_bonus"})


@dataclass(frozen=True)
class RefreshDecision:
    should_refresh: bool
    window: str                 # final_approach | pre_deadline | live | idle
    age_minutes: float | None   # age of the published artifacts
    max_age_minutes: float
    reason: str


def _window(now: datetime, deadline: datetime | None,
            fixture_states: list[str] | None) -> str:
    """Which regime we are in. Deadline proximity outranks live football: if both
    are true you are picking a team, and that is the more urgent number."""
    if deadline is not None:
        until = deadline - now
        if PRE_DEADLINE_CLOSE < until <= FINAL_APPROACH:
            return "final_approach"
        if FINAL_APPROACH < until <= PRE_DEADLINE_OPEN:
            return "pre_deadline"
    if any(s in LIVE_FIXTURE_STATES for s in (fixture_states or [])):
        return "live"
    return "idle"


def should_refresh(
    now: datetime | None = None,
    *,
    deadline: datetime | None = None,
    last_generated_at: datetime | None = None,
    fixture_states: list[str] | None = None,
) -> RefreshDecision:
    """Decide whether this scheduled tick should actually run the pipeline.

    Pure: every input is data, so each boundary is testable against a fixed clock
    rather than by waiting for one.
    """
    now = now or datetime.now(UTC)
    window = _window(now, deadline, fixture_states)
    limit = MAX_AGE[window]
    limit_min = limit.total_seconds() / 60

    if last_generated_at is None:
        return RefreshDecision(
            True, window, None, limit_min,
            "nothing is published, or its timestamp could not be read — "
            "refreshing rather than assuming the site is current")
    age = now - last_generated_at
    if age < timedelta(0):
        # A future timestamp is a clock problem, not freshness. Treat it as
        # current so a skewed clock cannot trigger a refresh every 15 minutes.
        return RefreshDecision(
            False, window, 0.0, limit_min,
            f"published timestamp {last_generated_at.isoformat()} is in the "
            "future; treating as current and skipping")
    age_min = age.total_seconds() / 60
    if age >= limit:
        return RefreshDecision(
            True, window, age_min, limit_min,
            f"{window}: published data is {age_min:.0f} min old, over the "
            f"{limit_min:.0f} min bar for this window")
    return RefreshDecision(
        False, window, age_min, limit_min,
        f"{window}: published data is {age_min:.0f} min old, inside the "
        f"{limit_min:.0f} min bar — nothing to do")


def read_published_state(data_dir: Path | str = "data") -> dict[str, Any]:
    """Deadline, publish time and fixture states, from the committed artifacts.

    Deliberately tolerant: the gate runs before anything is installed and must
    never be the reason a refresh does not happen. Anything unreadable comes back
    as None, which `should_refresh` treats as "refresh".
    """
    d = Path(data_dir)
    out: dict[str, Any] = {"deadline": None, "generated_at": None,
                           "fixture_states": []}
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        out["deadline"] = parse_timestamp(meta.get("deadline"))
        out["generated_at"] = parse_timestamp(meta.get("generated_at"))
    except (OSError, ValueError, TypeError):
        pass
    try:
        live = json.loads((d / "live.json").read_text(encoding="utf-8"))
        out["fixture_states"] = [
            f.get("state") for f in (live.get("fixtures") or [])
            if isinstance(f, dict)
        ]
    except (OSError, ValueError, TypeError):
        pass
    return out


def render_refresh(decision: RefreshDecision) -> str:
    head = "REFRESH" if decision.should_refresh else "skip"
    age = "unknown" if decision.age_minutes is None else f"{decision.age_minutes:.0f} min"
    return (
        f"{head}\n"
        f"  window        : {decision.window}\n"
        f"  published age : {age} (bar: {decision.max_age_minutes:.0f} min)\n"
        f"  reason        : {decision.reason}"
    )


@dataclass(frozen=True)
class KeepaliveDecision:
    should_act: bool
    days_since_push: int | None
    days_until_disable: int | None
    reason: str


def parse_timestamp(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def evaluate(
    pushed_at: str | None,
    now: datetime | None = None,
    threshold_days: int = KEEPALIVE_THRESHOLD_DAYS,
) -> KeepaliveDecision:
    """Decide whether a keepalive push is warranted.

    Returns ``should_act=True`` only when the repository has been quiet for
    ``threshold_days`` or more. An unreadable timestamp is treated as "act", and
    says so — failing closed is safer than letting the schedule lapse silently.
    """
    now = now or datetime.now(UTC)
    ts = parse_timestamp(pushed_at)
    if ts is None:
        return KeepaliveDecision(
            True, None, None,
            f"could not parse pushed_at={pushed_at!r}; acting so the schedule "
            "cannot lapse while the check itself is broken",
        )
    if ts > now:
        return KeepaliveDecision(
            False, 0, GITHUB_DISABLE_DAYS,
            f"pushed_at {ts.isoformat()} is in the future; treating as fresh",
        )
    days = (now - ts).days
    remaining = GITHUB_DISABLE_DAYS - days
    if days >= threshold_days:
        return KeepaliveDecision(
            True, days, remaining,
            f"last push was {days}d ago (>= {threshold_days}d); scheduled "
            f"workflows are disabled at {GITHUB_DISABLE_DAYS}d, leaving "
            f"{remaining}d. The refresh has not pushed in that window, which "
            "means it is failing or producing no diff — investigate.",
        )
    return KeepaliveDecision(
        False, days, remaining,
        f"last push was {days}d ago (< {threshold_days}d); the refresh is "
        f"keeping the repository active, {remaining}d of slack remain",
    )


def render(decision: KeepaliveDecision) -> str:
    head = "KEEPALIVE REQUIRED" if decision.should_act else "no action needed"
    return (
        f"{head}\n"
        f"  days since last push : {decision.days_since_push}\n"
        f"  days until disable   : {decision.days_until_disable}\n"
        f"  reason               : {decision.reason}"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI used by the keepalive workflow. Exit 0 = no action, 10 = act."""
    import argparse
    import json
    import os

    ap = argparse.ArgumentParser(description="Decide if a keepalive push is needed")
    ap.add_argument("--pushed-at", default=os.environ.get("GAFFER_PUSHED_AT"))
    ap.add_argument("--threshold-days", type=int, default=KEEPALIVE_THRESHOLD_DAYS)
    ap.add_argument("--now", default=None, help="ISO 8601 override for testing")
    ap.add_argument("--json", action="store_true")
    # Refresh gating: a different question, same module, because both are
    # "should this scheduled workflow do anything".
    ap.add_argument("--should-refresh", action="store_true",
                    help="decide whether a scheduled refresh should run; prints "
                         "refresh=true|false for $GITHUB_OUTPUT")
    ap.add_argument("--force", action="store_true",
                    help="with --should-refresh: always decide yes (manual runs)")
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args(argv)

    if args.should_refresh:
        return _refresh_cli(args)

    d = evaluate(args.pushed_at, parse_timestamp(args.now), args.threshold_days)
    if args.json:
        print(json.dumps({
            "should_act": d.should_act,
            "days_since_push": d.days_since_push,
            "days_until_disable": d.days_until_disable,
            "reason": d.reason,
        }, indent=2))
    else:
        print(render(d))
    return 10 if d.should_act else 0


def _refresh_cli(args) -> int:
    """`--should-refresh`: one line for $GITHUB_OUTPUT, the reasoning on stderr.

    Always exits 0. A gate that fails the build when it cannot decide would turn
    a missing artifact into a red run for no reason; the decision itself already
    fails open.
    """
    import sys

    if args.force:
        d = RefreshDecision(True, "forced", None, 0.0,
                            "manual dispatch — always refreshes")
    else:
        state = read_published_state(args.data_dir)
        d = should_refresh(
            parse_timestamp(args.now),
            deadline=state["deadline"],
            last_generated_at=state["generated_at"],
            fixture_states=state["fixture_states"],
        )
    print(f"refresh={'true' if d.should_refresh else 'false'}")
    print(f"window={d.window}")
    print(render_refresh(d), file=sys.stderr)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
