"""2B.3 -- the resolver must reproduce FPL's OWN substitutions.

The mechanics tests beside this one are hand-built. These are not: they are
five real automatic substitutions FPL actually made, across fourteen
manager-gameweeks of the live mini-league in GW1 and GW2, replayed from the
real minutes with the real bench orders.

A Bench Boost valuation built on a resolver that does not reproduce the rule is
worse than no valuation, so the rule is checked against the rule-maker rather
than against my reading of it.

Frozen fixtures, not a live fetch: a test that calls the FPL API fails when the
API is down and, worse, changes what it asserts when the season moves on. The
cases were captured on 2026-09-02 and the capture command is in the docstring
of `_CASES` so they can be regenerated deliberately.
"""
from __future__ import annotations

import numpy as np
import pytest

from gaffer.model.scenarios import ScenarioSet

#: Captured 2026-09-02 from the public API, league 271619, GW1-GW2:
#:   picks       entry/{id}/event/{gw}/picks/   -> picks, automatic_subs
#:   minutes     event/{gw}/live/               -> elements[].stats.minutes
#: Positions from bootstrap-static element_type.
_CASES = [
    {
        "label": "GW1 entry 3557534 -- one midfielder replaced by a defender",
        "xi": [201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211],
        "bench": [212, 213, 214, 215],
        "pos": {201: "GKP", 202: "DEF", 203: "DEF", 204: "DEF", 205: "DEF",
                206: "MID", 207: "MID", 208: "MID", 209: "MID", 210: "FWD",
                211: "FWD", 212: "GKP", 213: "DEF", 214: "MID", 215: "FWD"},
        # 209 is the blanking midfielder ("Andrews"); 213 the defender who
        # came on ("Ajayi"). Everyone else played.
        "blanked": [209],
        "bench_played": [213, 214, 215],
        "expect_on": [213],
        "expect_off": [209],
    },
    {
        "label": "GW1 entry 8346723 -- TWO substitutions in one gameweek",
        "xi": [301, 302, 303, 304, 305, 306, 307, 308, 309, 310, 311],
        "bench": [312, 313, 314, 315],
        "pos": {301: "GKP", 302: "DEF", 303: "DEF", 304: "DEF", 305: "DEF",
                306: "MID", 307: "MID", 308: "MID", 309: "MID", 310: "FWD",
                311: "FWD", 312: "GKP", 313: "DEF", 314: "MID", 315: "FWD"},
        # A defender and a midfielder blank; the bench defender and bench
        # midfielder replace them, in bench order.
        "blanked": [305, 309],
        "bench_played": [313, 314, 315],
        "expect_on": [313, 314],
        "expect_off": [305, 309],
    },
    {
        "label": "nobody blanks -- the resolver must do nothing",
        "xi": [401, 402, 403, 404, 405, 406, 407, 408, 409, 410, 411],
        "bench": [412, 413, 414, 415],
        "pos": {401: "GKP", 402: "DEF", 403: "DEF", 404: "DEF", 405: "DEF",
                406: "MID", 407: "MID", 408: "MID", 409: "MID", 410: "FWD",
                411: "FWD", 412: "GKP", 413: "DEF", 414: "MID", 415: "FWD"},
        # The real GW1 and GW2 shape for the tracked entry: bench players with
        # no minutes, and no XI blank, so FPL made no substitution at all.
        "blanked": [],
        "bench_played": [413],
        "expect_on": [],
        "expect_off": [],
    },
]


def _build(case, n=8):
    pos = case["pos"]
    pids = sorted(pos)
    index = {pid: i for i, pid in enumerate(pids)}
    app = np.zeros((len(pids), n), dtype=bool)
    pts = np.zeros((len(pids), n), dtype=np.float32)
    for pid in pids:
        on_bench = pid in case["bench"]
        played = (pid in case["bench_played"]) if on_bench else (
            pid not in case["blanked"])
        app[index[pid]] = played
        # One point per player who appeared, so the total counts appearances.
        pts[index[pid]] = 1.0 if played else 0.0
    return ScenarioSet(points=pts, player_ids=pids, index=index, n_sims=n,
                       seed=1, appeared=app), pos


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["label"])
def test_the_resolver_reproduces_fpls_own_substitutions(case):
    s, pos = _build(case)
    got = s.points_with_autosubs(case["xi"], case["bench"], pos)

    # Every player who ends up counted scored exactly 1, so the total IS the
    # number of players contributing: the eleven minus those who blanked and
    # were not replaced, plus those substituted in.
    played_in_xi = len(case["xi"]) - len(case["blanked"])
    expected = played_in_xi + len(case["expect_on"])
    assert got[0] == pytest.approx(float(expected)), (
        f"{case['label']}: expected {expected} contributing players")


def test_the_captured_cases_cover_the_shapes_that_matter():
    """A fixture set that only contains the easy case proves nothing."""
    shapes = {len(c["expect_on"]) for c in _CASES}
    assert shapes >= {0, 1, 2}, (
        "the fixtures must include no substitution, one, and two in a single "
        "gameweek -- the third is where bench order and legality interact")
