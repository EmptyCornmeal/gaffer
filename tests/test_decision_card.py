"""4.1 / 4.5 / 4.6 -- one decision card, on every surface.

The same recommendation used to be assembled three times by three renderers
with three ideas of what mattered, and the post-gameweek review scored a
reconstruction of what had been on screen rather than the thing itself. These
tests hold the card's shape, its refusals, and the digest that makes "the site,
the MCP and the snapshot agree" a checkable claim instead of a hope.
"""
from __future__ import annotations

import json

import pytest

from gaffer import card


def _decision(**over):
    base = {
        "action": "transfer",
        "headline": "Tzolis -> Semenyo",
        "reason": "because",
        "transfers_out": [1],
        "transfers_in": [2],
        "captain": 3,
        "vice": 4,
        "confidence": "clear",
        "biggest_risk": "Semenyo is a rotation risk",
        "assumptions": ["squad is as of GW3"],
        "thresholds": {"basis": "policy", "fitted": False,
                       "min_actionable_points": 1.0,
                       "min_actionable_probability": 0.55},
        "comparison": {
            "move_expected": 52.0, "hold_expected": 50.0, "delta": 2.0,
            "delta_ci95": [1.4, 2.6], "delta_ci95_interval_type": "monte_carlo",
            "delta_range_p10_p90": [-6.0, 11.0],
            "delta_range_interval_type": "prediction",
            "p_move_beats_hold": 0.61, "simulations": 4000, "hit_cost": 0,
            "horizon_delta": 3.4,
            "domain": {"delta": "the next gameweek only",
                       "horizon_delta": "gameweeks 2 onward"},
        },
        "executability": {"affordable": True, "paid_transfers": 0,
                          "free_transfers_before": 1, "free_transfers_after": 0,
                          "bank_before": 12, "bank_after": 5, "reason": ""},
        "evidence_quality": {"available": True, "weak_evidence_share": 0.26},
        "league_effects": [{"league_id": 1, "effect": "+0.4"}],
    }
    base.update(over)
    return base


NAMES = {1: {"id": 1, "name": "Tzolis", "team": "BOU", "pos": "MID"},
         2: {"id": 2, "name": "Semenyo", "team": "BOU", "pos": "MID"},
         3: {"id": 3, "name": "Haaland", "team": "MCI", "pos": "FWD"},
         4: {"id": 4, "name": "Raya", "team": "ARS", "pos": "GKP"}}


def _build(**over):
    return card.build(_decision(**over), gameweek=4, horizon=6,
                      resolve=NAMES.get)


# --------------------------------------------------------------------------
# 4.1 -- the schema
# --------------------------------------------------------------------------

def test_the_card_carries_every_field_the_schema_names():
    """A card missing a field is a renderer quietly dropping part of the
    answer, which is the failure this whole task exists to stop."""
    c = _build()
    assert tuple(k for k in c if k != card.HASH_FIELD) == card.CARD_FIELDS


def test_the_two_intervals_are_different_quantities_and_both_say_so():
    """The reason §0.3 makes every interval name its type.

    `ci95` is simulation error on the mean and shrinks as draws rise;
    `realistic_range` is the spread of football outcomes and does not shrink at
    all. A +2.0 edge with a -6 to +11 range is a completely different claim
    from "+2.0 (±0.6)", and a card that showed only one of them would mislead
    whichever way it chose."""
    m = _build()["margin"]
    assert m["interval_type"] == "monte_carlo"
    assert m["ci95"] == [1.4, 2.6]
    assert m["realistic_range_interval_type"] == "prediction"
    assert m["realistic_range"] == [-6.0, 11.0]


def test_upside_and_downside_are_the_prediction_interval_not_the_error_bar():
    c = _build()
    assert c["upside"]["value"] == 11.0
    assert c["downside"]["value"] == -6.0
    assert c["upside"]["interval_type"] == "prediction"


def test_the_strength_says_the_bars_are_policy_not_fitted():
    s = _build()["strength"]
    assert s["fitted"] is False
    assert s["basis"] == "policy"
    assert "not a fitted parameter" in s["note"]


def test_the_alternatives_include_the_thing_it_actually_beat():
    alts = _build()["alternatives"]
    assert alts[0]["option"] == "hold"
    assert alts[0]["expected"] == 50.0


def test_what_would_change_it_is_arithmetic_not_a_platitude():
    c = _build()
    changers = c["what_would_change_it"]
    assert any("1.0" in x for x in changers)
    assert any("61%" in x for x in changers)
    assert "Semenyo is a rotation risk" in changers


def test_ids_are_resolved_so_the_card_is_self_contained():
    """The snapshot and the site must hash to the same digest, which they
    cannot do if one of them still needs a join to be readable."""
    c = _build()
    assert c["recommendation"]["transfers_in"][0]["name"] == "Semenyo"
    assert c["recommendation"]["captain"]["name"] == "Haaland"


def test_an_unresolvable_id_is_carried_not_dropped():
    c = card.build(_decision(transfers_in=[99]), gameweek=4, horizon=6,
                   resolve=NAMES.get)
    assert c["recommendation"]["transfers_in"][0] == {
        "id": 99, "name": None, "team": None, "pos": None}


# --------------------------------------------------------------------------
# Absence is a value
# --------------------------------------------------------------------------

def test_a_field_that_could_not_be_filled_says_why():
    """An empty list and "we could not measure this" are different answers,
    and the review scores what was shown -- including a shown absence."""
    c = card.build(_decision(comparison=None, league_effects=None,
                             evidence_quality=None, biggest_risk="",
                             assumptions=[]),
                   gameweek=4, horizon=6, resolve=NAMES.get)
    for field in ("margin", "upside", "downside", "sensitivity",
                  "league_effect", "evidence_quality", "what_would_change_it"):
        assert c[field]["available"] is False, field
        assert c[field]["reason"], field


# --------------------------------------------------------------------------
# 4.6 -- the digest
# --------------------------------------------------------------------------

def test_the_card_verifies_against_its_own_hash():
    ok, why = card.verify(_build())
    assert ok, why


def test_the_hash_is_stable_across_json_round_trips():
    """Two surfaces must agree on the digest even when one of them round-trips
    the object through a different JSON writer."""
    c = _build()
    again = json.loads(json.dumps(c))
    assert card.content_hash(again) == c[card.HASH_FIELD]


def test_the_hash_does_not_depend_on_key_order():
    c = _build()
    shuffled = dict(reversed(list(c.items())))
    assert card.content_hash(shuffled) == c[card.HASH_FIELD]


def test_tampering_with_any_field_breaks_verification():
    """The point of the digest: a surface that changed what it shows cannot
    keep claiming to show the canonical card."""
    for path, value in [
        (("recommendation", "headline"), "Something else"),
        (("margin", "value"), 99.0),
        (("cost", "hit_points"), 4),
        (("strength", "label"), "certain"),
    ]:
        c = _build()
        node = c
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        ok, why = card.verify(c)
        assert not ok, f"tampering with {path} went undetected"
        assert "recomputes to" in why


@pytest.mark.parametrize("bad", [None, [], "card", 7, {}, {"content_hash": "x"}])
def test_a_malformed_card_is_refused_with_a_reason(bad):
    ok, why = card.verify(bad)
    assert not ok
    assert why


def test_the_version_travels_with_the_card():
    """A stored card is a record of what was shown. Re-versioning history would
    be a lie about the past, so the version is part of the hashed body."""
    c = _build()
    assert c["card_version"] == card.CARD_VERSION
    c["card_version"] = "decision-card-99"
    ok, _ = card.verify(c)
    assert not ok
