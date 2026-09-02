"""4.2/4.3/4.4 -- a projection says what it rests on.

Gaffer has always measured which of its own components are unreliable and has
always published that on one page, then spent them at full confidence on every
other. These tests hold the wiring between the two halves, and — more
importantly — hold the *semantics*, because the easiest way to lose this is to
quietly rename it `confidence` and turn a statement about evidence into a
claim about correctness.
"""
from __future__ import annotations

import pytest

from gaffer.model import projection as P

# --------------------------------------------------------------------------
# The distinction the whole feature exists to preserve
# --------------------------------------------------------------------------

def test_it_is_evidence_quality_and_never_confidence():
    """"56% of this projection depends on poorly validated components" and
    "this recommendation is 44% likely to be correct" are different claims.

    Gaffer has no calibrated probability that a recommendation beats its
    alternative, so it must not publish a field whose name asserts one. If a
    future release earns that number, `confidence` is free for it — and this
    test should then be changed deliberately rather than eroded."""
    eq = P.evidence_quality({"appearance": 1.0, "clean_sheet": 1.0})
    assert "weak_evidence_share" in eq
    assert not any("confidence" in k for k in eq)
    assert "EVIDENCE" in eq["policy"].upper()
    # And the word does not sneak in through the component table either.
    for meta in P.COMPONENT_EVIDENCE.values():
        assert "confidence" not in meta["status"]


def test_measured_and_failed_is_not_the_same_as_never_measured():
    """Collapsing these two would be a confidence violation in the other
    direction: treating absence of proof as proof of failure. The clean-sheet
    term was measured and lost; the saves term was never measured at all."""
    assert P.COMPONENT_EVIDENCE["clean_sheet"]["status"] == P.WEAK_OR_FAILED
    assert P.COMPONENT_EVIDENCE["saves"]["status"] == P.INSUFFICIENT_EVIDENCE
    assert P.WEAK_OR_FAILED != P.INSUFFICIENT_EVIDENCE
    eq = P.evidence_quality({"clean_sheet": 1.0, "saves": 1.0})
    assert eq["share_by_status"][P.WEAK_OR_FAILED] == 0.5
    assert eq["share_by_status"][P.INSUFFICIENT_EVIDENCE] == 0.5


def test_every_status_carries_where_it_was_measured():
    """A status with no pointer is an assertion. Every claim about Gaffer's own
    reliability must name the thing that measured it, or say plainly that
    nothing did."""
    for name, meta in P.COMPONENT_EVIDENCE.items():
        assert meta["evidence"].strip(), name
        assert meta["where"].strip(), name
        if meta["status"] == P.SUPPORTED:
            assert meta["where"] != "-", f"{name} claims support with no source"


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------

def test_a_wholly_supported_projection_reports_no_weak_share():
    eq = P.evidence_quality({"appearance": 2.0, "goals": 3.0, "assists": 1.0})
    assert eq["weak_evidence_share"] == 0.0
    assert eq["largest_weak_component"] is None


def test_shares_are_taken_by_magnitude_not_by_sign():
    """The `other` term (cards, own goals) is routinely negative. A term that
    subtracts a point is as much of the answer as one that adds it, and
    signing it would let a projection hide its worst component."""
    eq = P.evidence_quality({"appearance": 2.0, "other": -2.0})
    assert eq["weak_evidence_share"] == 0.5


def test_the_largest_weak_component_is_named_with_its_evidence():
    eq = P.evidence_quality(
        {"appearance": 2.0, "clean_sheet": 1.5, "bonus": 0.5})
    biggest = eq["largest_weak_component"]
    assert biggest["component"] == "clean_sheet"
    assert biggest["share"] == 0.375
    assert "0.1899" in biggest["evidence"]


def test_an_unknown_component_is_flagged_not_silently_trusted():
    """A component nobody classified is not evidence of anything. Counting it
    as supported would let a new term enter the model and inherit a clean bill
    of health it never earned."""
    eq = P.evidence_quality({"appearance": 1.0, "vibes": 1.0})
    assert eq["unrecognised_components"] == ["vibes"]
    assert eq["weak_evidence_share"] == 0.5


@pytest.mark.parametrize("bad", [None, {}, "no", 7, {"appearance": 0.0}])
def test_absence_is_stated_rather_than_reported_as_zero(bad):
    """An unmeasured projection is not a well-evidenced one. Returning 0.0 for
    a missing breakdown would read as "nothing weak here", which is exactly
    backwards."""
    eq = P.evidence_quality(bad)
    assert eq["available"] is False
    assert eq["reason"]
    assert "weak_evidence_share" not in eq


def test_the_policy_line_admits_the_choice_is_a_choice():
    """Which statuses count as weak is a declared policy, like the blend
    weight and the minimum-actionable thresholds. Nothing fitted it, and the
    published object has to say so where it is read."""
    eq = P.evidence_quality({"appearance": 1.0})
    assert "POLICY" in eq["policy"].upper()
    assert "not a fitted" in eq["policy"]
