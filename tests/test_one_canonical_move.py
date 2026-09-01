"""1.4/1.5 -- one run must publish one first move.

On 2026-09-01 `decision.json` offered four transfers for -12 while `plan.json`
offered three for -8, from the same pipeline run, rendered on adjacent pages.
Every shape invariant passed because each artifact was individually well
formed. `objective.py` had unified the two solvers' WEIGHTS; nobody had
unified the window they were applied over (`min(5, horizon)` against `horizon`)
or decided which solver owned the answer.
"""
from __future__ import annotations

import json

from gaffer import contract, weekly


def _write(tmp_path, dec_in, dec_out, plan_in, plan_out):
    (tmp_path / "decision.json").write_text(json.dumps({
        "decision": {"transfers_in": dec_in, "transfers_out": dec_out},
    }), encoding="utf-8")
    (tmp_path / "plan.json").write_text(json.dumps({
        "steps": [{"gw": 3, "transfers_in": plan_in, "transfers_out": plan_out}],
    }), encoding="utf-8")


def _violations(tmp_path):
    rep = contract.Report(data_dir=str(tmp_path))
    contract._check_one_canonical_first_move(tmp_path, rep)
    return rep.violations


def test_the_invariant_fires_on_a_real_divergence(tmp_path):
    """The exact 2026-09-01 shape: four transfers against three."""
    _write(tmp_path, [31, 204, 211, 388], [175, 418, 504, 557],
           [31, 127, 388], [175, 504, 557])
    v = _violations(tmp_path)
    assert v, "a contract that cannot catch the defect it was written for is decoration"
    assert any("first move" in str(x.expected) for x in v)


def test_it_passes_when_they_agree(tmp_path):
    _write(tmp_path, [31, 94, 388], [418, 504, 557],
           [31, 94, 388], [418, 504, 557])
    assert _violations(tmp_path) == []


def test_ordering_is_not_a_disagreement(tmp_path):
    """Order is a rendering choice; sets are the claim."""
    _write(tmp_path, [388, 31, 94], [557, 418, 504],
           [31, 94, 388], [418, 504, 557])
    assert _violations(tmp_path) == []


def test_a_roll_agrees_with_a_roll(tmp_path):
    _write(tmp_path, [], [], [], [])
    assert _violations(tmp_path) == []


def test_a_rejected_candidate_is_still_compared(tmp_path):
    """When the decision is a roll it publishes its move as `candidate_move`.
    That is still the move, and it must still match the plan."""
    (tmp_path / "decision.json").write_text(json.dumps({
        "decision": {"transfers_in": [], "transfers_out": [],
                     "candidate_move": {"transfers_in": [{"id": 31}],
                                        "transfers_out": [{"id": 504}]}},
    }), encoding="utf-8")
    (tmp_path / "plan.json").write_text(json.dumps({
        "steps": [{"gw": 3, "transfers_in": [{"id": 99}],
                   "transfers_out": [{"id": 504}]}],
    }), encoding="utf-8")
    rep = contract.Report(data_dir=str(tmp_path))
    contract._check_one_canonical_first_move(tmp_path, rep)
    assert rep.violations, "a candidate move is still a published first move"


def test_the_decision_reads_the_plans_first_step_not_the_single_window_solve():
    """The source of the move is now explicit and testable."""
    class _Step:
        gw = 3
        squad = list(range(1, 16))
        starting = list(range(1, 12))
        captain, vice = 1, 2
        transfers_in, transfers_out, hits = [31], [504], 0

    class _Plan:
        steps = [_Step()]

    mv = weekly._first_step(_Plan(), 3)
    assert mv is not None
    assert mv["source"] == "multiperiod_first_step"
    assert mv["transfers_in"] == [31]
    assert len(mv["bench"]) == 4


def test_a_step_for_the_wrong_gameweek_is_refused():
    """Reading last week's step as this week's move would be worse than
    re-solving."""
    class _Step:
        gw = 4
        squad = list(range(1, 16))
        starting = list(range(1, 12))
        captain, vice = 1, 2
        transfers_in, transfers_out, hits = [31], [504], 0

    class _Plan:
        steps = [_Step()]

    assert weekly._first_step(_Plan(), 3) is None
    assert weekly._first_step(None, 3) is None


def test_an_incomplete_xi_falls_back_rather_than_publishing_ten_players():
    class _Step:
        gw = 3
        squad = list(range(1, 16))
        starting = list(range(1, 11))   # ten
        captain, vice = 1, 2
        transfers_in, transfers_out, hits = [], [], 0

    class _Plan:
        steps = [_Step()]

    assert weekly._first_step(_Plan(), 3) is None
