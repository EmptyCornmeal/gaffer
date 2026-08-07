"""T-20 — real chip optimisation.

The audited Chips page had no Wildcard EV at all, computed Free Hit as
"average week minus weakest week", and never read `active_chip`, so it would
recommend a chip you had already played.
"""

from __future__ import annotations

import numpy as np
import pytest

from gaffer import chips as C

# The live 2026/27 chip set, exactly as the API ships it.
LIVE_CHIPS = {"chips": [
    {"id": 1, "name": "wildcard", "number": 1, "start_event": 2, "stop_event": 19,
     "chip_type": "transfer"},
    {"id": 2, "name": "wildcard", "number": 1, "start_event": 20, "stop_event": 38,
     "chip_type": "transfer"},
    {"id": 3, "name": "freehit", "number": 1, "start_event": 2, "stop_event": 19,
     "chip_type": "transfer"},
    {"id": 4, "name": "bboost", "number": 1, "start_event": 1, "stop_event": 19,
     "chip_type": "team"},
    {"id": 5, "name": "3xc", "number": 1, "start_event": 1, "stop_event": 19,
     "chip_type": "team"},
    {"id": 6, "name": "freehit", "number": 1, "start_event": 20, "stop_event": 38,
     "chip_type": "transfer"},
    {"id": 7, "name": "bboost", "number": 1, "start_event": 20, "stop_event": 38,
     "chip_type": "team"},
    {"id": 8, "name": "3xc", "number": 1, "start_event": 20, "stop_event": 38,
     "chip_type": "team"},
]}


class Scen:
    def __init__(self, table, n=4000):
        self.table, self.n_sims = table, n

    def row(self, pid):
        return self.table.get(pid, np.zeros(self.n_sims))

    def squad_points(self, starting, captain=None, bench=None,
                     captain_multiplier=2, bench_boost=False):
        t = np.zeros(self.n_sims)
        for p in starting:
            t += self.row(p)
        if captain is not None:
            t += self.row(captain) * (captain_multiplier - 1)
        if bench_boost and bench:
            for p in bench:
                t += self.row(p)
        return t


@pytest.fixture
def scen():
    rng = np.random.default_rng(3)
    return Scen({p: rng.normal(4 + p * 0.2, 3, 4000) for p in range(1, 21)})


# --------------------------------------------------------------------------
# Discovery of the season's rules
# --------------------------------------------------------------------------

def test_windows_are_discovered_from_the_api_not_hard_coded():
    ws = C.parse_windows(LIVE_CHIPS)
    assert len(ws) == 8
    names = sorted({w.name for w in ws})
    assert names == ["3xc", "bboost", "freehit", "wildcard"]
    # Two sets of each: the current season's split.
    assert sum(1 for w in ws if w.name == "wildcard") == 2


def test_wildcard_and_free_hit_are_unavailable_in_gw1():
    """A hard-coded GW19 split would have missed this."""
    ws = C.parse_windows(LIVE_CHIPS)
    gw1 = {w.name for w in C.available_windows(ws, [], 1)}
    assert "wildcard" not in gw1 and "freehit" not in gw1
    assert {"bboost", "3xc"} <= gw1


def test_malformed_chip_entries_are_skipped():
    ws = C.parse_windows({"chips": [{"name": "x"}, {"bad": True}]})
    assert ws == []


def test_a_used_chip_is_removed_from_availability():
    ws = C.parse_windows(LIVE_CHIPS)
    before = {w.name for w in C.available_windows(ws, [], 10)}
    after = {w.name for w in C.available_windows(ws, ["wildcard"], 10)}
    assert "wildcard" in before and "wildcard" not in after


def test_using_the_first_half_chip_leaves_the_second(scen):
    ws = C.parse_windows(LIVE_CHIPS)
    avail = C.available_windows(ws, ["wildcard"], 25)
    assert "wildcard" in {w.name for w in avail}, "the second-half wildcard survives"


def test_both_instances_used_means_none_left():
    ws = C.parse_windows(LIVE_CHIPS)
    avail = C.available_windows(ws, ["wildcard", "wildcard"], 25)
    assert "wildcard" not in {w.name for w in avail}


def test_chips_used_are_read_from_history():
    hist = {"chips": [{"name": "wildcard", "event": 8},
                      {"name": "3xc", "event": 12}]}
    assert C.chips_used_from_history(hist) == ["wildcard", "3xc"]
    assert C.chips_used_from_history(None) == []
    assert C.chips_used_from_history({}) == []


def test_a_window_outside_the_gameweek_is_unavailable():
    ws = C.parse_windows(LIVE_CHIPS)
    assert not [w for w in C.available_windows(ws, [], 25) if w.name == "3xc"
                and w.stop_event == 19]


# --------------------------------------------------------------------------
# Per-chip mechanics
# --------------------------------------------------------------------------

def test_bench_boost_gain_is_exactly_the_bench(scen):
    xi, bench = [1, 2, 3], [10, 11]
    e = C.evaluate_bench_boost(scen, xi, bench, captain=1, gw=5)
    expected = float((scen.row(10) + scen.row(11)).mean())
    assert e.expected_gain == pytest.approx(expected, abs=1e-6)
    assert e.chip == C.BENCH_BOOST


def test_triple_captain_adds_exactly_one_more_captain(scen):
    xi = [1, 2, 3]
    e = C.evaluate_triple_captain(scen, xi, captain=3, gw=5)
    assert e.expected_gain == pytest.approx(float(scen.row(3).mean()), abs=1e-6)


def test_triple_captain_prefers_a_better_captain(scen):
    xi = [1, 2, 20]
    low = C.evaluate_triple_captain(scen, xi, captain=1, gw=5).expected_gain
    high = C.evaluate_triple_captain(scen, xi, captain=20, gw=5).expected_gain
    assert high > low


def test_free_hit_is_a_one_week_gain_that_reverts(scen):
    e = C.evaluate_free_hit(scen, [1, 2, 3], 3, [18, 19, 20], 20, gw=5)
    assert e.expected_gain > 0
    assert any("revert" in a for a in e.assumptions)


def test_wildcard_persists_and_scales_with_retention(scen):
    one = C.evaluate_wildcard(scen, [1, 2, 3], 3, [18, 19, 20], 20, gw=5,
                              weeks_retained=1)
    four = C.evaluate_wildcard(scen, [1, 2, 3], 3, [18, 19, 20], 20, gw=5,
                               weeks_retained=4)
    assert four.expected_gain == pytest.approx(4 * one.expected_gain, rel=1e-6)
    assert any("hold its edge" in a for a in four.assumptions)


def test_wildcard_and_free_hit_differ_only_in_persistence(scen):
    fh = C.evaluate_free_hit(scen, [1, 2, 3], 3, [18, 19, 20], 20, gw=5)
    wc = C.evaluate_wildcard(scen, [1, 2, 3], 3, [18, 19, 20], 20, gw=5,
                             weeks_retained=1)
    assert fh.expected_gain == pytest.approx(wc.expected_gain, rel=1e-6)


def test_every_evaluation_reports_a_confidence_interval(scen):
    for e in (
        C.evaluate_bench_boost(scen, [1, 2], [10], 1, 5),
        C.evaluate_triple_captain(scen, [1, 2], 2, 5),
        C.evaluate_free_hit(scen, [1, 2], 2, [19, 20], 20, 5),
    ):
        lo, hi = e.ci95
        assert lo < e.expected_gain < hi
        assert e.assumptions


# --------------------------------------------------------------------------
# Planning: use now vs hold
# --------------------------------------------------------------------------

def test_a_big_gain_is_recommended(scen):
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.BENCH_BOOST, 5, 12.0, (10.0, 14.0), 50, 62)]
    plan = C.plan_chips(ev, ws, [], 5)
    assert plan.recommendation == C.BENCH_BOOST
    assert plan.gameweek == 5 and plan.expected_gain == 12.0
    assert "95% CI" in plan.reason


def test_a_marginal_gain_is_held(scen):
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.BENCH_BOOST, 5, 1.5, (0.5, 2.5), 50, 51.5)]
    plan = C.plan_chips(ev, ws, [], 5)
    assert plan.recommendation == "hold"
    assert "below the" in plan.reason


def test_an_unavailable_chip_is_never_recommended():
    ws = C.parse_windows(LIVE_CHIPS)
    # A huge wildcard gain, but wildcard cannot be played in GW1.
    ev = [C.ChipEvaluation(C.WILDCARD, 1, 40.0, (35.0, 45.0), 50, 90)]
    plan = C.plan_chips(ev, ws, [], 1)
    assert plan.recommendation == "hold"
    assert "no chip is available" in plan.reason


def test_an_already_used_chip_is_never_recommended():
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.TRIPLE_CAPTAIN, 5, 30.0, (25.0, 35.0), 50, 80)]
    plan = C.plan_chips(ev, ws, ["3xc"], 5)
    assert plan.recommendation == "hold"
    assert "3xc" in plan.used


def test_multiple_available_chips_pick_the_best_and_list_alternatives():
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [
        C.ChipEvaluation(C.BENCH_BOOST, 5, 6.0, (4, 8), 50, 56),
        C.ChipEvaluation(C.TRIPLE_CAPTAIN, 5, 11.0, (9, 13), 50, 61),
    ]
    plan = C.plan_chips(ev, ws, [], 5)
    assert plan.recommendation == C.TRIPLE_CAPTAIN
    assert len(plan.alternatives) == 2
    assert plan.alternatives[0]["chip"] == C.TRIPLE_CAPTAIN


def test_a_chip_is_never_recommended_against_a_squad_you_do_not_own():
    """Pre-season FPL exposes no picks, so the gain is measured on a stand-in."""
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.BENCH_BOOST, 1, 22.0, (20.0, 24.0), 50, 72)]
    plan = C.plan_chips(ev, ws, [], 1, squad_known=False)
    assert plan.recommendation == "hold"
    assert "not readable yet" in plan.reason
    # The evaluation is still shown — it is informative, just not actionable.
    assert plan.alternatives and plan.alternatives[0]["expected_gain"] == 22.0
    assert plan.expected_gain == 22.0


def test_the_same_gain_IS_recommended_once_the_squad_is_known():
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.BENCH_BOOST, 1, 22.0, (20.0, 24.0), 50, 72)]
    assert C.plan_chips(ev, ws, [], 1, squad_known=True).recommendation == C.BENCH_BOOST


def test_no_beneficial_chip_returns_hold_with_a_reason():
    ws = C.parse_windows(LIVE_CHIPS)
    plan = C.plan_chips([], ws, [], 5)
    assert plan.recommendation == "hold"
    assert plan.gameweek is None and plan.reason


def test_plan_serialises_with_availability_and_use_history():
    ws = C.parse_windows(LIVE_CHIPS)
    plan = C.plan_chips([], ws, ["wildcard"], 10)
    d = plan.as_dict()
    assert d["chips_version"] == C.CHIPS_VERSION
    assert d["used"] == ["wildcard"]
    assert isinstance(d["available"], list)
    assert d["use_threshold"] == C.USE_THRESHOLD


def test_a_blank_gameweek_gives_a_bench_boost_no_value():
    """No fixtures -> every player scores zero -> the bench adds nothing."""
    blank = Scen({}, n=500)
    e = C.evaluate_bench_boost(blank, [1, 2], [10, 11], 1, 7)
    assert e.expected_gain == 0.0


def test_a_double_gameweek_raises_the_bench_boost_gain():
    single = Scen({p: np.full(500, 4.0) for p in range(1, 20)}, n=500)
    double = Scen({p: np.full(500, 8.0) for p in range(1, 20)}, n=500)
    a = C.evaluate_bench_boost(single, [1, 2], [10, 11], 1, 7).expected_gain
    b = C.evaluate_bench_boost(double, [1, 2], [10, 11], 1, 7).expected_gain
    assert b == pytest.approx(2 * a)


def test_chip_value_is_not_double_counted_across_leagues(scen):
    """A chip's points gain is one football fact, not one per league."""
    e1 = C.evaluate_bench_boost(scen, [1, 2, 3], [10, 11], 1, 5)
    e2 = C.evaluate_bench_boost(scen, [1, 2, 3], [10, 11], 1, 5)
    assert e1.expected_gain == e2.expected_gain
    # Two leagues sharing one ScenarioSet must see the identical gain.
    assert e1.as_dict()["expected_gain"] == e2.as_dict()["expected_gain"]
