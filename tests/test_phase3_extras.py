"""Phase 3.6 / 3.9 / 3.10 -- leverage, the target band, and the chip horizon."""
from __future__ import annotations

import numpy as np
import pytest

from gaffer import league as LG
from gaffer import strategy as ST


class _Scen:
    def __init__(self, rows, n):
        self._rows, self.n_sims = rows, n

    def row(self, pid):
        return self._rows.get(pid, np.zeros(self.n_sims))

    def squad_points(self, starting, captain=None, bench=None,
                     captain_multiplier=2, bench_boost=False):
        t = np.zeros(self.n_sims)
        for pid in starting:
            t = t + self.row(pid)
        if captain is not None:
            t = t + self.row(captain) * (captain_multiplier - 1)
        return t


def _state(rivals, size=None):
    me = LG.RivalEntry(entry_id=99, manager="Me", total=100)
    return LG.LeagueState(
        league_id=1, name="L", league_type="x", classification="small_private",
        size=size or (1 + len(rivals)), me=99, entries=[me, *rivals])


def _rival(eid, starting, name="R"):
    return LG.RivalEntry(entry_id=eid, manager=name, total=100,
                         starting=list(starting), picks_status=LG.PICKS_OK)


# --- 3.6 ------------------------------------------------------------------

def test_a_steady_differential_has_far_less_leverage_than_a_volatile_one():
    """The distinction the review asked for. Both differ from every rival's
    squad; only one of them decides weeks."""
    n = 4000
    rng = np.random.default_rng(3)
    shared = rng.normal(4, 2, n)
    rows = {
        1: shared, 2: shared, 3: shared,          # owned by both sides
        10: np.full(n, 4.0),                      # a differential with NO spread
        11: rng.normal(4, 8, n),                  # same mean, much more spread
    }
    st = _state([_rival(2, [1, 2, 3])])
    lev = LG.differential_leverage(_Scen(rows, n), st, [1, 2, 3, 10, 11], None)
    by = {r["player_id"]: r for r in lev}
    assert by[10]["leverage_points"] == pytest.approx(0.0, abs=0.05), (
        "a differential that never varies cannot separate anybody")
    assert by[11]["leverage_points"] > 5.0
    assert lev[0]["player_id"] == 11, "ranked by leverage, not by ownership"


def test_leverage_is_smaller_than_raw_spread_when_the_gap_is_noisy():
    """Raw variance overstates separation: the gap moves for reasons that have
    nothing to do with him, and only his covariance with it counts."""
    n = 4000
    rng = np.random.default_rng(9)
    rows = {1: rng.normal(5, 6, n), 2: rng.normal(5, 6, n),
            10: rng.normal(5, 6, n)}
    st = _state([_rival(2, [2])])
    r = LG.differential_leverage(_Scen(rows, n), st, [1, 10], None)[0]
    assert r["leverage_points"] < r["own_std"], (
        "contribution to the GAP's spread must be below the player's own")


def test_a_player_every_rival_owns_is_not_a_differential():
    n = 500
    rows = {1: np.random.default_rng(2).normal(5, 5, n)}
    st = _state([_rival(2, [1])])
    assert LG.differential_leverage(_Scen(rows, n), st, [1], None) == []


def test_no_published_rival_squad_means_no_opinion():
    n = 500
    rows = {1: np.ones(n)}
    st = _state([LG.RivalEntry(entry_id=2, manager="Hidden", total=100)])
    assert LG.differential_leverage(_Scen(rows, n), st, [1], None) == []


# --- 3.9 ------------------------------------------------------------------

def test_a_tiny_league_still_targets_first():
    st = _state([_rival(i, []) for i in range(2, 8)])
    st.classification = LG.TINY
    assert ST.default_target(st) == 1


def test_a_twenty_four_person_league_targets_a_band_not_first_place():
    """`classify` calls anything up to thirty SMALL, so a 24-person work league
    was scored on "will you finish first": p_target came out at 0.001 and every
    option scored the same nothing, which cannot rank a decision."""
    st = _state([_rival(i, []) for i in range(2, 25)])
    assert ST.default_target(st) == 3


def test_the_target_never_depends_on_how_well_you_are_doing():
    """Choosing a band because it flatters the current position is how a metric
    stops measuring anything. Only the league's size may move it."""
    a = _state([_rival(i, []) for i in range(2, 25)])
    b = _state([_rival(i, []) for i in range(2, 25)])
    b.entries[0].total = 9999
    assert ST.default_target(a) == ST.default_target(b)


# --- 3.10 -----------------------------------------------------------------

def test_the_coarse_outlook_refuses_to_rank_the_two_chips_it_cannot_value():
    out = ST.coarse_chip_outlook(None, 3, 19, [1], [2])
    assert out["not_ranked"] == ["wildcard", "freehit"]
    assert "re-solved squad" in out["not_ranked_reason"]


def test_it_publishes_bands_and_never_a_point_estimate(conn):
    """A GW3 model must not assert that GW16 is worth 8.54 points."""
    out = ST.coarse_chip_outlook(conn, 1, 6, [1], [2])
    for block in out.get("by_chip", {}).values():
        for row in block["ranked"]:
            assert set(row) == {"gameweek", "band"}
            assert row["band"] in ST.COARSE_BANDS


def test_it_says_how_far_it_looked():
    out = ST.coarse_chip_outlook(None, 3, 19, [1], [2])
    assert out["assessed_to"] == 19
    assert "ORDERS gameweeks, does not price them" in out["method"]
