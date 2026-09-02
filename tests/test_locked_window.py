"""5.4 / RM-G27 -- the gap between the deadline and the first kick-off.

For roughly ninety minutes a week the deadline has gone, the squad is locked,
and no ball has been kicked. `_window` had no name for that: the pre-deadline
branches need a positive `until`, no fixture is live, so it fell through to
`idle` -- a six-hour staleness bar over advice nobody can act on any more.

The window is not about freshness for its own sake. A refresh inside it is what
rolls `projection_event` forward, so it is what makes the site stop offering a
transfer for a squad that is already locked.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gaffer import schedule

DEADLINE = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)


def _win(minutes_after: float, **kw):
    return schedule._window(DEADLINE + timedelta(minutes=minutes_after),
                            DEADLINE, kw.get("states"), kw.get("kickoffs"))


def test_the_minutes_after_a_deadline_are_locked_not_idle():
    """The regression itself. One minute past the deadline nothing about the
    published advice is actionable, and the old code gave it the same bar as a
    Tuesday afternoon."""
    assert _win(1) == "locked"
    assert _win(89) == "locked"


def test_the_bar_in_that_window_is_tight_enough_to_force_one_refresh():
    """Six hours would let a locked gameweek's pre-deadline advice stand for
    the entire gap. Fifteen minutes buys exactly one prompt run, which is all
    it takes: that run republishes with the next gameweek's deadline."""
    assert schedule.MAX_AGE["locked"] == timedelta(minutes=15)
    assert schedule.MAX_AGE["locked"] < schedule.MAX_AGE["idle"]


def test_it_ends_rather_than_running_forever():
    """A bounded window. Long after a deadline with no football, the honest
    answer is idle -- an unbounded `locked` would refresh every fifteen minutes
    through an international break."""
    assert _win(schedule.LOCKED_WINDOW.total_seconds() / 60 + 1) == "idle"


def test_kick_off_wins():
    """Checked after `live` on purpose: once the football is on, that is the
    more urgent number and the window has to say so."""
    assert _win(120, states=["live"]) == "live"


def test_before_the_deadline_nothing_changes():
    """The windows that already worked must be untouched -- this is a new state
    for an unnamed gap, not a re-cut of the existing ones."""
    assert _win(-30) == "final_approach"
    assert _win(-3 * 60) == "pre_deadline"
    assert _win(-10 * 60) == "idle"


def test_no_deadline_is_still_idle():
    assert schedule._window(datetime.now(UTC), None, None, None) == "idle"


def test_should_refresh_acts_on_stale_advice_in_the_locked_window():
    """End to end: the reason the window exists."""
    now = DEADLINE + timedelta(minutes=20)
    d = schedule.should_refresh(
        now, deadline=DEADLINE,
        last_generated_at=DEADLINE - timedelta(minutes=25))
    assert d.window == "locked"
    assert d.should_refresh, d.reason


def test_advice_already_written_after_the_deadline_is_not_locked():
    """It forces one run, not a run every tick.

    The window exists for one harm: advice written BEFORE a deadline still
    standing after it, for a squad nobody can change. A publish that already
    happened after the deadline has answered that -- it is what rolled the
    event forward -- so holding it to a fifteen-minute bar would be churn
    rather than correctness."""
    now = DEADLINE + timedelta(minutes=20)
    d = schedule.should_refresh(
        now, deadline=DEADLINE,
        last_generated_at=DEADLINE + timedelta(minutes=17))
    assert d.window == "idle"
    assert not d.should_refresh, d.reason


def test_the_window_is_about_what_is_published_not_only_the_clock():
    """The same instant, judged twice. Only the age of the published advice
    differs, and that is the whole distinction."""
    now = DEADLINE + timedelta(minutes=20)
    before = schedule.should_refresh(
        now, deadline=DEADLINE,
        last_generated_at=DEADLINE - timedelta(minutes=5))
    after = schedule.should_refresh(
        now, deadline=DEADLINE,
        last_generated_at=DEADLINE + timedelta(minutes=5))
    assert before.window == "locked" and before.should_refresh
    assert after.window == "idle" and not after.should_refresh


def test_the_window_is_published_so_the_browser_cannot_disagree():
    """The site evaluates the same policy. Two staleness opinions about one
    artifact is the bug the published policy was introduced to end."""
    assert schedule.LOCKED_WINDOW == timedelta(hours=4)
    assert "locked" in schedule.MAX_AGE
