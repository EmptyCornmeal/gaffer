"""The headline names the horizon it is quoting.

Found live on 2026-09-02, on the published site, while verifying Phase 6 in a
browser. The recommendation read:

    "+11.1 points over holding, ahead in 56% of scenarios"

and the panel directly beneath it said the gain was +1.9. Both numbers were
right. The sentence was not: 11.12 is the six-gameweek edge, 1.95 is the
one-week edge, and 56% is the probability of the ONE-WEEK one. A single
sentence borrowed the larger number's size and the smaller number's
probability, and named neither timescale.

That is a Scope violation (0.3) in the single most-read sentence Gaffer
publishes -- and every other branch of `classify` already said "this gameweek"
or "the longer-term plan". This one was the exception.
"""
from __future__ import annotations

from gaffer import decision as D


class _Cmp:
    """The live GW3 comparison that exposed it."""
    delta = 1.95
    horizon_delta = 11.12
    p_move_beats_hold = 0.56
    hit_cost = 4
    delta_ci95 = (1.0, 3.0)
    n_sims = 2000
    short_term_delta = 1.95
    move_expected = 50.0
    hold_expected = 48.0
    delta_p10 = -6.0
    delta_p90 = 10.0


def _cmp(**over):
    c = _Cmp()
    for k, v in over.items():
        setattr(c, k, v)
    return c


def test_a_horizon_number_says_it_is_a_horizon_number():
    action, reason = D.classify(_cmp())
    assert action == D.ACTION_TRANSFER
    assert "across the planning horizon" in reason
    # ...and the one-week figure is present, so the panel below cannot appear
    # to contradict the sentence above it.
    assert "+1.9 next gameweek" in reason


def test_the_probability_says_which_number_it_belongs_to():
    """56% is the chance the move wins NEXT GAMEWEEK. Attached to a six-week
    total with no qualifier, it reads as the chance the six-week total is
    achieved, which nothing measured."""
    _, reason = D.classify(_cmp())
    assert "56% of next-gameweek scenarios" in reason


def test_a_one_week_number_is_not_dressed_up_as_a_horizon_one():
    _, reason = D.classify(_cmp(horizon_delta=1.0))
    assert "+1.9 points over holding next gameweek" in reason
    assert "planning horizon" not in reason


def test_no_transfer_headline_quotes_a_bare_number():
    """The property, rather than the two phrasings: whatever branch fires, the
    sentence must say which timescale its points figure belongs to."""
    for horizon in (None, -3.0, 1.0, 11.12, 40.0):
        action, reason = D.classify(_cmp(horizon_delta=horizon))
        if action != D.ACTION_TRANSFER:
            continue
        assert ("next gameweek" in reason or "this gameweek" in reason
                or "planning horizon" in reason), reason


def test_the_other_branches_still_name_their_timescale():
    """They always did -- this asserts the fix did not cost that."""
    _, roll = D.classify(_cmp(delta=-4.6, p_move_beats_hold=0.29,
                              horizon_delta=16.7))
    assert "gameweek" in roll or "longer-term" in roll
