"""1.3 -- a hold must name the date it becomes a loss.

Gaffer recommended `hold` on all four first-half chips while a scan of the
published calendar showed every gameweek GW1-GW38 with exactly ten fixtures
and none unscheduled: no double and no blank anywhere. "Hold for a double" was
a bet on a fixture that does not exist, and the chips expire at GW19 -- before
cup postponements historically create one.
"""
from __future__ import annotations

from gaffer import chips as CH


def _cal(gws, doubles=(), blanks=()):
    return {g: {"double_teams": 1 if g in doubles else 0,
                "blank_teams": 1 if g in blanks else 0,
                "fixtures": 10} for g in gws}


def test_an_empty_calendar_window_says_there_is_nothing_to_wait_for():
    r = CH.expiry_report("wildcard", 3, 19, _cal(range(3, 39)))
    assert r["stop_event"] == 19
    assert r["gameweeks_left_including_this_one"] == 17
    assert r["doubles_in_window"] == [] and r["blanks_in_window"] == []
    assert "NO double and NO blank" in r["note"]
    assert "GW19" in r["note"]


def test_a_double_inside_the_window_is_named():
    r = CH.expiry_report("bboost", 3, 19, _cal(range(3, 39), doubles=(14,)))
    assert r["doubles_in_window"] == [14]
    assert "14" in r["note"]


def test_a_double_beyond_the_window_is_not_offered_as_a_reason_to_hold():
    """GW25 is real and irrelevant: the chip is gone by then."""
    r = CH.expiry_report("bboost", 3, 19, _cal(range(3, 39), doubles=(25,)))
    assert r["doubles_in_window"] == []
    assert "NO double" in r["note"]


def test_an_unknown_window_end_is_said_rather_than_assumed():
    r = CH.expiry_report("wildcard", 3, None, _cal(range(3, 39)))
    assert "unknown" in r["note"]
    assert "gameweeks_left_including_this_one" not in r


def test_an_unreadable_calendar_does_not_invent_an_empty_one():
    """No fixtures is 'could not read', not 'there are none'."""
    r = CH.expiry_report("wildcard", 3, 19, {})
    assert "could not be read" in r["note"]
    assert r["calendar_checked_through"] is None


def test_the_hold_reason_carries_the_expiry():
    """The whole point: it must reach the RECOMMENDATION, not only a nested
    limitations block nobody reads."""
    import inspect
    src = inspect.getsource(CH.plan_chips)
    assert "with_expiry(" in src
    assert src.count("with_expiry(") >= 3, (
        "every hold-with-candidate path must carry it")
