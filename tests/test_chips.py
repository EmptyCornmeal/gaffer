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



def now_is_best(chip, gw, through=19, now=99.0, later=1.0):
    """A timing profile covering the whole window, peaking at ``gw``.

    Planning tests that are not about timing pass this so the WHEN question is
    answered explicitly. A test that simply omits it is asserting the opposite —
    that Gaffer declines to recommend a chip whose timing it has not checked.
    """
    prof = {g: later for g in range(gw, through + 1)}
    prof[gw] = now
    return {chip: prof}


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


def test_chip_uses_carry_the_gameweek_they_were_played_in():
    hist = {"chips": [{"name": "wildcard", "event": 8},
                      {"name": "3xc", "event": "12"},
                      {"name": "bboost"},
                      {"name": "freehit", "event": None}]}
    uses = C.chip_uses_from_history(hist)
    assert uses[0] == C.ChipUse("wildcard", 8)
    assert uses[1] == C.ChipUse("3xc", 12), "a string event is still an event"
    assert uses[2].event is None and uses[3].event is None


def test_a_second_half_chip_consumes_its_own_window_not_the_expired_one():
    """The defect: matching a use to a window by NAME deleted the earliest
    window bearing that name. Play the second-half Wildcard while the first-half
    one expired unused and it removed the *expired* window, leaving the one you
    had just spent looking available — so it could be recommended again."""
    ws = C.parse_windows(LIVE_CHIPS)
    used = [C.ChipUse("wildcard", 20)]      # played in the second half
    assert "wildcard" not in {w.name for w in C.available_windows(ws, used, 21)}, \
        "the wildcard played in GW20 must not still be on offer in GW21"


def test_the_first_half_window_survives_a_second_half_use():
    ws = C.parse_windows(LIVE_CHIPS)
    used = [C.ChipUse("wildcard", 20)]
    # Hypothetically back in GW10 the first-half instance is still unspent.
    assert "wildcard" in {w.name for w in C.available_windows(ws, used, 10)}


def test_a_bare_name_still_works_for_callers_without_event_data():
    ws = C.parse_windows(LIVE_CHIPS)
    assert "wildcard" not in {w.name for w in C.available_windows(ws, ["wildcard"], 10)}


def test_an_unreadable_chip_ledger_recommends_nothing(scen):
    """"We could not read your chip history" is not "you have played none"."""
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.evaluate_bench_boost(scen, list(range(1, 12)), [12, 13, 14, 15], 1, 5)]
    plan = C.plan_chips(ev, ws, [], 5, chip_state_known=False)
    assert plan.recommendation == "hold"
    assert plan.state_known is False
    assert "already played" in plan.reason
    assert plan.as_dict()["state_known"] is False


def test_a_known_ledger_still_recommends_a_strong_chip(scen):
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.evaluate_bench_boost(scen, list(range(1, 12)), [12, 13, 14, 15], 1, 5)]
    plan = C.plan_chips(ev, ws, [], 5, timing=now_is_best(C.BENCH_BOOST, 5))
    assert plan.state_known is True
    assert plan.recommendation == "bboost"


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


def test_the_wildcard_is_not_the_free_hit_times_the_window(scen):
    """The defect: `d = (optimal - yours) * weeks_retained` on the SAME inputs
    the Free Hit got, so the wildcard was `free_hit_gain x weeks` by
    construction. It could never rank below the Free Hit, and a four-week
    window quartered the real bar for burning it."""
    one = C.evaluate_wildcard(scen, [1, 2, 3], 3, [18, 19, 20], 20, gw=5,
                              weeks_retained=1)
    four = C.evaluate_wildcard(scen, [1, 2, 3], 3, [18, 19, 20], 20, gw=5,
                               weeks_retained=4)
    assert four.expected_gain != pytest.approx(4 * one.expected_gain, rel=1e-3)
    assert any("free-transfer path" in a for a in four.assumptions)
    assert any("OVERSTATES" in a for a in four.assumptions)


def test_a_one_week_wildcard_never_beats_a_free_hit(scen):
    """One week of the same squad, but the Free Hit costs no transfers and the
    wildcard consumes ones you would have had anyway."""
    fh = C.evaluate_free_hit(scen, [1, 2, 3], 3, [18, 19, 20], 20, gw=5)
    wc = C.evaluate_wildcard(scen, [1, 2, 3], 3, [18, 19, 20], 20, gw=5,
                             weeks_retained=1)
    assert wc.expected_gain < fh.expected_gain


def test_the_wildcard_nets_off_the_transfers_you_had_anyway(scen):
    """Its value is ACCELERATION. The raw distance to the optimum is a property
    every squad has every week; free transfers close it for nothing."""
    xi, best = [1, 2, 3], [18, 19, 20]
    raw = float((scen.squad_points(best, captain=20)
                 - scen.squad_points(xi, captain=3)).mean())
    wc = C.evaluate_wildcard(scen, xi, 3, best, 20, gw=5, weeks_retained=4)
    # Three players differ, one free transfer a week: by GW4 the free-transfer
    # path has caught up entirely, so only 2/3 + 1/3 + 0 + 0 = 1 week of edge
    # is the chip's.
    assert wc.expected_gain == pytest.approx(raw, rel=1e-6)
    assert wc.expected_gain < 4 * raw


def test_a_wildcard_that_changes_nothing_is_worth_nothing(scen):
    """A free transfer reaches an identical squad, so the chip buys no time."""
    wc = C.evaluate_wildcard(scen, [1, 2, 3], 3, [1, 2, 3], 3, gw=5,
                             weeks_retained=4)
    assert wc.expected_gain == pytest.approx(0.0, abs=1e-9)


def test_the_free_transfer_catchup_closes_the_gap_and_stops():
    w = C.free_transfer_catchup(weeks=6, changes=3, free_transfers_per_week=1.0)
    assert w == pytest.approx([2 / 3, 1 / 3, 0.0, 0.0, 0.0, 0.0])
    # A gap wider than the window is never fully closed by free transfers.
    assert all(x > 0 for x in C.free_transfer_catchup(4, 10, 1.0))
    assert C.free_transfer_catchup(3, 0, 1.0) == [0.0, 0.0, 0.0]


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
    plan = C.plan_chips(ev, ws, [], 5, timing=now_is_best(C.BENCH_BOOST, 5))
    assert plan.recommendation == C.BENCH_BOOST
    assert plan.gameweek == 5 and plan.expected_gain == 12.0
    assert "95% CI" in plan.reason
    assert plan.candidate is None


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
    plan = C.plan_chips(ev, ws, [], 5,
                        timing={**now_is_best(C.BENCH_BOOST, 5),
                                **now_is_best(C.TRIPLE_CAPTAIN, 5)})
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
    plan = C.plan_chips(ev, ws, [], 1, squad_known=True,
                        timing=now_is_best(C.BENCH_BOOST, 1))
    assert plan.recommendation == C.BENCH_BOOST


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


# --------------------------------------------------------------------------
# Published arithmetic must close
# --------------------------------------------------------------------------

def _all_four(scen):
    xi, bench, best = [1, 2, 3], [10, 11], [18, 19, 20]
    return [
        C.evaluate_bench_boost(scen, xi, bench, captain=3, gw=5),
        C.evaluate_triple_captain(scen, xi, captain=3, gw=5),
        C.evaluate_free_hit(scen, xi, 3, best, 20, gw=5),
        C.evaluate_wildcard(scen, xi, 3, best, 20, gw=5, weeks_retained=4),
    ]


def test_every_chip_gain_equals_its_own_published_difference(scen):
    """The defect: the wildcard multiplied the GAIN by the retention window and
    published the UN-multiplied one-week means beside it, so `strategy.json`
    disagreed with itself by exactly the multiplier — 0.47 shown against 0.11
    computable — and only for the wildcard."""
    for e in _all_four(scen):
        d = e.as_dict()
        implied = round(d["with_chip_points"] - d["baseline_points"], 2)
        assert implied == d["expected_gain"], (
            f"{e.chip}: publishes a gain of {d['expected_gain']} beside figures "
            f"that differ by {implied}")


def test_the_wildcard_specifically_cannot_diverge_again(scen):
    """The regression that matters: the wildcard's ratio of published gain to
    published difference was 4.27 while every other chip's was 1.00."""
    wc = next(e for e in _all_four(scen) if e.chip == C.WILDCARD)
    d = wc.as_dict()
    implied = d["with_chip_points"] - d["baseline_points"]
    assert implied == pytest.approx(d["expected_gain"], abs=0.01)
    assert wc.horizon == 4 and d["horizon_gameweeks"] == 4
    assert d["expected_gain_per_gameweek"] == pytest.approx(
        d["expected_gain"] / 4, abs=0.01)


def test_an_evaluation_whose_arithmetic_does_not_close_cannot_be_built():
    with pytest.raises(ValueError, match="with_chip - baseline"):
        C.ChipEvaluation(C.WILDCARD, 5, 0.47, (0.0, 1.0), 49.38, 49.49)


def test_a_horizon_below_one_gameweek_is_refused():
    with pytest.raises(ValueError, match="at least one gameweek"):
        C.ChipEvaluation(C.WILDCARD, 5, 1.0, (0.0, 2.0), 10.0, 11.0, horizon=0)


def test_the_published_figures_reconcile_at_two_decimal_places():
    """Rounding all three independently let a 0.115 gain print beside a
    difference of 0.11."""
    e = C.ChipEvaluation(C.BENCH_BOOST, 5, 0.115, (0.0, 0.3), 49.375, 49.49)
    d = e.as_dict()
    assert round(d["with_chip_points"] - d["baseline_points"], 2) == \
        d["expected_gain"]


# --------------------------------------------------------------------------
# Timing: a chip is a WHEN decision
# --------------------------------------------------------------------------

def test_a_chip_is_not_recommended_when_its_timing_was_never_assessed():
    """The defect: the highest-gain available chip was fired in the CURRENT
    gameweek the moment it cleared a flat bar. Live, that recommended Triple
    Captain in GW3 with 36 gameweeks left in the season."""
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.TRIPLE_CAPTAIN, 3, 7.82, (7.6, 8.1), 49.38, 57.20)]
    plan = C.plan_chips(ev, ws, [], 3)
    assert plan.recommendation == "hold"
    assert plan.candidate is not None
    assert plan.candidate["chip"] == C.TRIPLE_CAPTAIN
    assert "not been assessed" in plan.reason
    assert plan.timing["not_assessed"] == [C.TRIPLE_CAPTAIN]


def test_a_partly_assessed_window_is_a_candidate_not_a_recommendation():
    """Gaffer projects five gameweeks; the 3xc window runs to GW19. Best of what
    it can see is not best of the window, and it must not claim otherwise."""
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.TRIPLE_CAPTAIN, 3, 7.82, (7.6, 8.1), 49.38, 57.20)]
    profile = {C.TRIPLE_CAPTAIN: {3: 7.8, 4: 6.0, 5: 6.2, 6: 5.9, 7: 6.1}}
    plan = C.plan_chips(ev, ws, [], 3, timing=profile, projected_through=7)
    assert plan.recommendation == "hold"
    assert plan.candidate is not None
    assert "GW8-GW19" in plan.reason
    assert plan.timing["partly_assessed"] == [C.TRIPLE_CAPTAIN]
    assert plan.timing["by_chip"][C.TRIPLE_CAPTAIN]["coverage"] == C.TIMING_PARTIAL


def test_a_better_later_gameweek_is_held_for_by_name():
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.BENCH_BOOST, 3, 8.0, (6.0, 10.0), 50.0, 58.0)]
    profile = {C.BENCH_BOOST: {3: 8.0, 4: 6.0, 5: 19.0, 6: 7.0, 7: 6.5}}
    plan = C.plan_chips(ev, ws, [], 3, timing=profile, projected_through=7)
    assert plan.recommendation == "hold"
    assert "GW5" in plan.reason
    assert plan.timing["by_chip"][C.BENCH_BOOST]["best_gameweek"] == 5


def test_a_thin_future_edge_is_not_a_plan():
    """Multi-week projections are weak; a fraction of a point later is noise."""
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.BENCH_BOOST, 3, 8.0, (6.0, 10.0), 50.0, 58.0)]
    profile = {C.BENCH_BOOST: {g: 8.0 for g in range(3, 20)}}
    profile[C.BENCH_BOOST][9] = 8.0 + C.TIMING_MARGIN / 2
    plan = C.plan_chips(ev, ws, [], 3, timing=profile, projected_through=19)
    assert plan.recommendation == C.BENCH_BOOST


def test_a_fully_assessed_window_where_now_wins_is_recommended():
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.BENCH_BOOST, 3, 12.0, (10.0, 14.0), 50.0, 62.0)]
    profile = {C.BENCH_BOOST: {g: 4.0 for g in range(3, 20)}}
    profile[C.BENCH_BOOST][3] = 12.0
    plan = C.plan_chips(ev, ws, [], 3, timing=profile, projected_through=19)
    assert plan.recommendation == C.BENCH_BOOST
    assert plan.timing["by_chip"][C.BENCH_BOOST]["coverage"] == C.TIMING_FULL
    assert plan.candidate is None


def test_the_last_gameweek_of_a_window_needs_no_timing_check():
    """There is no later gameweek to hold it for: use it or lose it."""
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.BENCH_BOOST, 19, 6.0, (4.0, 8.0), 50.0, 56.0)]
    plan = C.plan_chips(ev, ws, [], 19)
    assert plan.recommendation == C.BENCH_BOOST
    assert plan.timing["by_chip"][C.BENCH_BOOST]["coverage"] == C.TIMING_MOOT
    assert "window ends here" in plan.reason


def test_the_plan_publishes_what_it_did_and_did_not_check():
    ws = C.parse_windows(LIVE_CHIPS)
    ev = [C.ChipEvaluation(C.TRIPLE_CAPTAIN, 3, 7.82, (7.6, 8.1), 49.38, 57.20),
          C.ChipEvaluation(C.WILDCARD, 3, 5.0, (3.0, 7.0), 197.5, 202.5,
                           horizon=4)]
    plan = C.plan_chips(ev, ws, [], 3,
                        timing={C.TRIPLE_CAPTAIN: {3: 7.8, 4: 6.0}},
                        timing_basis="mean projections GW3-GW4",
                        projected_through=4)
    d = plan.as_dict()
    assert d["timing"]["basis"] == "mean projections GW3-GW4"
    assert d["timing"]["projected_through"] == 4
    assert d["timing"]["not_assessed"] == [C.WILDCARD]
    assert d["timing"]["partly_assessed"] == [C.TRIPLE_CAPTAIN]
    assert d["candidate"]["chip"] == C.TRIPLE_CAPTAIN
    assert d["candidate"]["why_not_recommended"]


def test_timing_ignores_gameweeks_outside_the_chips_window():
    """A second-half wildcard peak is no reason to hold a first-half chip."""
    rep = C.timing_report(C.BENCH_BOOST, 3, 19,
                          {3: 6.0, 10: 7.0, 25: 40.0})
    assert rep["best_gameweek"] == 10
    assert "25" not in rep["gameweeks"]
