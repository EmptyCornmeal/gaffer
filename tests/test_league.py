"""T-17/T-18 — rival ingestion, league-scoped EO, placing probability, conflicts."""

from __future__ import annotations

import numpy as np
import pytest

from gaffer import league as LG
from gaffer import multileague as ML

ME = 1066421


class FakeScen:
    """Deterministic scenario stand-in: each player scores a fixed vector."""

    def __init__(self, table: dict[int, np.ndarray], n: int):
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


def entry(eid, total=0, starting=None, captain=None, status=LG.PICKS_OK, rank=1):
    return LG.RivalEntry(
        entry_id=eid, entry_name=f"T{eid}", manager=f"M{eid}", rank=rank,
        total=total, starting=starting or [], captain=captain, picks_status=status)


def state(entries, size=4, cls=LG.TINY, ltype="x"):
    return LG.LeagueState(league_id=271619, name="Crouch Potatoes", league_type=ltype,
                          classification=cls, size=size, me=ME, entries=entries,
                          source_event=1)


# --------------------------------------------------------------------------
# Classification and bounding
# --------------------------------------------------------------------------

@pytest.mark.parametrize("size,ltype,expected", [
    (4, "x", LG.TINY), (8, "x", LG.TINY), (20, "x", LG.SMALL),
    (200, "x", LG.MEDIUM), (5000, "x", LG.LARGE),
    (4, "s", LG.GLOBAL), (10_000_000, "s", LG.GLOBAL), (None, "x", LG.MEDIUM),
])
def test_classification(size, ltype, expected):
    assert LG.classify(size, ltype) == expected


def test_every_class_has_a_bounded_cohort():
    for cls in (LG.TINY, LG.SMALL, LG.MEDIUM, LG.LARGE, LG.GLOBAL):
        assert 0 < LG.COHORT_LIMIT[cls] <= 100


# --------------------------------------------------------------------------
# League-scoped effective ownership
# --------------------------------------------------------------------------

def test_effective_ownership_counts_the_captain_twice():
    s = state([entry(ME, starting=[1, 2]),
               entry(2, starting=[1, 3], captain=1),
               entry(3, starting=[1, 4], captain=1),
               entry(4, starting=[5, 6], captain=5)])
    own = LG.league_ownership(s)
    # 3 rivals; player 1 owned by 2, captained by 2 -> EO = (2+2)/3
    assert own[1].owners == 2 and own[1].captains == 2
    assert own[1].effective == pytest.approx(4 / 3)
    assert own[1].ownership == pytest.approx(2 / 3)
    assert own[1].captain_eo == pytest.approx(2 / 3)


def test_a_four_person_league_has_quantised_ownership():
    """0/33/67/100% — not a continuous percentage over millions."""
    s = state([entry(ME, starting=[1]), entry(2, starting=[1]),
               entry(3, starting=[]), entry(4, starting=[])])
    own = LG.league_ownership(s)
    assert own[1].ownership in (0.0, 1 / 3, 2 / 3, 1.0)


def test_ownership_ignores_rivals_whose_squads_are_unknown():
    """Inferring from an unknown squad invents the number we are measuring."""
    s = state([entry(ME, starting=[1]),
               entry(2, starting=[1]),
               entry(3, status=LG.PICKS_NONE_YET),
               entry(4, status=LG.PICKS_FAILED)])
    own = LG.league_ownership(s)
    assert own[1].n_rivals == 1
    assert own[1].ownership == 1.0


def test_shields_and_differentials_split_correctly():
    s = state([entry(ME, starting=[1, 9]),
               entry(2, starting=[1, 3], captain=1),
               entry(3, starting=[1, 4]),
               entry(4, starting=[1, 5])])
    sd = LG.shields_and_differentials(s, [1, 9], my_captain=1)
    assert any(x["player_id"] == 1 for x in sd["shields"])
    assert any(x["player_id"] == 9 for x in sd["differentials"])
    assert sd["my_captain_eo_pct"] > 0


def test_global_ownership_is_never_read_by_the_league_layer():
    """League EO must come from rival squads, never from selected_by_percent.

    Checked against the parsed source so a mention in prose (this module
    explains the distinction at length) does not count as a use.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(LG))
    banned = {"selected_by_pct", "selected_by_percent"}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            hits.append(node.attr)
        if isinstance(node, ast.Constant) and node.value in banned:
            # A string constant is only a use if it indexes something; the
            # docstrings that discuss it are ast.Expr statements, not subscripts.
            hits.append(node.value)
    subscript_uses = [
        n.slice.value for n in ast.walk(tree)
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
        and n.slice.value in banned
    ]
    assert not [h for h in hits if h in banned and h in subscript_uses]
    assert not subscript_uses
    assert not any(isinstance(n, ast.Attribute) and n.attr in banned
                   for n in ast.walk(tree))


# --------------------------------------------------------------------------
# Placing probabilities
# --------------------------------------------------------------------------

def _scen(n=4000, seed=1):
    rng = np.random.default_rng(seed)
    return FakeScen({p: rng.normal(50, 10, n) for p in range(1, 20)}, n)


def test_leading_by_a_lot_gives_a_high_probability():
    sc = _scen()
    s = state([entry(ME, total=200, starting=[1]),
               entry(2, total=100, starting=[2]),
               entry(3, total=100, starting=[3]),
               entry(4, total=100, starting=[4])])
    r = LG.placing_probabilities(sc, s, [1], 1, target=1, gameweeks_remaining=1)
    assert r.p_first > 0.95
    assert r.expected_position < 1.2


def test_trailing_badly_gives_a_low_probability():
    sc = _scen()
    s = state([entry(ME, total=100, starting=[1]),
               entry(2, total=300, starting=[2]),
               entry(3, total=300, starting=[3]),
               entry(4, total=300, starting=[4])])
    r = LG.placing_probabilities(sc, s, [1], 1, target=1, gameweeks_remaining=1)
    assert r.p_first < 0.05


def test_target_position_is_easier_than_first():
    sc = _scen()
    s = state([entry(ME, total=100, starting=[1]),
               entry(2, total=120, starting=[2]),
               entry(3, total=110, starting=[3]),
               entry(4, total=90, starting=[4])])
    r1 = LG.placing_probabilities(sc, s, [1], 1, target=1)
    r3 = LG.placing_probabilities(sc, s, [1], 1, target=3)
    assert r3.p_target >= r1.p_target


def test_probabilities_are_reproducible():
    sc = _scen()
    s = state([entry(ME, total=100, starting=[1]), entry(2, total=100, starting=[2])])
    a = LG.placing_probabilities(sc, s, [1], 1, rng_seed=9)
    b = LG.placing_probabilities(sc, s, [1], 1, rng_seed=9)
    assert a.p_first == b.p_first


def test_confidence_interval_is_reported():
    sc = _scen()
    s = state([entry(ME, total=100, starting=[1]), entry(2, total=100, starting=[2])])
    r = LG.placing_probabilities(sc, s, [1], 1)
    assert r.ci_halfwidth > 0
    assert r.n_sims == sc.n_sims
    assert "p_first" in r.as_dict() and "ci95_halfwidth" in r.as_dict()


def test_unknown_rivals_are_flagged_not_treated_as_empty():
    sc = _scen()
    s = state([entry(ME, total=100, starting=[1]),
               entry(2, total=100, status=LG.PICKS_NONE_YET),
               entry(3, total=100, status=LG.PICKS_NONE_YET)])
    r = LG.placing_probabilities(sc, s, [1], 1)
    assert r.coverage_pct == 0.0
    assert any("unknown" in c for c in r.caveats)
    assert r.p_first < 0.99, "unknown rivals must not hand us a certain win"


def test_multi_week_probabilities_carry_the_weak_projection_caveat():
    sc = _scen()
    s = state([entry(ME, total=100, starting=[1]), entry(2, total=100, starting=[2])])
    r = LG.placing_probabilities(sc, s, [1], 1, gameweeks_remaining=6)
    assert any("multi-week" in c for c in r.caveats)


def test_no_scenarios_yields_no_false_precision():
    s = state([entry(ME, total=100, starting=[1])])
    r = LG.placing_probabilities(FakeScen({}, 0), s, [1], 1)
    assert r.basis == LG.BASIS_UNAVAILABLE and r.p_first == 0.0
    assert r.as_dict()["available"] is False


def test_an_empty_field_is_unknown_not_a_certain_win():
    """The live run said 100% to win the Overall league.

    Pre-season the global leagues publish no standings, so the cohort is just
    you. Simulating that field returns "you finish first in every scenario",
    which reads as certainty and is an artefact of having nobody to compare to.
    """
    s = state([entry(ME, total=0, starting=[1])])
    assert s.rivals == []
    r = LG.placing_probabilities(_scen(), s, [1], 1)
    assert r.basis == LG.BASIS_UNAVAILABLE
    assert r.p_first == 0.0 and r.p_target == 0.0
    assert r.as_dict()["available"] is False
    assert any("no rivals" in c for c in r.caveats)


def test_a_real_field_is_still_measured():
    s = state([entry(ME, total=100, starting=[1]), entry(2, total=100, starting=[2])])
    r = LG.placing_probabilities(_scen(), s, [1], 1)
    assert r.basis != LG.BASIS_UNAVAILABLE
    assert r.as_dict()["available"] is True


def test_an_empty_standings_page_reports_unknown_size_not_zero():
    class C:
        def league_classic(self, lid, page=1):
            return {"league": {"name": "Overall", "league_type": "s"},
                    "standings": {"has_next": False, "results": []},
                    "new_entries": {"results": []}}

    st = LG.fetch_league(C(), 314, ME, squad_event=None)
    assert st.size is None, "0 entries means 'not published', not 'a league of zero'"
    assert st.note


def test_tied_standings_do_not_crash():
    sc = _scen()
    s = state([entry(ME, total=100, starting=[1]), entry(2, total=100, starting=[2]),
               entry(3, total=100, starting=[3])])
    r = LG.placing_probabilities(sc, s, [1], 1)
    assert 0.0 <= r.p_first <= 1.0


def test_hits_reduce_a_rivals_total():
    sc = _scen()
    e = entry(2, total=100, starting=[2])
    e.hits = 8
    s = state([entry(ME, total=100, starting=[1]), e])
    r = LG.placing_probabilities(sc, s, [1], 1)
    assert r.p_first > 0.5, "a rival who took hits should be easier to beat"


# --------------------------------------------------------------------------
# Posture
# --------------------------------------------------------------------------

def test_leading_late_prefers_low_variance():
    p = LG.posture(points_gap=60, gameweeks_remaining=2, league_size=4)
    assert p.stance == "protect" and p.variance_preference < 0


def test_trailing_late_prefers_high_variance():
    p = LG.posture(points_gap=-80, gameweeks_remaining=2, league_size=4)
    assert p.stance == "desperate" and p.variance_preference > 0


def test_level_is_neutral():
    p = LG.posture(points_gap=0, gameweeks_remaining=10, league_size=4)
    assert p.stance == "neutral" and p.variance_preference == 0.0


def test_early_season_gaps_matter_less_than_late_ones():
    early = LG.posture(points_gap=-40, gameweeks_remaining=30, league_size=4)
    late = LG.posture(points_gap=-40, gameweeks_remaining=1, league_size=4)
    assert late.variance_preference > early.variance_preference


def test_thin_rival_data_forces_neutral():
    p = LG.posture(points_gap=-80, gameweeks_remaining=1, league_size=4, coverage=0.1)
    assert p.stance == "neutral"
    assert "too little rival data" in p.reason


# --------------------------------------------------------------------------
# Multi-league conflict (T-18)
# --------------------------------------------------------------------------

def opt(key, ep, **p):
    return ML.Option(key=key, label=key, expected_points=ep, p_target=p)


def test_two_leagues_favour_opposite_options():
    a = opt("captain_haaland", 60.0, L1=0.55, L2=0.20)
    b = opt("captain_differential", 58.0, L1=0.30, L2=0.45)
    conflicts = ML.find_conflicts([a, b], ["L1", "L2"])
    assert len(conflicts) == 1
    prefs = {d["league"]: d["prefers"] for d in conflicts[0].detail}
    assert prefs["L1"] == "captain_haaland"
    assert prefs["L2"] == "captain_differential"
    assert prefs["expected_points"] == "captain_haaland"


def test_no_default_without_weights_when_nothing_dominates():
    a = opt("A", 60.0, L1=0.55, L2=0.20)
    b = opt("B", 58.0, L1=0.30, L2=0.45)
    r = ML.resolve([a, b], None, ["L1", "L2"])
    assert r["default"] is None
    assert "no league weights configured" in r["reason"]
    assert len(r["shortlist"]) == 2 and r["conflicts"]


def test_a_dominating_option_is_named_even_without_weights():
    a = opt("A", 60.0, L1=0.6, L2=0.6)
    b = opt("B", 50.0, L1=0.3, L2=0.3)
    r = ML.resolve([a, b], None, ["L1", "L2"])
    assert r["default"] == "A"
    assert "dominates" in r["reason"]


def test_weights_break_the_tie_explicitly():
    a = opt("A", 60.0, L1=0.55, L2=0.20)
    b = opt("B", 58.0, L1=0.30, L2=0.45)
    r = ML.resolve([a, b], {"L1": 1.0, "L2": 0.0}, ["L1", "L2"])
    assert r["default"] == "A"
    r2 = ML.resolve([a, b], {"L1": 0.0, "L2": 1.0}, ["L1", "L2"])
    assert r2["default"] == "B"


def test_overall_rank_is_an_objective_not_an_absence():
    a = opt("A", 70.0, **{ML.OVERALL_KEY: 0.5, "L1": 0.2})
    b = opt("B", 60.0, **{ML.OVERALL_KEY: 0.2, "L1": 0.6})
    r = ML.resolve([a, b], {ML.OVERALL_KEY: 1.0, "L1": 0.0}, [ML.OVERALL_KEY, "L1"])
    assert r["default"] == "A"


def test_pareto_front_drops_dominated_options():
    a = opt("A", 60.0, L1=0.6)
    b = opt("B", 50.0, L1=0.3)      # dominated on both axes
    c = opt("C", 40.0, L1=0.9)
    front = ML.pareto_front([a, b, c], ["L1"])
    assert {o.key for o in front} == {"A", "C"}


def test_a_shield_can_matter_in_one_league_and_not_another():
    tiny = state([entry(ME, starting=[1]), entry(2, starting=[1]),
                  entry(3, starting=[1])], size=3)
    big = LG.LeagueState(
        league_id=314, name="Overall", league_type="s", classification=LG.GLOBAL,
        size=None, me=ME, source_event=1,
        entries=[entry(ME, starting=[1])] +
                [entry(100 + i, starting=[7]) for i in range(20)])
    assert LG.league_ownership(tiny)[1].ownership == 1.0
    assert LG.league_ownership(big).get(1) is None


def test_league_views_are_isolated():
    v1 = ML.LeagueView(1, "A", "x", LG.TINY, 4, 1, {}, {}, [], [], {})
    v2 = ML.LeagueView(2, "B", "x", LG.TINY, 4, 1, {}, {}, [], [], {})
    ML.assert_isolated([v1, v2])
    with pytest.raises(ValueError):
        ML.assert_isolated([v1, v1])


def test_no_options_is_handled():
    r = ML.resolve([], None, ["L1"])
    assert r["default"] is None and r["shortlist"] == []
