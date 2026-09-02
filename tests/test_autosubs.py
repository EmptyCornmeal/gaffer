"""2B.3 -- automatic substitutions, and the Bench Boost baseline.

The Bench Boost gain was measured against an XI with NO substitutions, and its
own assumption line said so: "a benched non-starter contributes his own
simulated points rather than a replacement's." That understates the baseline --
without the chip, a starter who does not play is replaced by a bench player --
so the chip was credited with points the rules collect for free. It valued a
bench containing two players with no minutes at 6.74, against a bench that
returned 1 and 2 in the gameweeks actually played.

MECHANICS FIRST. A valuation built on a resolver that does not reproduce FPL's
rule is worse than no valuation, so the rule is tested directly before the chip
is scored on it.

FPL's rule, as implemented: a starter who records no minutes is replaced by the
first bench player IN BENCH ORDER who did play and whose introduction leaves at
least one goalkeeper, three defenders and one forward IN THE XI. Formation is a
property of the eleven names on the pitch, not of who scored -- a defender who
blanks and is not substituted still occupies a defensive slot.
"""
from __future__ import annotations

import numpy as np
import pytest

from gaffer.model.scenarios import ScenarioSet

# 1 GK + 4 DEF + 4 MID + 2 FWD starting; GK, DEF, MID, FWD on the bench.
POS = {
    1: "GKP", 2: "DEF", 3: "DEF", 4: "DEF", 5: "DEF",
    6: "MID", 7: "MID", 8: "MID", 9: "MID", 10: "FWD", 11: "FWD",
    12: "GKP", 13: "DEF", 14: "MID", 15: "FWD",
}
XI = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
BENCH = [12, 13, 14, 15]


def _set(appeared: dict[int, object], scores: dict[int, float], n: int = 4):
    """A scenario set whose points and appearances are CONSISTENT.

    A player who did not appear scores nothing. The first version of this file
    gave non-appearing players points, which is not a state the simulator can
    produce and made two expectations meaningless.
    """
    pids = sorted(POS)
    index = {pid: i for i, pid in enumerate(pids)}
    pts = np.zeros((len(pids), n), dtype=np.float32)
    app = np.zeros((len(pids), n), dtype=bool)
    for pid in pids:
        a = appeared.get(pid, True)
        app[index[pid]] = a
        pts[index[pid]] = np.where(app[index[pid]], scores.get(pid, 0.0), 0.0)
    return ScenarioSet(points=pts, player_ids=pids, index=index, n_sims=n,
                       seed=1, appeared=app)


ALL_TWO = dict.fromkeys(POS, 2.0)


def test_a_starter_who_does_not_play_is_replaced():
    s = _set({9: False}, ALL_TWO)
    # Ten who played at 2, plus the first legal bench replacement at 2.
    assert s.points_with_autosubs(XI, BENCH, POS)[0] == pytest.approx(22.0)


def test_nothing_happens_when_everyone_plays():
    s = _set({}, ALL_TWO)
    assert s.points_with_autosubs(XI, BENCH, POS)[0] == pytest.approx(22.0)


def test_a_bench_player_who_also_blanked_cannot_come_on():
    """The commonest real case, and the one the old baseline got wrong by
    crediting him anyway."""
    s = _set({9: False, 13: False, 14: False, 15: False}, ALL_TWO)
    # The bench keeper cannot replace a midfielder, and the rest did not play.
    assert s.points_with_autosubs(XI, BENCH, POS)[0] == pytest.approx(20.0)


def test_a_goalkeeper_is_replaced_only_by_the_bench_goalkeeper():
    s = _set({1: False}, {12: 7.0, 13: 9.0})
    assert s.points_with_autosubs(XI, BENCH, POS)[0] == pytest.approx(7.0), (
        "only the bench keeper may replace a keeper")


def test_an_outfielder_never_replaces_a_keeper():
    s = _set({1: False, 12: False}, {13: 9.0})
    assert s.points_with_autosubs(XI, BENCH, POS)[0] == pytest.approx(0.0)


def test_formation_legality_stops_the_second_substitution():
    """Four defenders start and three blank. The bench defender also blanks, so
    the midfielder may come on for the first (leaving three defenders) and the
    forward may NOT come on for the second (which would leave two)."""
    s = _set({2: False, 3: False, 4: False, 13: False}, {14: 9.0, 15: 5.0})
    got = s.points_with_autosubs(XI, BENCH, POS)[0]
    assert got == pytest.approx(9.0), (
        "one substitution is legal and the second is not")


def test_bench_order_is_respected():
    s = _set({9: False}, {13: 5.0, 14: 9.0})
    assert s.points_with_autosubs(XI, BENCH, POS)[0] == pytest.approx(5.0), (
        "FPL substitutes in BENCH ORDER, not by who scored most")


def test_it_resolves_per_scenario_not_once():
    """The whole point of doing this inside the simulation: which substitution
    happens depends on who played in THAT scenario."""
    s = _set({9: np.array([False, True, False, True])}, {13: 6.0})
    got = s.points_with_autosubs(XI, BENCH, POS)
    assert list(got) == pytest.approx([6.0, 0.0, 6.0, 0.0])


def test_without_an_appearance_mask_it_falls_back_rather_than_raising():
    pids = sorted(POS)
    index = {pid: i for i, pid in enumerate(pids)}
    s = ScenarioSet(points=np.ones((len(pids), 3), dtype=np.float32),
                    player_ids=pids, index=index, n_sims=3, seed=1)
    assert s.points_with_autosubs(XI, BENCH, POS)[0] == pytest.approx(11.0)


def test_the_bench_boost_gain_is_measured_against_the_autosubbed_baseline():
    """The regression case. The chip must not be credited with the points the
    substitution rules would have collected for free."""
    from gaffer import chips as CH

    s = _set({9: False}, ALL_TWO, n=64)
    ev = CH.evaluate_bench_boost(s, XI, BENCH, captain=None, gw=3, positions=POS)
    # Boosted: ten starters at 2 (the eleventh blanked) + four bench at 2 = 28.
    # Baseline WITH autosubs: 20 + the substitute's 2 = 22. The chip is worth 6.
    assert ev.expected_gain == pytest.approx(6.0)
    assert any("Autosubs ARE modelled" in a for a in ev.assumptions)

    naive = CH.evaluate_bench_boost(s, XI, BENCH, captain=None, gw=3)
    assert naive.expected_gain == pytest.approx(8.0)
    assert naive.expected_gain > ev.expected_gain, (
        "the old baseline must be the higher one; that was the over-credit")
    assert any("upper bound" in a for a in naive.assumptions)
