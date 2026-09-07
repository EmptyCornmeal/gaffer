"""What a frozen belief has to guarantee before anything may be compared to it.

The contract these pin is narrow and load-bearing: two candidates scored from
one frozen object index the same players and the same scenario columns, and no
path exists that draws a second matrix. Everything else here — hits, the
armband, chips, immutability, refusal — protects that one property from being
true in principle and false in a particular call.

Synthetic fixtures throughout, small enough to reason about by hand. The real
matrix is exercised by the dry run, not by the suite: a test that needs a live
database is a test that stops running.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pytest

from gaffer import candidate as CAND
from gaffer import evidence as EV

# --- a hand-made world -----------------------------------------------------
# 20 players, 8 scenarios. Ids 1..20; positions give a legal 15.
N_SIMS = 8
PIDS = tuple(range(1, 21))
POS = {}
for _p in PIDS:
    POS[_p] = ("GKP" if _p <= 2 else "DEF" if _p <= 7
               else "MID" if _p <= 12 else "FWD")
SQUAD = tuple(range(1, 16))              # 2 GKP, 5 DEF, 5 MID, 3 FWD
XI = (1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15)      # 1-4-4-3... GKP+3DEF+4MID+3FWD
BENCH = (2, 6, 7, 12)                     # bench GK first, then outfield


def _matrix(seed: int = 0):
    rng = np.random.default_rng(seed)
    pts = rng.integers(0, 12, size=(len(PIDS), N_SIMS)).astype(np.int16)
    app = rng.random((len(PIDS), N_SIMS)) > 0.2
    return pts, app


def _before(**kw) -> CAND.BeforeState:
    base = dict(known=True, squad=SQUAD, bank=0, free_transfers=1,
                source="test", source_event=1)
    base.update(kw)
    return CAND.BeforeState(**base)


def _evidence(points=None, appeared=None, before=None, cands=None,
              selected=None, baseline=None, gameweek=4) -> EV.FrozenEvidence:
    pts, app = _matrix() if points is None else (points, appeared)
    before = before or _before()
    if cands is None:
        hold = CAND.Candidate.create(
            candidate_id="c_hold", before=before,
            action=CAND.Action(kind=CAND.KIND_HOLD), xi=XI, bench=BENCH,
            captain=13, vice=14, label="Hold")
        cands = {"c_hold": hold}
        selected = baseline = "c_hold"
    cset = CAND.CandidateSet(before=before, candidates=cands,
                             baseline_id=baseline, selected_id=selected,
                             gameweek=gameweek, season="2026-27")

    class _Scen:
        pass
    s = _Scen()
    s.points = pts.astype(np.float32)
    s.appeared = app
    s.player_ids = list(PIDS)
    s.n_sims = N_SIMS
    s.seed = 4242
    s.meta = {"sim_version": "scenarios-1.1"}
    s.diagnostics = {"clean_sheet_contradiction": {"mean": 0.0555}}
    return EV.freeze(
        s, cset, season="2026-27", gameweek=gameweek,
        deadline="2026-09-12T12:30:00+00:00",
        information_cutoff="2026-09-12T06:30:00+00:00",
        positions=POS, model_version="heuristic-0.6",
        rules_version="fpl-2026-27",
        limitations=("appearance gating is incomplete",))


def _cand(before, *, cid="c", out=(), inn=(), chip=None, xi=XI, bench=BENCH,
          captain=13, vice=14, kind=None):
    kind = kind or (CAND.KIND_CHIP if chip else
                    CAND.KIND_TRANSFER if inn else CAND.KIND_HOLD)
    return CAND.Candidate.create(
        candidate_id=cid, before=before,
        action=CAND.Action(kind=kind, transfers_out=out, transfers_in=inn,
                           chip=chip),
        xi=xi, bench=bench, captain=captain, vice=vice)


# --- the same-world guarantee ---------------------------------------------

def test_two_candidates_index_the_same_players_and_scenarios():
    """The property everything else exists to protect."""
    ev = _evidence()
    before = _before()
    a = _cand(before, cid="a", captain=13, vice=14)
    b = _cand(before, cid="b", captain=14, vice=13)
    sa, sb = ev.score(a), ev.score(b)
    assert sa.shape == sb.shape == (N_SIMS,)

    # The only difference between them is the armband, so the difference must
    # be exactly the two captains' points where each played. If the two calls
    # had drawn different football this identity would not hold.
    i13, i14 = ev.index[13], ev.index[14]
    cap_a = np.where(ev.appeared[i13], ev.points[i13], 0.0)
    cap_b = np.where(ev.appeared[i14], ev.points[i14], 0.0)
    # vice fallback: a falls back to 14 when 13 is absent, and vice versa
    cap_a = np.where(~ev.appeared[i13] & ev.appeared[i14], ev.points[i14], cap_a)
    cap_b = np.where(~ev.appeared[i14] & ev.appeared[i13], ev.points[i13], cap_b)
    assert np.allclose(sb - sa, cap_b - cap_a)


def test_scoring_never_redraws():
    """Repeated scoring is deterministic; nothing samples at score time."""
    ev = _evidence()
    c = _cand(_before())
    first = ev.score(c)
    for _ in range(5):
        assert np.array_equal(ev.score(c), first)


def test_identical_candidates_differ_by_exactly_zero():
    ev = _evidence()
    before = _before()
    cmp_ = ev.compare(_cand(before, cid="a"), _cand(before, cid="b"))
    assert cmp_["model_relative_delta"] == 0.0
    assert cmp_["p_a_beats_b"] == 0.0 and cmp_["p_b_beats_a"] == 0.0
    assert cmp_["p_tie"] == 1.0


# --- reload ----------------------------------------------------------------

def test_persist_and_reload_is_exact(tmp_path):
    ev = _evidence()
    p = ev.save(tmp_path / "gw04.ndjson")
    back = EV.FrozenEvidence.load(p)
    assert np.array_equal(back.points, ev.points)
    assert np.array_equal(back.appeared, ev.appeared)
    assert back.player_ids == ev.player_ids
    assert back.content_digest == ev.content_digest
    assert back.evidence_id == ev.evidence_id
    assert back.scenario_set_id == ev.scenario_set_id
    assert dict(back.positions) == dict(ev.positions)


def test_reloaded_evidence_scores_identically(tmp_path):
    ev = _evidence()
    back = EV.FrozenEvidence.load(ev.save(tmp_path / "gw04.ndjson"))
    c = _cand(_before())
    assert np.array_equal(ev.score(c), back.score(c))


def test_an_edited_file_is_refused(tmp_path):
    """A digest that does not match is not a warning."""
    ev = _evidence()
    p = ev.save(tmp_path / "gw04.ndjson")
    lines = p.read_text().splitlines()
    row = json.loads(lines[1])
    vals = row["s"].split()
    vals[0] = str(int(vals[0]) + 1)          # one point, in one scenario
    row["s"] = " ".join(vals)
    lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(EV.EvidenceError, match="content digest"):
        EV.FrozenEvidence.load(p)


def test_a_truncated_matrix_is_refused(tmp_path):
    ev = _evidence()
    p = ev.save(tmp_path / "gw04.ndjson")
    lines = p.read_text().splitlines()
    row = json.loads(lines[1])
    row["s"] = " ".join(row["s"].split()[:-1])
    lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(EV.EvidenceError, match="expected 8 of each"):
        EV.FrozenEvidence.load(p)


# --- immutability ----------------------------------------------------------

def test_a_second_write_to_one_path_is_refused(tmp_path):
    ev = _evidence()
    p = ev.save(tmp_path / "gw04.ndjson")
    with pytest.raises(EV.ImmutableError, match="written once"):
        _evidence().save(p)
    # and the original survives untouched
    assert EV.FrozenEvidence.load(p).content_digest == ev.content_digest


def test_a_different_draw_gets_a_different_identity():
    """Two beliefs are two objects, never one object that changed."""
    a = _evidence()
    b = _evidence(*_matrix(seed=99))
    assert a.content_digest != b.content_digest
    assert a.evidence_id != b.evidence_id
    assert a.scenario_set_id != b.scenario_set_id


def test_scenario_identity_is_content_not_seed():
    """`seed:n_sims` was the same string every gameweek. This cannot be."""
    a = _evidence(gameweek=4)
    b = _evidence(*_matrix(seed=7), gameweek=4)
    assert a.scenario_seed == b.scenario_seed        # same recipe
    assert a.scenario_set_id != b.scenario_set_id    # different football


# --- candidate identity ----------------------------------------------------

def test_model_and_human_candidates_share_one_before_state_and_one_matrix():
    before = _before()
    model = _cand(before, cid="c_model", captain=13, vice=14)
    human = _cand(before, cid="c_human", captain=14, vice=13)
    ev = _evidence(before=before,
                   cands={"c_model": model}, selected="c_model",
                   baseline="c_model")
    assert ev.before_state_fingerprint == before.fingerprint
    cmp_ = ev.compare(model, human)
    assert cmp_["scenario_set_id"] == ev.scenario_set_id
    assert cmp_["evidence_id"] == ev.evidence_id


def test_a_candidate_from_a_different_before_state_is_a_different_world():
    a, b = _before(), _before(free_transfers=2)
    assert a.fingerprint != b.fingerprint


# --- hits ------------------------------------------------------------------

def test_the_transfer_hit_is_subtracted_exactly_once():
    before = _before()                       # one free transfer
    ev = _evidence(before=before)
    hold = _cand(before, cid="h")
    # two transfers, one free -> one hit
    move = _cand(before, cid="m", out=(15, 14), inn=(16, 17),
                 xi=(1, 3, 4, 5, 8, 9, 10, 11, 13, 16, 17), bench=(2, 6, 7, 12),
                 captain=13, vice=16)
    assert move.hit == 4
    gross = ev.as_scenario_set().points_with_autosubs(
        list(move.xi), list(move.bench), dict(ev.positions), captain=None)
    gross = np.asarray(gross) + ev._armband(move, 2)
    assert np.allclose(ev.score(move), gross - 4)
    # and the hold, which pays none
    assert hold.hit == 0


def test_a_chip_that_replaces_the_transfer_economy_pays_no_hit():
    before = _before()
    wc = _cand(before, cid="wc", out=(15, 14), inn=(16, 17), chip="wildcard",
               xi=(1, 3, 4, 5, 8, 9, 10, 11, 13, 16, 17), bench=(2, 6, 7, 12),
               captain=13, vice=16)
    assert wc.hit == 0


# --- captain and chips -----------------------------------------------------

def test_the_armband_moves_to_the_vice_only_when_the_captain_did_not_appear():
    pts, app = _matrix()
    app[:] = True
    app[12] = False                      # player 13 (index 12) never appears
    ev = _evidence(pts, app)
    c = _cand(_before(), captain=13, vice=14)
    i13, i14 = ev.index[13], ev.index[14]
    extra = ev._armband(c, 2)
    assert np.array_equal(extra, ev.points[i14].astype(np.float32))
    assert not np.array_equal(extra, ev.points[i13].astype(np.float32))


def test_the_armband_evaporates_when_neither_captain_nor_vice_appears():
    pts, app = _matrix()
    app[:] = True
    app[12] = False
    app[13] = False
    ev = _evidence(pts, app)
    assert np.all(ev._armband(_cand(_before(), captain=13, vice=14), 2) == 0)


def test_triple_captain_triples_the_armband_and_nothing_else():
    pts, app = _matrix()
    app[:] = True
    ev = _evidence(pts, app)
    before = _before()
    plain = _cand(before, cid="p")
    tc = _cand(before, cid="t", chip="3xc")
    i = ev.index[13]
    # x3 adds one further copy of the captain over x2, and no other change.
    assert np.allclose(ev.score(tc) - ev.score(plain),
                       ev.points[i].astype(np.float32))


def test_bench_boost_adds_the_bench_and_keeps_the_armband_at_two():
    pts, app = _matrix()
    app[:] = True
    ev = _evidence(pts, app)
    before = _before()
    plain = _cand(before, cid="p")
    bb = _cand(before, cid="b", chip="bboost")
    bench_pts = sum(ev.points[ev.index[p]].astype(np.float32) for p in BENCH)
    assert np.allclose(ev.score(bb) - ev.score(plain), bench_pts)


# --- refusal ---------------------------------------------------------------

def test_a_candidate_with_no_xi_is_refused_rather_than_scored_as_zero():
    ev = _evidence()
    bare = CAND.Candidate.create(
        candidate_id="bare", before=_before(),
        action=CAND.Action(kind=CAND.KIND_HOLD))
    with pytest.raises(EV.InsufficientState, match="no XI"):
        ev.score(bare)


def test_a_player_absent_from_the_evidence_is_refused():
    ev = _evidence()
    before = _before(squad=tuple(range(1, 15)) + (99,))
    c = _cand(before, cid="x",
              xi=(1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 99), bench=BENCH,
              captain=13, vice=14)
    with pytest.raises(EV.InsufficientState, match="not in this evidence"):
        ev.score(c)


def test_freezing_refuses_fractional_points():
    pts, app = _matrix()

    class _S:
        points = pts.astype(np.float32) + 0.5
        appeared = app
        player_ids = list(PIDS)
        n_sims = N_SIMS
        seed = 1
        meta: dict = {}
        diagnostics: dict = {}
    hold = CAND.Candidate.create(
        candidate_id="c", before=_before(),
        action=CAND.Action(kind=CAND.KIND_HOLD), xi=XI, bench=BENCH, captain=13)
    cs = CAND.CandidateSet(before=_before(), candidates={"c": hold},
                           baseline_id="c", selected_id="c")
    with pytest.raises(EV.EvidenceError, match="not whole numbers"):
        EV.freeze(_S(), cs, season="2026-27", gameweek=4, deadline="x",
                  information_cutoff="y", positions=POS,
                  model_version="m", rules_version="r")


def test_freezing_refuses_a_set_with_no_appearance_mask():
    pts, _ = _matrix()

    class _S:
        points = pts.astype(np.float32)
        appeared = None
        player_ids = list(PIDS)
        n_sims = N_SIMS
        seed = 1
        meta: dict = {}
        diagnostics: dict = {}
    hold = CAND.Candidate.create(
        candidate_id="c", before=_before(),
        action=CAND.Action(kind=CAND.KIND_HOLD), xi=XI, bench=BENCH, captain=13)
    cs = CAND.CandidateSet(before=_before(), candidates={"c": hold},
                           baseline_id="c", selected_id="c")
    with pytest.raises(EV.EvidenceError, match="no appearance mask"):
        EV.freeze(_S(), cs, season="2026-27", gameweek=4, deadline="x",
                  information_cutoff="y", positions=POS,
                  model_version="m", rules_version="r")


# --- reconstruction from FPL's own picks -----------------------------------

def _picks(elements, captain, vice, chip=None, cost=0):
    rows = []
    for i, e in enumerate(elements, start=1):
        rows.append({"element": e, "position": i,
                     "is_captain": e == captain,
                     "is_vice_captain": e == vice,
                     "multiplier": 0 if i > 11 else (2 if e == captain else 1)})
    return {"picks": rows, "active_chip": chip,
            "entry_history": {"event_transfers_cost": cost}}


def test_an_action_is_reconstructed_from_official_picks():
    before = _before()
    after = list(XI) + list(BENCH)
    after[after.index(15)] = 16                  # sold 15, bought 16
    p = _picks(after, captain=13, vice=14)
    c = EV.action_from_picks(before, p, free_transfers=1)
    assert c.action.transfers_out == (15,)
    assert c.action.transfers_in == (16,)
    assert c.hit == 0
    assert set(c.xi) | set(c.bench) == set(c.after_squad)
    assert c.captain == 13 and c.vice == 14
    assert c.bench == BENCH                      # order preserved


def test_a_reconstructed_action_scores_against_the_frozen_model_unchanged():
    """FPL choices do not change the football, so this is legitimate."""
    before = _before()
    ev = _evidence(before=before)
    digest_before = ev.content_digest
    p = _picks(list(XI) + list(BENCH), captain=13, vice=14)
    human = EV.action_from_picks(before, p, free_transfers=1)
    s = ev.score(human)
    assert s.shape == (N_SIMS,)
    assert ev.content_digest == digest_before, "scoring mutated the evidence"


def test_reconstruction_refuses_when_the_before_state_is_unknown():
    before = CAND.BeforeState(known=False)
    p = _picks(list(XI) + list(BENCH), captain=13, vice=14)
    with pytest.raises(EV.InsufficientState, match="not reconstructable"):
        EV.action_from_picks(before, p)


def test_reconstruction_refuses_picks_that_are_not_one_gameweek_apart():
    """Fifteen changes is a legitimate wildcard and an illegitimate two-week
    gap. Only the published cost tells them apart, so that is what is checked;
    counting transfers cannot, because both squads are always fifteen."""
    before = _before()                            # one free transfer
    after = [90 + i for i in range(15)]           # every player different
    p = _picks(after, captain=90, vice=91, cost=0)
    with pytest.raises(EV.InsufficientState, match="not one gameweek apart"):
        EV.action_from_picks(before, p, free_transfers=1)


def test_fifteen_transfers_are_accepted_when_a_wildcard_explains_them():
    """The same squad diff, with a chip and a zero cost, is legitimate."""
    before = _before()
    after = [90 + i for i in range(15)]
    p = _picks(after, captain=90, vice=91, chip="wildcard", cost=0)
    c = EV.action_from_picks(before, p, free_transfers=1)
    assert c.action.chip == "wildcard"
    assert c.action.n_transfers == 15 and c.hit == 0


def test_reconstruction_refuses_when_the_published_cost_disagrees():
    """A hit that does not reconcile means the before-state is the wrong week."""
    before = _before()
    after = list(XI) + list(BENCH)
    after[after.index(15)] = 16
    p = _picks(after, captain=13, vice=14, cost=8)   # FPL says this cost 8
    with pytest.raises(EV.InsufficientState, match="cost 8 points"):
        EV.action_from_picks(before, p, free_transfers=1)


def test_provenance_is_explicit_and_checked():
    before = _before()
    c = _cand(before)
    rec = EV.RecordedAction(
        season="2026-27", gameweek=4, evidence_id="ev_x",
        provenance=EV.PROV_RECONSTRUCTED, recorded_at="2026-09-12T13:00:00Z",
        candidate=c)
    d = rec.as_dict()
    assert d["provenance"] == EV.PROV_RECONSTRUCTED
    assert "not evidence about what he knew" in d["provenance_means"]
    with pytest.raises(EV.EvidenceError, match="unknown provenance"):
        EV.RecordedAction(season="2026-27", gameweek=4, evidence_id="e",
                          provenance="guessed", recorded_at="x", candidate=c)


def test_recorded_actions_append_rather_than_overwrite(tmp_path):
    before = _before()
    c = _cand(before)
    for prov in (EV.PROV_PREDEADLINE, EV.PROV_RECONSTRUCTED):
        EV.record_action(EV.RecordedAction(
            season="2026-27", gameweek=4, evidence_id="ev_x", provenance=prov,
            recorded_at="2026-09-12T13:00:00Z", candidate=c), data_dir=tmp_path)
    rows = EV.actions_for("2026-27", 4, data_dir=tmp_path)
    assert [r["provenance"] for r in rows] == [EV.PROV_PREDEADLINE,
                                               EV.PROV_RECONSTRUCTED]
    assert EV.actions_for("2026-27", 5, data_dir=tmp_path) == []


def test_a_stored_action_round_trips_back_into_a_validated_candidate(tmp_path):
    before = _before()
    c = _cand(before, cid="c_actual", out=(15,), inn=(16,),
              xi=(1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 16), bench=BENCH,
              captain=13, vice=14)
    EV.record_action(EV.RecordedAction(
        season="2026-27", gameweek=4, evidence_id="ev_x",
        provenance=EV.PROV_PREDEADLINE, recorded_at="t", candidate=c),
        data_dir=tmp_path)
    rec = EV.actions_for("2026-27", 4, data_dir=tmp_path)[0]
    back = EV.candidate_from_record(rec, before)
    assert back.after_squad == c.after_squad
    assert back.hit == c.hit and back.xi == c.xi and back.captain == c.captain


# --- the freeze rule -------------------------------------------------------

def _now(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


def test_one_freeze_per_gameweek_the_first_run_inside_the_window(tmp_path):
    dl = "2026-09-12T12:30:00+00:00"
    ok, why = EV.should_freeze(_now("2026-09-12T08:00:00"), dl, "2026-27", 4,
                               data_dir=tmp_path)
    assert ok and "inside the freeze window" in why

    _evidence().save(EV.path_for("2026-27", 4, tmp_path))
    ok, why = EV.should_freeze(_now("2026-09-12T09:00:00"), dl, "2026-27", 4,
                               data_dir=tmp_path)
    assert not ok and why == "already frozen for this gameweek"


def test_a_run_far_from_the_deadline_does_not_freeze(tmp_path):
    ok, why = EV.should_freeze(_now("2026-09-07T03:00:00"),
                               "2026-09-12T12:30:00+00:00", "2026-27", 4,
                               data_dir=tmp_path)
    assert not ok and "outside the" in why


def test_nothing_freezes_after_the_deadline(tmp_path):
    ok, why = EV.should_freeze(_now("2026-09-12T13:00:00"),
                               "2026-09-12T12:30:00+00:00", "2026-27", 4,
                               data_dir=tmp_path)
    assert not ok and "deadline has passed" in why


def test_a_missing_deadline_freezes_nothing(tmp_path):
    ok, why = EV.should_freeze(_now("2026-09-12T08:00:00"), None, "2026-27", 4,
                               data_dir=tmp_path)
    assert not ok and "no deadline" in why


# --- what the freeze says about itself -------------------------------------

def test_known_limitations_travel_with_the_belief(tmp_path):
    ev = _evidence()
    assert ev.limitations
    assert ev.scenario_diagnostics["clean_sheet_contradiction"]["mean"] == 0.0555
    back = EV.FrozenEvidence.load(ev.save(tmp_path / "gw04.ndjson"))
    assert back.limitations == ev.limitations
    assert back.scenario_diagnostics == ev.scenario_diagnostics


def test_a_comparison_publishes_facts_and_refuses_a_verdict():
    ev = _evidence()
    before = _before()
    out = ev.compare(_cand(before, cid="a", captain=13, vice=14),
                     _cand(before, cid="b", captain=14, vice=13))
    for key in ("model_expected_a", "model_expected_b", "model_relative_delta",
                "p_a_beats_b", "p_b_beats_a", "delta_percentiles"):
        assert key in out
    assert "not_a_verdict" in out
    for banned in ("good_decision", "bad_decision", "verdict", "quality"):
        assert banned not in {k.lower() for k in out}


# --- the pipeline seam -----------------------------------------------------

def _payload(before, cands, selected, baseline):
    cset = CAND.CandidateSet(before=before, candidates=cands,
                             baseline_id=baseline, selected_id=selected,
                             gameweek=4, season="2026-27")
    return {"candidate_set": cset.as_dict()}


def test_a_stored_candidate_set_rebuilds_and_revalidates():
    """The pipeline holds the set as a dict by freeze time; rebuilding it
    re-checks every candidate rather than trusting the serialisation."""
    before = _before()
    hold = _cand(before, cid="c_hold")
    pay = _payload(before, {"c_hold": hold}, "c_hold", "c_hold")
    back = EV.candidate_set_from_payload(pay["candidate_set"])
    assert back.before.fingerprint == before.fingerprint
    assert back.selected.candidate_id == "c_hold"
    assert back.selected.after_squad == hold.after_squad


def test_a_tampered_before_state_is_refused_at_rebuild():
    before = _before()
    pay = _payload(before, {"c": _cand(before, cid="c")}, "c", "c")
    pay["candidate_set"]["before_state"]["bank"] = 999      # fingerprint stale
    with pytest.raises(EV.EvidenceError, match="own fingerprint"):
        EV.candidate_set_from_payload(pay["candidate_set"])


def test_the_freeze_seam_runs_end_to_end_from_a_stored_payload(tmp_path):
    """Exactly what `pipeline` does: decide, serialise, rebuild, freeze, save."""
    before = _before()
    hold = _cand(before, cid="c_hold")
    move = _cand(before, cid="c_move", out=(15,), inn=(16,),
                 xi=(1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 16), bench=BENCH,
                 captain=13, vice=14)
    pay = _payload(before, {"c_hold": hold, "c_move": move}, "c_hold", "c_hold")

    ok, why = EV.should_freeze(_now("2026-09-12T08:00:00"),
                               "2026-09-12T12:30:00+00:00", "2026-27", 4,
                               data_dir=tmp_path)
    assert ok, why
    cset = EV.candidate_set_from_payload(pay["candidate_set"])
    pts, app = _matrix()

    class _S:
        points = pts.astype(np.float32)
        appeared = app
        player_ids = list(PIDS)
        n_sims = N_SIMS
        seed = 4242
        meta = {"sim_version": "scenarios-1.1"}
        diagnostics: dict = {}
    ev = EV.freeze(_S(), cset, season="2026-27", gameweek=4,
                   deadline="2026-09-12T12:30:00+00:00",
                   information_cutoff="2026-09-12T08:00:00+00:00",
                   positions=POS, model_version="heuristic-0.6",
                   rules_version="fpl-2026-27",
                   limitations=EV.SCENARIO_LIMITATIONS)
    ev.save(EV.path_for("2026-27", 4, tmp_path))

    # the run that follows declines rather than rewriting
    ok2, why2 = EV.should_freeze(_now("2026-09-12T10:00:00"),
                                 "2026-09-12T12:30:00+00:00", "2026-27", 4,
                                 data_dir=tmp_path)
    assert not ok2 and why2 == "already frozen for this gameweek"

    back = EV.load_for("2026-27", 4, tmp_path)
    assert back.selected_candidate_id == "c_hold"
    assert set(back.candidate_ids) == {"c_hold", "c_move"}
    assert back.limitations == EV.SCENARIO_LIMITATIONS
    # both candidates score against the one stored matrix
    assert back.compare(cset.selected, cset.candidates["c_move"])["n_sims"] == N_SIMS


def test_the_review_block_names_what_is_missing_rather_than_inventing_it(tmp_path):
    before = _before()
    pay = _payload(before, {"c": _cand(before, cid="c")}, "c", "c")

    out = EV.paired_comparison("2026-27", 4, pay, data_dir=tmp_path)
    assert out["available"] is False and "no frozen" in out["why"]

    _evidence(before=before, cands={"c": _cand(before, cid="c")},
              selected="c", baseline="c").save(
                  EV.path_for("2026-27", 4, tmp_path))
    out = EV.paired_comparison("2026-27", 4, pay, data_dir=tmp_path)
    assert out["available"] is False and "no action has been recorded" in out["why"]


def test_the_review_block_compares_the_selection_with_the_recorded_action(tmp_path):
    before = _before()
    model = _cand(before, cid="c_model", captain=13, vice=14)
    pay = _payload(before, {"c_model": model}, "c_model", "c_model")
    _evidence(before=before, cands={"c_model": model}, selected="c_model",
              baseline="c_model").save(EV.path_for("2026-27", 4, tmp_path))

    human = _cand(before, cid="c_human", captain=14, vice=13)
    EV.record_action(EV.RecordedAction(
        season="2026-27", gameweek=4, evidence_id="ev",
        provenance=EV.PROV_RECONSTRUCTED, recorded_at="2026-09-12T13:00:00Z",
        candidate=human), data_dir=tmp_path)

    out = EV.paired_comparison("2026-27", 4, pay, data_dir=tmp_path)
    assert out["available"] is True
    assert out["a_is"] == "gaffer_selected_candidate"
    assert out["b_is"] == "action_actually_taken"
    assert out["action_provenance"] == EV.PROV_RECONSTRUCTED
    assert out["same_action"] is False
    assert out["limitations"]
    # facts, not a grade
    assert "not_a_verdict" in out
    assert not any("decision" in k for k in out)


def test_a_freeze_from_a_different_before_state_refuses_to_be_compared(tmp_path):
    """The freeze is one run; the reviewed snapshot is the last run before the
    deadline. Usually the same run — the measured gap between refreshes has a
    median of 140 minutes and a maximum of 51 hours — but when a later run moves
    the squad, the frozen football belongs to a different world."""
    frozen_before = _before()
    later_before = _before(free_transfers=2)          # resources moved
    assert frozen_before.fingerprint != later_before.fingerprint

    _evidence(before=frozen_before,
              cands={"c": _cand(frozen_before, cid="c")},
              selected="c", baseline="c").save(EV.path_for("2026-27", 4, tmp_path))
    EV.record_action(EV.RecordedAction(
        season="2026-27", gameweek=4, evidence_id="ev",
        provenance=EV.PROV_RECONSTRUCTED, recorded_at="t",
        candidate=_cand(later_before, cid="h")), data_dir=tmp_path)

    pay = _payload(later_before, {"c2": _cand(later_before, cid="c2")},
                   "c2", "c2")
    out = EV.paired_comparison("2026-27", 4, pay, data_dir=tmp_path)
    assert out["available"] is False
    assert "different before-state" in out["why"]


def test_a_recommendation_that_changed_after_the_freeze_refuses_to_be_compared(tmp_path):
    before = _before()
    _evidence(before=before, cands={"c_frozen": _cand(before, cid="c_frozen")},
              selected="c_frozen", baseline="c_frozen").save(
                  EV.path_for("2026-27", 4, tmp_path))
    EV.record_action(EV.RecordedAction(
        season="2026-27", gameweek=4, evidence_id="ev",
        provenance=EV.PROV_RECONSTRUCTED, recorded_at="t",
        candidate=_cand(before, cid="h")), data_dir=tmp_path)

    # same world, but the selection moved to a candidate that was never frozen
    later = _cand(before, cid="c_later", captain=14, vice=13)
    pay = _payload(before, {"c_later": later}, "c_later", "c_later")
    out = EV.paired_comparison("2026-27", 4, pay, data_dir=tmp_path)
    assert out["available"] is False
    assert "not among the candidates frozen" in out["why"]
