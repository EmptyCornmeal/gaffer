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

#: RM-G27 -- how long after a deadline the gameweek can be locked with no
#: football being played yet.
#:
#: The gap between an FPL deadline and the first kick-off is usually about 90
#: minutes, but it is set by the fixture list, not by a rule: a Friday 18:30
#: deadline can precede a 20:00 kick-off, and an international-break gameweek
#: can leave a longer gap. Four hours bounds every shape of it without being so
#: wide that a genuinely idle mid-week afternoon is mistaken for one.
#:
#: This window self-closes: a run inside it republishes with the NEXT
#: gameweek's deadline (`ingest` writes whatever `gameweek.projection_event`
#: selects), so the state becomes `idle` again after one refresh. It costs one
#: prompt run, which is exactly what it is for.
LOCKED_WINDOW = timedelta(hours=4)

#: Maximum tolerated age of the published artifacts, per window. These are the
#: *loosest* bars. Inside the pre-deadline windows `_age_bar` tightens them as
#: the deadline closes in; nothing ever loosens them.
MAX_AGE = {
    "final_approach": timedelta(minutes=20),
    "pre_deadline": timedelta(minutes=90),
    "live": timedelta(minutes=60),
    # RM-G27. The deadline has passed and no ball has been kicked. Everything
    # on the screen is now advice about a gameweek nobody can act on, and under
    # the 6 h idle bar it could stand there for the whole gap.
    #
    # A tight bar is not about freshness for its own sake here: the refresh is
    # what rolls the published event forward, so it is what makes the site stop
    # offering a transfer for a locked squad.
    "locked": timedelta(minutes=15),
    "idle": timedelta(hours=6),
}

#: No bar ever goes below this, whatever the deadline arithmetic asks for. The
#: workflow installs, runs the tests, runs the pipeline and commits before
#: anything changes on screen, so two runs started a couple of minutes apart
#: publish very nearly the same artifact and the second is pure cost. Five
#: minutes is a deliberate floor, not a measurement — it is the point below
#: which a refresh is competing with the run already in flight.
MIN_AGE_BAR = timedelta(minutes=5)

#: How far apart *delivered* ticks actually are. The workflow asks every 15
#: minutes; GitHub delivers roughly 1 tick in 2.2, a median gap of 33 minutes
#: and a worst observed gap of 69. The bar below is discounted by the median,
#: not the worst case: discounting by 69 would pin the bar to MIN_AGE_BAR across
#: the whole two-hour final approach — a pipeline run on every delivered tick
#: for two hours — to insure against a gap seen once. The median already says
#: "expect no further chance" for everything inside ~53 minutes of the close,
#: which is where the damage in C6 was done.
TICK_GAP = timedelta(minutes=33)

#: Fixture states that mean football is being played right now.
LIVE_FIXTURE_STATES = frozenset({"live", "half_time", "awaiting_bonus"})

#: How long after kick-off a fixture can still be moving points: 90 minutes of
#: football, 15 of half time, stoppage at both ends, and the stretch after the
#: whistle where FPL is still settling bonus. Rounded up on purpose. Being wrong
#: on the high side costs one cheap tick that finds nothing to do; being wrong on
#: the low side freezes the live scores in the middle of a match.
MATCH_LIVE_WINDOW = timedelta(hours=2, minutes=45)

#: How far ahead of us a published timestamp may sit and still be believed.
#: Runner clocks drift by seconds, not hours. Beyond this the artifact is corrupt,
#: and a corrupt artifact must never be able to switch the schedule off: with no
#: bound, one bad timestamp makes ``age`` permanently negative and every later
#: tick skips forever. Inside the bound we still treat it as current, so ordinary
#: skew cannot trigger a refresh every 15 minutes.
CLOCK_SKEW_GRACE = timedelta(minutes=10)


@dataclass(frozen=True)
class RefreshDecision:
    should_refresh: bool
    window: str                 # final_approach | pre_deadline | live | idle
    age_minutes: float | None   # age of the published artifacts
    max_age_minutes: float
    reason: str


def _football_is_on(now: datetime, fixture_states: list[str] | None,
                    fixture_kickoffs: list[datetime] | None) -> bool:
    """Is a match in progress?

    C12: asking the fixture *states* alone cannot bootstrap. A state only changes
    when a refresh writes it, and the refresh only runs when this function says
    football is on — so a Saturday that begins with every fixture ``scheduled``
    keeps saying ``scheduled``, the gate stays in ``idle``, and the 6 h bar holds
    the live scores frozen right through the afternoon. The artifact was both the
    evidence and the thing that evidence was used to decide whether to update.

    Kick-off times break the loop, because they do not need a refresh to become
    true: this morning's publish already carries this afternoon's kick-offs, and
    a clock is enough to read them. States are still honoured first — they are
    the better signal whenever they are current, and they cover a match that runs
    long or sits in ``awaiting_bonus``.

    A kick-off moved after the last publish is still missed, and no reading of a
    stale artifact can fix that. The age bar remains the backstop for it.
    """
    if any(s in LIVE_FIXTURE_STATES for s in (fixture_states or [])):
        return True
    return any(
        ko is not None and timedelta(0) <= now - ko <= MATCH_LIVE_WINDOW
        for ko in (fixture_kickoffs or []))


def _window(now: datetime, deadline: datetime | None,
            fixture_states: list[str] | None,
            fixture_kickoffs: list[datetime] | None = None) -> str:
    """Which regime we are in. Deadline proximity outranks live football: if both
    are true you are picking a team, and that is the more urgent number."""
    if deadline is not None:
        until = deadline - now
        if PRE_DEADLINE_CLOSE < until <= FINAL_APPROACH:
            return "final_approach"
        if FINAL_APPROACH < until <= PRE_DEADLINE_OPEN:
            return "pre_deadline"
    if _football_is_on(now, fixture_states, fixture_kickoffs):
        return "live"
    # RM-G27 -- deadline gone, football not started. Checked AFTER `live`, so a
    # kick-off always wins: once the football is on, that is the more urgent
    # number and the window says so.
    if deadline is not None and -LOCKED_WINDOW <= (deadline - now) < timedelta(0):
        return "locked"
    return "idle"


def _age_bar(window: str, now: datetime, deadline: datetime | None) -> timedelta:
    """How old the published data may be, at this tick.

    A flat bar is an age-relative answer to a deadline-relative question, and C6
    is what that costs. With a flat 20-minute final-approach bar, the 17:00 tick
    before a 17:30 deadline sees data 19 minutes old, rules it inside the bar and
    skips; the 17:15 tick is past PRE_DEADLINE_CLOSE so the window never opens
    again; and the reader picks a team on a projection built at 16:41. The one
    decision this product exists for is made on the stalest data of the day.

    So stop asking "is this old?" and ask "is there still time to fix it?".
    Inside the pre-deadline windows the bar is the *usable* window remaining —
    the time left before the close, minus one tick gap, because one gap is the
    chance you can actually expect to be given — clamped to the window's own bar
    and floored at MIN_AGE_BAR.

    Two properties matter more than the exact numbers:

    * it only ever tightens a bar, never relaxes one, so nothing that refreshed
      under the old rule refreshes less often under this one; and
    * it assumes no future tick exists. Every tick is judged as though it were
      the last, which is the only assumption that survives a scheduler that
      delivers 1 tick in 2.2. "Skip it, the 17:15 tick will catch this" is
      precisely the reasoning that produced C6, and it is not repaired by
      choosing a different flat number.
    """
    limit = MAX_AGE[window]
    if deadline is None or window not in ("final_approach", "pre_deadline"):
        return limit
    usable = (deadline - now) - PRE_DEADLINE_CLOSE - TICK_GAP
    return max(MIN_AGE_BAR, min(limit, usable))


def should_refresh(
    now: datetime | None = None,
    *,
    deadline: datetime | None = None,
    last_generated_at: datetime | None = None,
    fixture_states: list[str] | None = None,
    fixture_kickoffs: list[datetime] | None = None,
    degraded: str | None = None,
) -> RefreshDecision:
    """Decide whether this scheduled tick should actually run the pipeline.

    Pure: every input is data, so each boundary is testable against a fixed clock
    rather than by waiting for one.

    ``degraded`` carries a description of why the published state could not be
    trusted, or None when it could. It exists because the two ways of being wrong
    here are not symmetric — see below.
    """
    now = now or datetime.now(UTC)
    window = _window(now, deadline, fixture_states, fixture_kickoffs)
    # RM-G27, refined. `locked` is a tight bar for one specific harm: advice
    # written BEFORE a deadline still standing after it, for a squad nobody can
    # change. A publish that already happened after the deadline has answered
    # that -- it rolled the event forward -- so judging it on a 15-minute bar
    # would be churn, not correctness.
    if (window == "locked" and deadline is not None
            and last_generated_at is not None and last_generated_at >= deadline):
        window = "idle"
    limit = _age_bar(window, now, deadline)
    limit_min = limit.total_seconds() / 60

    if degraded:
        # Corruption fails OPEN, deliberately. The window was derived from the
        # very artifacts we just failed to read, so the bar it selected is not
        # evidence of anything: an unreadable deadline silently downgrades a
        # deadline day to the 6 h idle bar, and the reader is shown stale advice
        # with nothing on screen to say so. A needless pipeline run costs a few
        # minutes of CI and leaves a visible log line.
        return RefreshDecision(
            True, window, None, limit_min,
            f"published state is unreliable ({degraded}) — refreshing rather "
            f"than trusting the {window} bar computed from it")

    if last_generated_at is None:
        return RefreshDecision(
            True, window, None, limit_min,
            "nothing is published, or its timestamp could not be read — "
            "refreshing rather than assuming the site is current")
    age = now - last_generated_at
    if age < -CLOCK_SKEW_GRACE:
        # Far in the future is not skew, it is a corrupt timestamp — and left
        # unbounded it disables the schedule permanently, because every later
        # tick also computes a negative age and skips.
        ahead = -age.total_seconds() / 60
        grace = CLOCK_SKEW_GRACE.total_seconds() / 60
        return RefreshDecision(
            True, window, None, limit_min,
            f"published timestamp {last_generated_at.isoformat()} is "
            f"{ahead:.0f} min ahead of now, past the {grace:.0f} min clock-skew "
            "allowance — treating the artifact as corrupt and refreshing")
    # A build made before a deadline that has since passed cannot know the squad:
    # FPL exposes no picks until the deadline, so every artifact downstream of it
    # says "we do not know your squad" about a squad that is now readable. This is
    # not staleness — the published answer was correct when written and is void
    # now — so it is decided before the age bar rather than by it.
    #
    # `deadline` is the published build's OWN target deadline, which is why this
    # cannot loop: a successful refresh advances it to the next gameweek, putting
    # it in the future and switching this branch off.
    if deadline is not None and deadline <= now and last_generated_at < deadline:
        return RefreshDecision(
            True, window, (now - last_generated_at).total_seconds() / 60, limit_min,
            f"published build predates the {deadline.isoformat()} deadline, which "
            "has now passed — FPL is exposing picks it cannot have read")

    if age < timedelta(0):
        # Ordinary runner skew. Treat as current; it self-heals within minutes.
        age = timedelta(0)
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
    never be the reason a refresh does not happen.

    It separates two kinds of unreadable, because they mean opposite things:

    * **absent** — ordinary. ``live.json`` does not exist before the first run,
      and a deadline is legitimately absent once the season ends. Not degraded.
    * **present but unusable** — truncated JSON, a payload of the wrong shape, a
      timestamp that will not parse. That is corruption, and it comes back in
      ``degraded`` so the caller can fail open instead of quietly selecting a
      window from data it could not read.
    """
    d = Path(data_dir)
    out: dict[str, Any] = {"deadline": None, "generated_at": None,
                           "fixture_states": [], "fixture_kickoffs": [],
                           "degraded": None}
    faults: list[str] = []

    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            faults.append(f"meta.json is a {type(meta).__name__}, not an object")
        else:
            for field in ("deadline", "generated_at"):
                raw = meta.get(field)
                out[field] = parse_timestamp(raw)
                if raw is not None and out[field] is None:
                    faults.append(f"meta.{field}={raw!r} is not a timestamp")
    except FileNotFoundError:
        pass                          # first run: nothing has been published yet
    except (OSError, ValueError, TypeError, AttributeError) as e:
        faults.append(f"meta.json unreadable ({type(e).__name__})")

    try:
        live = json.loads((d / "live.json").read_text(encoding="utf-8"))
        if not isinstance(live, dict):
            faults.append(f"live.json is a {type(live).__name__}, not an object")
        else:
            fixtures = live.get("fixtures")
            if fixtures is None:
                pass                  # a published gameweek with no fixture list
            elif not isinstance(fixtures, list):
                faults.append(
                    f"live.fixtures is a {type(fixtures).__name__}, not a list")
            else:
                entries = [f for f in fixtures if isinstance(f, dict)]
                out["fixture_states"] = [f.get("state") for f in entries]
                # Kick-offs, because states alone cannot bootstrap the live
                # window — see `_football_is_on`. `kickoff` is what the artifact
                # writes and `kickoff_time` is FPL's own name for the same
                # field, accepted so a future artifact that passes it through
                # unrenamed still reads.
                #
                # A missing or unparseable kick-off is dropped, not recorded as a
                # fault: a fixture with no confirmed date legitimately has none,
                # and `degraded` forces a refresh on every single tick — far too
                # big a hammer to hand to one odd row in a fixture list.
                out["fixture_kickoffs"] = [
                    ts for ts in (
                        parse_timestamp(f.get("kickoff") or f.get("kickoff_time"))
                        for f in entries)
                    if ts is not None
                ]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError) as e:
        faults.append(f"live.json unreadable ({type(e).__name__})")

    if faults:
        out["degraded"] = "; ".join(faults)
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

    Always exits 0, and always prints a decision. The workflow runs the pipeline
    only when this prints ``refresh=true``, so any path that crashes or prints
    nothing switches the schedule off silently — far worse than a wasted run.
    Whatever goes wrong in here, the answer is yes.
    """
    import sys

    try:
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
                fixture_kickoffs=state["fixture_kickoffs"],
                degraded=state["degraded"],
            )
    except Exception as e:
        # The catch-all is the point: an unanticipated shape must not be able to
        # stop the schedule. It is reported loudly on stderr, never swallowed.
        d = RefreshDecision(
            True, "unknown", None, 0.0,
            f"the gate itself failed ({type(e).__name__}: {e}) — refreshing, "
            "because a broken gate must never be the reason nothing runs")
    print(f"refresh={'true' if d.should_refresh else 'false'}")
    print(f"window={d.window}")
    print(render_refresh(d), file=sys.stderr)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
