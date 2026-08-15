"""The prediction ledger — frozen pre-deadline, immutable afterwards.

The point of this module is a refusal, so most of what is worth testing is what
it declines to do.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gaffer import ledger

DEADLINE = "2026-08-21T17:30:00Z"
BEFORE = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
AFTER = datetime(2026, 8, 21, 19, 0, tzinfo=UTC)


def _slate(**over):
    s = {
        "ledger_version": ledger.LEDGER_VERSION,
        "gameweek": 1,
        "deadline": DEADLINE,
        "model_version": "heuristic-0.4",
        "frozen_at": "2026-08-21T12:00:00+00:00",
        "entries": [{
            "method": "gaffer", "label": "Gaffer", "objective": "next_gw_xp",
            "squad": list(range(1, 16)), "xi": list(range(1, 12)),
            "bench": [12, 13, 14, 15], "captain": 1, "vice": 2,
            "projected_xi_points": 60.0, "squad_value": 100.0, "names": {},
        }],
        "scored": None,
    }
    s.update(over)
    return s


def test_a_slate_can_be_refreshed_until_the_deadline(tmp_path):
    """Before kickoff a later run simply has better team news, so replacing the
    slate is honest — it is still a forecast."""
    p = tmp_path / "gw01.json"
    ledger.freeze(_slate(), p, now=BEFORE)
    ledger.freeze(_slate(frozen_at="later"), p, now=BEFORE)
    assert json.loads(p.read_text(encoding="utf-8"))["frozen_at"] == "later"


def test_a_slate_is_immutable_once_the_deadline_has_passed(tmp_path):
    """The whole feature. After the deadline every incentive points at editing
    the prediction, so nothing may rewrite it."""
    p = tmp_path / "gw01.json"
    ledger.freeze(_slate(), p, now=BEFORE)
    with pytest.raises(ledger.AlreadyFrozen, match="evidence now"):
        ledger.freeze(_slate(frozen_at="tampered"), p, now=AFTER)
    assert json.loads(p.read_text(encoding="utf-8"))["frozen_at"] != "tampered"


def test_a_scored_slate_cannot_be_rewritten_even_with_force(tmp_path):
    """`--force` covers a slate built from wrong inputs before kickoff. It does
    not cover a slate whose result is already known."""
    p = tmp_path / "gw01.json"
    ledger.freeze(_slate(), p, now=BEFORE)
    scored = ledger.score(json.loads(p.read_text(encoding="utf-8")), {1: 12})
    p.write_text(json.dumps(scored), encoding="utf-8")
    for force in (False, True):
        with pytest.raises(ledger.AlreadyFrozen, match="scored"):
            ledger.freeze(_slate(), p, now=BEFORE, force=force)


def test_a_slate_without_a_deadline_is_not_rewritten(tmp_path):
    """With no deadline there is no way to tell a refresh from a rewrite, and
    guessing in the permissive direction is how evidence gets edited."""
    p = tmp_path / "gw01.json"
    ledger.freeze(_slate(deadline=None), p, now=BEFORE)
    with pytest.raises(ledger.AlreadyFrozen):
        ledger.freeze(_slate(deadline=None), p, now=BEFORE)


def test_force_still_works_before_a_deadline(tmp_path):
    p = tmp_path / "gw01.json"
    ledger.freeze(_slate(deadline=None), p, now=BEFORE)
    ledger.freeze(_slate(deadline=None, frozen_at="fixed"), p, now=BEFORE, force=True)
    assert json.loads(p.read_text(encoding="utf-8"))["frozen_at"] == "fixed"


def test_scoring_doubles_the_captain_and_keeps_the_forecast(tmp_path):
    """The projection must survive scoring: a wrong forecast cannot later be
    described as a right one."""
    out = ledger.score(_slate(), {1: 10, 2: 5, 3: 2}, {1: 90, 2: 90})
    r = out["scored"]["results"][0]
    assert r["actual_xi_points"] == 10 + 5 + 2 + 10       # captain (1) counted twice
    assert r["projected_xi_points"] == 60.0
    assert r["error"] == pytest.approx(27 - 60.0)
    assert r["xi_players_who_played"] == 2


def test_the_scored_block_says_one_gameweek_proves_nothing():
    """The ordering it produces is the single most misreadable thing here, and
    the temptation to read GW1 as a verdict is the reason to write this down."""
    out = ledger.score(_slate(), {})
    assert "one sample" in out["scored"]["caveat"].lower()


def test_methods_cover_the_benchmarks_worth_having():
    names = {m[0] for m in ledger.METHODS}
    # The crowd and the naive baseline are the two that can embarrass the model,
    # which is exactly why they are not optional.
    assert {"gaffer", "naive_ppg", "template", "random"} <= names


def test_the_naive_baseline_ignores_a_three_game_sample():
    """`points_per_game` over a cameo is noise. A human running this strategy
    would notice, so the baseline is naive rather than silly."""
    assert ledger.MIN_SAMPLE_MINUTES >= 300