"""T-05 — projection event vs readable-squad event.

Every case uses a fixed clock and synthetic events. Nothing here may depend on
today's date or live FPL state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gaffer import gameweek as G

GW1_DL = datetime(2026, 8, 21, 17, 30, tzinfo=UTC)
GW2_DL = datetime(2026, 8, 28, 17, 30, tzinfo=UTC)
GW3_DL = datetime(2026, 9, 12, 17, 30, tzinfo=UTC)


def ev(i, deadline, finished=False, **kw):
    d = {"id": i, "name": f"Gameweek {i}",
         "deadline_time": deadline.isoformat().replace("+00:00", "Z"),
         "finished": finished}
    d.update(kw)
    return d


SEASON = [ev(1, GW1_DL), ev(2, GW2_DL), ev(3, GW3_DL)]


def played(upto: int):
    """Events with the first `upto` finished."""
    return [
        ev(i, dl, finished=(i <= upto))
        for i, dl in ((1, GW1_DL), (2, GW2_DL), (3, GW3_DL))
    ]


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------

def test_preseason_before_gw1():
    now = GW1_DL - timedelta(days=15)
    assert G.projection_event(SEASON, now) == 1
    # A public entry genuinely has no readable squad before the first deadline.
    assert G.readable_squad_event(SEASON, now) is None
    assert G.last_finished_event(SEASON) is None


def test_immediately_before_a_deadline():
    now = GW1_DL - timedelta(seconds=1)
    assert G.projection_event(SEASON, now) == 1
    assert G.readable_squad_event(SEASON, now) is None


def test_exactly_at_the_deadline_picks_become_readable():
    assert G.projection_event(SEASON, GW1_DL) == 2
    assert G.readable_squad_event(SEASON, GW1_DL) == 1


def test_immediately_after_a_deadline():
    now = GW1_DL + timedelta(seconds=1)
    # We now plan for GW2 while the squad we hold is the one revealed for GW1.
    assert G.projection_event(SEASON, now) == 2
    assert G.readable_squad_event(SEASON, now) == 1


def test_normal_between_gameweek_operation():
    now = GW1_DL + timedelta(days=3)  # GW1 played, GW2 not yet
    evs = played(1)
    assert G.projection_event(evs, now) == 2
    assert G.readable_squad_event(evs, now) == 1
    assert G.last_finished_event(evs) == 1


def test_finished_previous_plus_upcoming_next():
    now = GW2_DL - timedelta(hours=2)
    evs = played(1)
    assert G.projection_event(evs, now) == 2
    assert G.readable_squad_event(evs, now) == 1


def test_mid_season_progression():
    now = GW2_DL + timedelta(days=1)
    evs = played(2)
    assert G.projection_event(evs, now) == 3
    assert G.readable_squad_event(evs, now) == 2


def test_after_the_final_deadline():
    now = GW3_DL + timedelta(days=30)
    evs = played(3)
    # Nothing left to act on; fall back to the last event rather than crashing.
    assert G.projection_event(evs, now) == 3
    assert G.readable_squad_event(evs, now) == 3


def test_projection_and_squad_events_are_never_equal_mid_season():
    """The invariant the old code violated on every single run."""
    for offset in (timedelta(seconds=1), timedelta(days=2), timedelta(days=6)):
        now = GW1_DL + offset
        assert G.projection_event(SEASON, now) != G.readable_squad_event(SEASON, now)


# --------------------------------------------------------------------------
# Degenerate inputs
# --------------------------------------------------------------------------

def test_empty_events():
    assert G.projection_event([], GW1_DL) == 1
    assert G.readable_squad_event([], GW1_DL) is None
    assert G.last_finished_event([]) is None


def test_missing_deadlines_fall_back_to_api_flags():
    evs = [
        {"id": 1, "finished": True},
        {"id": 2, "finished": False, "is_next": True},
    ]
    assert G.projection_event(evs, GW1_DL) == 2
    assert G.readable_squad_event(evs, GW1_DL) == 1


def test_unparseable_deadline_is_ignored_not_guessed():
    evs = [ev(1, GW1_DL), {"id": 2, "deadline_time": "not-a-date", "finished": False}]
    assert G.parse_deadline("not-a-date") is None
    now = GW1_DL + timedelta(days=1)
    assert G.readable_squad_event(evs, now) == 1


def test_events_out_of_order_are_sorted():
    evs = [ev(3, GW3_DL), ev(1, GW1_DL), ev(2, GW2_DL)]
    now = GW1_DL + timedelta(days=1)
    assert G.projection_event(evs, now) == 2
    assert G.readable_squad_event(evs, now) == 1


@pytest.mark.parametrize("raw,expected", [
    ("2026-08-21T17:30:00Z", GW1_DL),
    ("2026-08-21T17:30:00+00:00", GW1_DL),
    ("2026-08-21T17:30:00", GW1_DL),  # naive -> assumed UTC
    ("", None), (None, None), ("nonsense", None), (12345, None),
])
def test_parse_deadline(raw, expected):
    assert G.parse_deadline(raw) == expected


def test_describe_reports_all_three():
    now = GW1_DL + timedelta(days=1)
    d = G.describe(played(1), now)
    assert d["projection_event"] == 2
    assert d["squad_source_event"] == 1
    assert d["last_finished_event"] == 1
    assert d["resolved_at"].startswith("2026-08-22")


def test_status_vocabulary_is_partitioned():
    """No status may mean both 'squad stored' and 'no squad stored'."""
    assert not (G.STATUSES_WITH_SQUAD & G.STATUSES_WITHOUT_SQUAD)
    assert G.ALL_STATUSES == G.STATUSES_WITH_SQUAD | G.STATUSES_WITHOUT_SQUAD
    assert G.STATUS_LOADED in G.STATUSES_WITH_SQUAD
    assert G.STATUS_NO_PUBLIC_SQUAD_YET in G.STATUSES_WITHOUT_SQUAD
