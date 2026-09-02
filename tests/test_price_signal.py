"""5.2 -- the price signal is FPL's, not a guess at FPL's.

Gaffer approximated the secret price-change threshold as 7.5% of the owner base
and reported progress against it. That was not merely imprecise. It modelled
the wrong variable: this week Bailey is due to fall on -1,395 net transfers
while Osula needs -28,786, and no function of net transfers over owner base
returns both. An estimator of the wrong quantity cannot be fixed by tuning it,
so it was deleted rather than improved.
"""
from __future__ import annotations

import json

from gaffer.export import artifacts


class Row(dict):
    """A stand-in for a sqlite3.Row: subscript access, KeyError when absent."""


def _row(**over):
    base = {
        "price_change_percent": 42.0,
        "price_change_locked_until": None,
        "price_change_projections": json.dumps([
            {"offset": 0, "projected_percent": "45.5", "likelihood": 2},
            {"offset": 2, "projected_percent": "88.0", "likelihood": 4},
        ]),
        "price_change_hourly_rate": 31.0,
        "price_change_calibrating": 0,
    }
    base.update(over)
    return Row(base)


def test_it_reports_fpls_number_rather_than_deriving_one():
    sig = artifacts._price_signal(_row(), net=12_000)
    assert sig["available"] is True
    assert sig["percent"] == 42.0
    assert "published by FPL" in sig["basis"]
    assert "estimated" not in sig["basis"]


def test_the_estimator_and_its_invented_threshold_are_gone():
    """Named explicitly so a later change cannot quietly reintroduce a guess
    under the same key."""
    assert not hasattr(artifacts, "_price_pred")
    sig = artifacts._price_signal(_row(), net=1)
    assert "threshold" not in sig
    assert "progress" not in sig


def test_a_change_past_100_percent_is_due_and_is_not_clipped():
    """-110 means the fall is already due. Clamping it to -100 would throw away
    the only part of the number that says so."""
    sig = artifacts._price_signal(_row(price_change_percent=-109.6), net=-28_786)
    assert sig["percent"] == -109.6
    assert sig["due"] is True
    assert sig["dir"] == "falling"


def test_direction_distinguishes_moving_from_arrived():
    assert artifacts._price_signal(_row(price_change_percent=42.0), net=1)["dir"] == "up"
    assert artifacts._price_signal(_row(price_change_percent=101.0), net=1)["dir"] == "rising"
    assert artifacts._price_signal(_row(price_change_percent=-42.0), net=-1)["dir"] == "down"
    assert artifacts._price_signal(_row(price_change_percent=-101.0), net=-1)["dir"] == "falling"
    assert artifacts._price_signal(_row(price_change_percent=0.0), net=0)["dir"] == "stable"


def test_the_projection_keeps_its_likelihood_grade():
    """FPL grades its own projection from -5 to +5. Dropping the grade would
    turn a hedged forecast into a flat claim."""
    sig = artifacts._price_signal(_row(), net=1)
    assert sig["projections"] == [
        {"offset": 0, "percent": 45.5, "likelihood": 2},
        {"offset": 2, "percent": 88.0, "likelihood": 4},
    ]


def test_net_transfers_survive_as_context_not_as_evidence():
    """`momentum` says why the number is moving. It is no longer allowed to say
    where it will land, which is exactly what the estimator did."""
    sig = artifacts._price_signal(_row(), net=-1_395)
    assert sig["momentum"] == -1_395


def test_a_player_fpl_publishes_nothing_for_says_so():
    """A stated absence, not a zero. "No published progress" and "heading
    nowhere" are different claims."""
    sig = artifacts._price_signal(_row(price_change_percent=None), net=500)
    assert sig["available"] is False
    assert sig["reason"]
    assert sig["dir"] == "unknown"
    assert "percent" not in sig


def test_malformed_projections_do_not_take_the_signal_down():
    for bad in ("not json", "[1,2,3]", "{}", None):
        sig = artifacts._price_signal(
            _row(price_change_projections=bad), net=1)
        assert sig["available"] is True
        assert isinstance(sig["projections"], list)


def test_a_lock_is_carried_through():
    sig = artifacts._price_signal(
        _row(price_change_locked_until="2026-09-04T19:30:49Z"), net=1)
    assert sig["locked_until"] == "2026-09-04T19:30:49Z"
