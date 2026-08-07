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

from dataclasses import dataclass
from datetime import UTC, datetime

#: GitHub's documented inactivity limit for scheduled workflows (public repos).
GITHUB_DISABLE_DAYS = 60
#: Act with this much headroom. The keepalive runs monthly, so the gap between
#: consecutive runs is at most 31 days — comfortably inside 60 - 45 = 15 days of
#: slack even if one run is missed.
KEEPALIVE_THRESHOLD_DAYS = 45


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
    args = ap.parse_args(argv)

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


if __name__ == "__main__":
    import sys

    sys.exit(main())
