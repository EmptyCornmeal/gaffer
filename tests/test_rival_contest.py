"""Phase 3 -- the league layer becomes an objective rather than a report.

Maximising expected points and maximising the chance of finishing above a
particular person are different optimisation problems. Gaffer optimised the
first, reported the second, and pinned `ownership_weight = 0.0` with a comment
saying it MUST stay there until a placing objective existed. These are that
objective's first two pieces: the distribution of the difference, and what a
candidate move does to it.
"""
from __future__ import annotations

import numpy as np
import pytest

from gaffer import league as LG


class _Scen:
    """A scenario set with hand-set per-player rows, so the arithmetic is checkable."""

    def __init__(self, rows: dict[int, np.ndarray], n: int):
        self._rows, self.n_sims = rows, n

    def squad_points(self, starting, captain=None, bench=None,
                     captain_multiplier=2, bench_boost=False):
        total = np.zeros(self.n_sims)
        for pid in starting:
            total = total + self._rows.get(pid, np.zeros(self.n_sims))
        if captain is not None:
            total = total + self._rows.get(
                captain, np.zeros(self.n_sims)) * (captain_multiplier - 1)
        return total


def _state(rivals):
    me = LG.RivalEntry(entry_id=99, manager="Me", total=100)
    st = LG.LeagueState(league_id=1, name="L", league_type="x",
                        classification="small", size=1 + len(rivals), me=99,
                        entries=[me, *rivals])
    return st, me


def _rival(entry_id, total, starting, name="R", hits=0):
    return LG.RivalEntry(entry_id=entry_id, manager=name, total=total,
                         starting=list(starting), picks_status=LG.PICKS_OK,
                         hits=hits)


def test_the_gap_is_a_difference_not_two_separate_distributions():
    """Shared players cancel. If we own the same eleven the difference is
    exactly the banked points gap, with no spread at all -- which two
    independently drawn distributions would never show."""
    n = 500
    rows = {i: np.random.default_rng(i).normal(5, 3, n) for i in range(1, 12)}
    rivals = [_rival(2, 90, list(range(1, 12)))]
    st, _me = _state(rivals)
    gaps = LG.rival_gaps(_Scen(rows, n), st, list(range(1, 12)), my_captain=None)
    assert len(gaps) == 1
    assert gaps[0].std == pytest.approx(0.0, abs=1e-9)
    assert gaps[0].mean == pytest.approx(10.0)
    assert gaps[0].p_above == 1.0
    assert gaps[0].overlap == 11


def test_the_banked_gap_enters_as_a_constant():
    n = 400
    rows = {1: np.zeros(n), 2: np.zeros(n)}
    st, _ = _state([_rival(2, 130, [2])])
    g = LG.rival_gaps(_Scen(rows, n), st, [1], my_captain=None)[0]
    assert g.gap == pytest.approx(-30.0)
    assert g.p_above == 0.0, "thirty behind with no variance is not a contest"


def test_rows_are_sorted_by_how_close_the_contest_is():
    """The rival a decision can actually move goes first; the one twenty points
    clear is not the one to plan around."""
    n = 200
    rows = {i: np.zeros(n) for i in range(1, 5)}
    st, _ = _state([_rival(2, 20, [2], "Far"), _rival(3, 99, [3], "Near")])
    gaps = LG.rival_gaps(_Scen(rows, n), st, [1], my_captain=None)
    assert [g.name for g in gaps] == ["Near", "Far"]


def test_an_unpublished_squad_is_marked_inferred():
    """Carrying him at his current points would claim he scores nothing, which
    is a stronger statement than "we cannot see his team"."""
    n = 300
    rows = {1: np.full(n, 50.0)}
    st = LG.LeagueState(league_id=1, name="L", league_type="x",
                        classification="small", size=2, me=99,
                        entries=[LG.RivalEntry(entry_id=99, manager="Me", total=100),
                                 LG.RivalEntry(entry_id=2, manager="Hidden", total=100)])
    g = LG.rival_gaps(_Scen(rows, n), st, [1], my_captain=None)[0]
    assert g.inferred is True
    assert g.std > 0.0, "an unknown squad must carry uncertainty, not a point"


def test_the_domain_says_next_gameweek():
    n = 100
    rows = {1: np.zeros(n)}
    st, _ = _state([_rival(2, 100, [1])])
    d = LG.rival_gaps(_Scen(rows, n), st, [1], my_captain=None)[0].as_dict()
    # The domain is a property of the MEASUREMENT, not of the rival, so it is
    # published once for the set rather than repeated on every row -- which was
    # both noise and 200 bytes of a capped response spent six times over.
    assert "domain" not in d
    assert LG.RivalGap.DOMAIN["horizon"] == "next_gameweek"
    assert "not at the end of the season" in LG.RivalGap.DOMAIN["measures"]
    assert d["p_above_ci95_interval_type"] == "monte_carlo"


# --- move effects ---------------------------------------------------------

def test_the_delta_is_paired_so_agreeing_scenarios_contribute_nothing():
    """The reason this is tight. In every scenario where the hold and the move
    are both ahead, the difference is exactly zero and adds no variance --
    which is why combining two marginal intervals would be far too wide and
    would report real edges as ties."""
    n = 2000
    rng = np.random.default_rng(4)
    common = rng.normal(50, 10, n)
    rows = {1: common, 2: common + 1.0, 3: np.zeros(n)}
    st, _ = _state([_rival(2, 100, [3])])
    scen = _Scen(rows, n)
    effects = LG.move_effects(scen, st, hold_starting=[1], hold_captain=None,
                              move_starting=[2], move_captain=None)
    e = effects[0]
    lo, hi = e.d_p_above_ci95
    assert hi - lo < 0.05, "a paired interval on a one-point improvement is tight"
    assert e.d_expected_points == pytest.approx(1.0, abs=1e-6)


def test_an_unresolvable_delta_is_reported_as_tied():
    """Two identical squads cannot differ. The interval must contain zero and
    `resolved` must be False rather than the point estimate being ranked."""
    n = 1000
    rows = {1: np.random.default_rng(1).normal(50, 10, n), 2: np.zeros(n)}
    st, _ = _state([_rival(2, 100, [2])])
    e = LG.move_effects(_Scen(rows, n), st, [1], None, [1], None)[0]
    assert e.d_p_above == pytest.approx(0.0)
    assert e.resolved is False
    assert e.as_dict()["d_p_above_ci95_interval_type"] == "monte_carlo_paired"


def test_a_hit_is_charged_to_the_move():
    n = 500
    rows = {1: np.full(n, 50.0), 2: np.full(n, 50.0), 3: np.zeros(n)}
    st, _ = _state([_rival(2, 100, [3])])
    e = LG.move_effects(_Scen(rows, n), st, [1], None, [2], None, hit_cost=4.0)[0]
    assert e.d_expected_points == pytest.approx(-4.0)


def test_the_variance_ratio_is_a_diagnostic_and_refuses_to_divide_by_nothing():
    """As a RANKING the ratio misbehaves exactly where covering decisions live:
    it explodes as the sacrifice approaches zero and is meaningless when the
    cover is also the better points pick."""
    e = LG.MoveEffect(
        entry_id=1, name="R", d_expected_points=+2.0, d_p_above=0.01,
        d_p_above_ci95=(0.0, 0.02), d_variance_of_gap=-10.0,
        p_above_before=0.5, p_above_after=0.51, n_sims=100)
    assert e.variance_reduction_per_point is None, (
        "no expected points were given up, so there is no ratio to report")

    e2 = LG.MoveEffect(
        entry_id=1, name="R", d_expected_points=-2.0, d_p_above=0.01,
        d_p_above_ci95=(0.0, 0.02), d_variance_of_gap=-10.0,
        p_above_before=0.5, p_above_after=0.51, n_sims=100)
    assert e2.variance_reduction_per_point == pytest.approx(5.0)


def test_resolved_effects_rank_above_unresolved_ones():
    """A large point estimate that the simulation cannot resolve must not
    outrank a small one it can."""
    big_unresolved = LG.MoveEffect(1, "A", 0.0, 0.20, (-0.05, 0.45), 0.0,
                                   0.5, 0.7, 2000)
    small_resolved = LG.MoveEffect(2, "B", 0.0, 0.02, (0.01, 0.03), 0.0,
                                   0.5, 0.52, 2000)
    assert big_unresolved.resolved is False
    assert small_resolved.resolved is True
    ranked = sorted([big_unresolved, small_resolved],
                    key=lambda e: (e.resolved, abs(e.d_p_above)), reverse=True)
    assert ranked[0] is small_resolved
