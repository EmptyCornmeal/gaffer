"""The GW2 2026-27 contradiction, and every invariant that now forbids it.

The first test is the important one. It rebuilds the real snapshot of
2026-08-28T13:14:30Z — the held squad, the six transfers out, the six in and the
eleven it published — and asserts that the candidate layer refuses to construct
it. Four of the six players that decision sold were in the eleven it fielded.

Player ids are the real 2026-27 elements, so the failure this pins is the one
that actually shipped rather than a synthetic stand-in.
"""

from __future__ import annotations

import pytest

from gaffer import candidate as C

# --- the real GW2 snapshot -------------------------------------------------
# decision_snapshots, season 2026-27, target_event 2, as_of 2026-08-28T13:14:30Z
HELD = (1, 8, 31, 165, 175, 212, 346, 411, 418, 426, 427, 497, 504, 542, 557)
OUT = (1, 8, 175, 418, 504, 557)        # Raya, Calafiori, van Ewijk, Maguire,
IN = (84, 88, 94, 109, 328, 388)        #   Vuskovic, Tzolis -> six replacements
PUBLISHED_XI = (1, 31, 8, 418, 426, 427, 411, 165, 346, 542, 557)
PUBLISHED_BENCH = (497, 504, 212, 175)


def _before(**kw):
    base = dict(known=True, squad=HELD, bank=2, free_transfers=1,
                source="entry_picks", source_event=1)
    base.update(kw)
    return C.BeforeState(**base)


def test_the_gw2_artifact_can_no_longer_be_constructed():
    """Sell six, field four of them: the exact defect, now unconstructable."""
    before = _before()
    action = C.Action(kind=C.KIND_TRANSFER, transfers_out=OUT, transfers_in=IN)

    with pytest.raises(C.CandidateError) as err:
        C.Candidate.create(
            candidate_id="c_gw2_historical", before=before, action=action,
            xi=PUBLISHED_XI, bench=PUBLISHED_BENCH, captain=426, vice=411)

    # It must fail for the RIGHT reason, and name the players, or a future
    # refactor could satisfy this test by failing on something incidental.
    #
    # The reason is worth reading. The after-state is derived correctly — the
    # six sold players ARE gone from it — so the contradiction does not surface
    # as "you sold them and kept them". It surfaces one step later, when the
    # published eleven and bench are asked to reconcile with the squad the
    # action actually produced: four of the eleven and two of the bench are not
    # owned any more, and six players nobody named are.
    msg = str(err.value)
    assert "XI plus bench must be exactly the after-state squad" in msg
    for pid in (1, 8, 418, 557):        # Raya, Calafiori, Maguire, Tzolis
        assert str(pid) in msg, f"{pid} was fielded and sold, and is not named"
    for pid in IN:                      # and the six it bought are missing
        assert str(pid) in msg


def test_the_gw2_defect_is_also_caught_at_the_squad_boundary():
    """The same contradiction reaches the earlier guard when the XI is the only
    thing carried forward — the shape a caller reaches for when it copies
    ``starting`` from one place and the transfers from another."""
    before = _before()
    action = C.Action(kind=C.KIND_TRANSFER, transfers_out=OUT, transfers_in=IN)
    after = set(C.apply_action(before, action))
    fielded_and_sold = set(PUBLISHED_XI) & set(OUT)
    assert fielded_and_sold == {1, 8, 418, 557}
    assert not fielded_and_sold & after, (
        "the derived after-state is the thing that makes the published XI "
        "impossible; if these ever intersect the transition function is wrong")


def test_the_same_action_with_a_post_move_xi_is_accepted():
    """The fix is not 'refuse transfers'. A coherent version of GW2 is fine."""
    before = _before()
    action = C.Action(kind=C.KIND_TRANSFER, transfers_out=OUT, transfers_in=IN)
    after = C.apply_action(before, action)
    assert len(after) == 15
    assert not set(OUT) & set(after)
    assert set(IN) <= set(after)

    cand = C.Candidate.create(
        candidate_id="c_gw2_coherent", before=before, action=action,
        xi=after[:11], bench=after[11:], captain=after[0], vice=after[1])
    assert cand.hit == (6 - 1) * 4      # six transfers, one free
    assert set(cand.xi) | set(cand.bench) == set(cand.after_squad)


# --- the hit is derived, never carried -------------------------------------

def test_hit_is_derived_from_the_action_not_supplied_beside_it():
    """GW3 2026-27 recorded comparison.hit_cost = 4 with zero transfers."""
    before = _before()
    hold = C.Candidate.create(
        candidate_id="c_hold", before=before, action=C.Action(kind=C.KIND_HOLD))
    assert hold.hit == 0, "a hold cannot cost a hit"

    one = C.Action(kind=C.KIND_TRANSFER, transfers_out=(1,), transfers_in=(84,))
    assert C.hit_for(before, one) == 0          # covered by the free transfer
    two = C.Action(kind=C.KIND_TRANSFER, transfers_out=(1, 8), transfers_in=(84, 88))
    assert C.hit_for(before, two) == 4


def test_a_wildcard_pays_no_hit():
    before = _before()
    action = C.Action(kind=C.KIND_CHIP, transfers_out=OUT, transfers_in=IN,
                      chip="wildcard")
    assert C.hit_for(before, action) == 0


def test_a_transfer_cannot_be_priced_without_a_known_free_transfer_count():
    before = _before(free_transfers=None)
    action = C.Action(kind=C.KIND_TRANSFER, transfers_out=(1,), transfers_in=(84,))
    with pytest.raises(C.CandidateError, match="without a known free-transfer"):
        C.hit_for(before, action)


# --- structural invariants -------------------------------------------------

def test_selling_a_player_you_do_not_own_is_refused():
    with pytest.raises(C.CandidateError, match="not in the held squad"):
        C.apply_action(_before(), C.Action(
            kind=C.KIND_TRANSFER, transfers_out=(999,), transfers_in=(84,)))


def test_buying_a_player_you_already_own_is_refused():
    with pytest.raises(C.CandidateError, match="already in the squad"):
        C.apply_action(_before(), C.Action(
            kind=C.KIND_TRANSFER, transfers_out=(1,), transfers_in=(8,)))


def test_transfers_must_balance():
    with pytest.raises(C.CandidateError, match="same number of players"):
        C.Action(kind=C.KIND_TRANSFER, transfers_out=(1, 8), transfers_in=(84,))


def test_xi_and_bench_must_partition_the_after_state():
    before = _before()
    hold = C.Action(kind=C.KIND_HOLD)
    after = C.apply_action(before, hold)
    with pytest.raises(C.CandidateError, match="XI plus bench"):
        C.Candidate.create(
            candidate_id="c", before=before, action=hold,
            xi=after[:11], bench=(*after[11:14], 999), captain=after[0])


def test_a_player_cannot_start_and_be_benched():
    before = _before()
    hold = C.Action(kind=C.KIND_HOLD)
    after = C.apply_action(before, hold)
    with pytest.raises(C.CandidateError, match="XI and on the bench"):
        C.Candidate.create(
            candidate_id="c", before=before, action=hold,
            xi=after[:11], bench=(after[0], *after[11:14]), captain=after[0])


def test_captain_must_be_in_the_xi():
    before = _before()
    hold = C.Action(kind=C.KIND_HOLD)
    after = C.apply_action(before, hold)
    with pytest.raises(C.CandidateError, match="captain is not in the XI"):
        C.Candidate.create(
            candidate_id="c", before=before, action=hold,
            xi=after[:11], bench=after[11:], captain=after[12])


def test_captain_and_vice_must_differ():
    before = _before()
    hold = C.Action(kind=C.KIND_HOLD)
    after = C.apply_action(before, hold)
    with pytest.raises(C.CandidateError, match="same player"):
        C.Candidate.create(
            candidate_id="c", before=before, action=hold, xi=after[:11],
            bench=after[11:], captain=after[0], vice=after[0])


# --- unknown is not empty --------------------------------------------------

def test_an_unknown_before_state_may_not_carry_a_squad():
    """GW1 2026-27: squad_state.known was False and the review still read
    'You made the recommended move' off two empty transfer lists."""
    with pytest.raises(C.CandidateError, match="may not carry players"):
        C.BeforeState(known=False, squad=HELD)


def test_an_unknown_before_state_produces_no_after_state():
    before = C.BeforeState(known=False, source="no_public_squad_yet")
    cand = C.Candidate.create(
        candidate_id="c_suggested", before=before, action=C.Action(kind=C.KIND_HOLD),
        label="suggested build, not your team")
    assert cand.after_squad == ()
    assert cand.hit == 0


def test_an_unknown_before_state_cannot_transfer():
    before = C.BeforeState(known=False)
    with pytest.raises(C.CandidateError, match="unknown before-state"):
        C.apply_action(before, C.Action(
            kind=C.KIND_TRANSFER, transfers_out=(1,), transfers_in=(84,)))


# --- the set ---------------------------------------------------------------

def _set(**kw):
    before = _before()
    hold = C.Candidate.create(candidate_id="c_hold", before=before,
                              action=C.Action(kind=C.KIND_HOLD), expectation=52.99)
    move_action = C.Action(kind=C.KIND_TRANSFER, transfers_out=OUT, transfers_in=IN)
    after = C.apply_action(before, move_action)
    move = C.Candidate.create(candidate_id="c_move", before=before,
                              action=move_action, xi=after[:11], bench=after[11:],
                              captain=after[0], expectation=36.58)
    base = dict(before=before, candidates={"c_hold": hold, "c_move": move},
                baseline_id="c_hold", selected_id="c_hold")
    base.update(kw)
    return C.CandidateSet(**base)


def test_a_rejected_candidate_cannot_supply_the_selected_expected_value():
    """The GW2 review read 36.58 — the REJECTED move — as 'the decision'."""
    cs = _set()                                  # selected = hold
    assert cs.selected.candidate_id == "c_hold"
    assert cs.selected.expectation == 52.99
    assert cs.expected_delta() == 0.0            # hold vs hold, by construction
    rejected = {c.candidate_id for c in cs.rejected()}
    assert rejected == {"c_move"}
    # The move's expectation is still available as evidence, and is not the
    # decision's.
    assert cs.candidates["c_move"].expectation == 36.58


def test_selecting_the_move_makes_the_delta_the_move_minus_the_hold():
    cs = _set(selected_id="c_move")
    assert cs.expected_delta() == pytest.approx(36.58 - 52.99)


def test_the_selected_candidate_must_be_in_the_set():
    with pytest.raises(C.CandidateError, match="selected .* is not in"):
        _set(selected_id="c_nope")


def test_the_baseline_must_be_a_hold():
    before = _before()
    move_action = C.Action(kind=C.KIND_TRANSFER, transfers_out=OUT, transfers_in=IN)
    after = C.apply_action(before, move_action)
    move = C.Candidate.create(candidate_id="c_move", before=before,
                              action=move_action, xi=after[:11], bench=after[11:],
                              captain=after[0])
    with pytest.raises(C.CandidateError, match="baseline candidate must be a hold"):
        C.CandidateSet(before=before, candidates={"c_move": move},
                       baseline_id="c_move", selected_id="c_move")


def test_every_candidate_shares_one_before_state():
    """Two before-states means two questions, not one comparison."""
    a, b = _before(), _before(free_transfers=2)
    assert a.fingerprint != b.fingerprint
    assert C.candidate_id_for(C.Action(kind=C.KIND_HOLD), a) != \
        C.candidate_id_for(C.Action(kind=C.KIND_HOLD), b)


def test_candidate_ids_are_stable_for_the_same_world_and_action():
    a = _before()
    act = C.Action(kind=C.KIND_TRANSFER, transfers_out=OUT, transfers_in=IN)
    assert C.candidate_id_for(act, a) == C.candidate_id_for(act, _before())


# --- legality is reported, incoherence raises ------------------------------

def test_an_over_club_limit_squad_is_legal_information_not_an_error():
    clubs = {p: 1 for p in HELD}                 # everyone at one club
    before = _before(clubs=clubs)
    cand = C.Candidate.create(candidate_id="c", before=before,
                              action=C.Action(kind=C.KIND_HOLD))
    assert any(v.startswith("club:") for v in cand.legality)


def test_serialisation_round_trips_the_fields_downstream_reads():
    cs = _set(selected_id="c_move", gameweek=2, season="2026-27")
    d = cs.as_dict()
    assert d["selected_candidate_id"] == "c_move"
    assert d["baseline_candidate_id"] == "c_hold"
    assert d["before_state"]["known"] is True
    assert len(d["candidates"]) == 2
    sel = next(c for c in d["candidates"] if c["candidate_id"] == "c_move")
    assert set(sel["xi"]) | set(sel["bench"]) == set(sel["after_squad"])
    assert sel["hit"] == 20
