"""Cross-implementation parity for the live view.

The Live page now scores in the browser, because a static artifact regenerated
three times a day cannot tick while football is being played. That means the
same rules exist twice: `gaffer.live` (Python, the reference) and
`web/src/lib/live` (TypeScript, what you actually look at during a match).

Two implementations of one rulebook is exactly the failure this project has been
bitten by before — the solver's duplicated objective constants agreed for months
and then quietly did not. So neither side owns the truth on its own: both are
driven from `tests/fixtures/live/cases.json`, and both must produce the byte-same
`LiveState`.

Python is the reference. Regenerate the expected outputs deliberately, never
casually:

    GAFFER_REGEN_LIVE_FIXTURES=1 python -m pytest tests/test_live_parity.py

and read the diff before committing it — a change here is a change to what the
site tells you mid-match.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gaffer import live

CASES = Path(__file__).parent / "fixtures" / "live" / "cases.json"

# --------------------------------------------------------------------------
# The scenarios. Payload shapes match the real endpoints.
# --------------------------------------------------------------------------

KO = "2026-08-22T14:00:00Z"
NOW = "2026-08-22T15:00:00Z"

POSITIONS = {
    **{str(p): "GKP" for p in (1, 12)},
    **{str(p): "DEF" for p in (2, 3, 4, 13)},
    **{str(p): "MID" for p in (5, 6, 7, 8, 14)},
    **{str(p): "FWD" for p in (9, 10, 11, 15)},
}
# Two clubs, so a fixture state applies to a real set of players.
TEAM_OF = {str(p): (1 if p <= 8 else 2) for p in range(1, 16)}
NAMES = {str(p): f"P{p}" for p in range(1, 16)}
XI = list(range(1, 12))
BENCH = [12, 13, 14, 15]


def fixture(fid=1, team_h=1, team_a=2, minutes=0, started=False, finished=False,
            provisional=False, event=1, kickoff=KO, bps=None):
    raw = {
        "id": fid, "event": event, "team_h": team_h, "team_a": team_a,
        "minutes": minutes, "started": started, "finished": finished,
        "finished_provisional": provisional, "kickoff_time": kickoff,
        "stats": [],
    }
    if bps:
        raw["stats"] = [{
            "identifier": "bps",
            "h": [{"value": v, "element": k} for k, v in bps.items()],
            "a": [],
        }]
    return raw


def element(pid, minutes=0, points=0, bonus=None):
    """One row of ``event/{gw}/live/``.

    ``bonus`` is FPL's own per-element bonus figure and is left OUT of the row
    unless asked for, which is how the real payload behaves before FPL publishes
    one. It is the field that decides whether our BPS-derived bonus is a second
    copy of points already inside ``total_points``, and until A2 not one row in
    this file carried it.
    """
    stats = {"minutes": minutes, "total_points": points, "bps": 0}
    if bonus is not None:
        stats["bonus"] = bonus
    return {"id": pid, "stats": stats}


def squad(captain=9, vice=10, starting=None, bench=None):
    return {"starting": list(starting or XI), "bench": list(bench or BENCH),
            "captain": captain, "vice": vice}


def base(**over):
    case = {
        "gw": 1,
        "now": NOW,
        "as_of": NOW,
        "fixtures_payload": [fixture(1, started=True, minutes=60)],
        "live_payload": {"elements": [element(p, 90, 2) for p in range(1, 16)]},
        "squad": squad(),
        "positions": POSITIONS,
        "team_of": TEAM_OF,
        "names": NAMES,
        "predictions": {},
        "rivals": [],
        "entry_id": 1,
        "baseline": 0,
        "hits": 0,
        "active_chip": None,
    }
    case.update(over)
    return case


def blanked(pid, elements):
    """Replace a player's row with a played-and-scored-nothing row."""
    return [element(pid, 0, 0) if e["id"] == pid else e for e in elements]


FINISHED_FIXTURE = fixture(1, started=True, minutes=90, finished=True)
AWAITING = fixture(1, started=True, minutes=90, provisional=True)


def build_cases() -> list[dict]:
    live_els = [element(p, 90, 2) for p in range(1, 16)]
    return [
        {
            "name": "nothing has kicked off",
            "input": base(fixtures_payload=[fixture(1)], live_payload={}),
        },
        {
            "name": "no fixtures in this gameweek",
            "input": base(fixtures_payload=[], live_payload={}),
        },
        {
            "name": "squad unreadable before the deadline",
            "input": base(squad=None),
        },
        {
            "name": "in play, provisional bonus with a tie on top",
            # Two tied on 40 both take 3; the next takes 1 and no 2 is awarded.
            "input": base(
                fixtures_payload=[fixture(1, started=True, minutes=70,
                                          bps={5: 40, 6: 40, 7: 32, 8: 20})],
                live_payload={"elements": live_els},
            ),
        },
        {
            "name": "bonus already final is not double counted",
            "input": base(
                fixtures_payload=[fixture(1, started=True, minutes=90,
                                          finished=True,
                                          bps={5: 40, 6: 32, 7: 20})],
                live_payload={"elements": live_els},
            ),
        },
        {
            "name": "goalkeeper blanked, bench keeper came on",
            "input": base(
                fixtures_payload=[FINISHED_FIXTURE,
                                  fixture(2, team_h=2, team_a=3, started=True,
                                          minutes=90, finished=True)],
                live_payload={"elements": blanked(1, live_els)},
            ),
        },
        {
            "name": "captain blanked so the armband passes to the vice",
            "input": base(
                fixtures_payload=[FINISHED_FIXTURE,
                                  fixture(2, team_h=2, team_a=3, started=True,
                                          minutes=90, finished=True)],
                live_payload={"elements": blanked(9, live_els)},
            ),
        },
        {
            "name": "triple captain",
            "input": base(active_chip="3xc",
                          live_payload={"elements": live_els}),
        },
        {
            "name": "bench boost plays all fifteen and makes no substitutions",
            "input": base(active_chip="bboost",
                          fixtures_payload=[FINISHED_FIXTURE,
                                            fixture(2, team_h=2, team_a=3,
                                                    started=True, minutes=90,
                                                    finished=True)],
                          live_payload={"elements": blanked(3, live_els)}),
        },
        {
            "name": "predicted points for a player yet to kick off",
            "input": base(
                fixtures_payload=[fixture(1, started=True, minutes=60),
                                  fixture(2, team_h=2, team_a=3, kickoff=KO)],
                live_payload={"elements": [element(p, 90, 2) for p in range(1, 9)]
                              + [element(p, 0, 0) for p in range(9, 16)]},
                predictions={str(p): 3.5 for p in range(9, 16)},
            ),
        },
        {
            "name": "rivals, live table and the biggest swing",
            "input": base(
                fixtures_payload=[fixture(1, started=True, minutes=70,
                                          bps={5: 40, 6: 30})],
                live_payload={"elements": [
                    element(p, 90, 12 if p == 9 else 2) for p in range(1, 16)]},
                rivals=[{"entry_id": 2, "name": "Rival",
                         "starting": [p for p in XI if p != 9] + [15],
                         "bench": [12, 13, 14, 9], "captain": 1, "vice": 2,
                         "total": 40, "hits": 4, "active_chip": None}],
                baseline=50, hits=4,
            ),
        },
        {
            # A2. Every element row in this file carried minutes, total_points
            # and bps and nothing else, so no case had ever exercised the one
            # field that decides whether provisional bonus is counted twice —
            # the case named "bonus already final" tests the FIXTURE-state path,
            # not this one. That is how a real bug shipped: the guard landed in
            # Python while the browser went on double-counting, and parity
            # stayed green through all of it.
            #
            # Player 5 tops the BPS and his row already carries FPL's bonus, so
            # ours must not be added on top; player 6 is second on BPS with no
            # bonus field at all, so ours is the only figure there is. 5 wears
            # the armband, which is what doubled the error in GW2.
            "name": "bonus already in the live row is not counted twice",
            "input": base(
                squad=squad(captain=5, vice=6),
                fixtures_payload=[fixture(1, started=True, minutes=70,
                                          bps={5: 40, 6: 30, 7: 20})],
                live_payload={"elements": [
                    element(5, 70, 8, bonus=3),
                    element(6, 70, 5),
                    *[element(p, 90, 2) for p in range(1, 16)
                      if p not in (5, 6)],
                ]},
            ),
        },
        {
            # A1. FPL flips a fixture's ``finished`` only when the WHOLE event
            # is processed, so a played match sits at
            # (finished=False, finished_provisional=True) for as long as one
            # straggler is outstanding — nine of GW2's ten were still there on
            # 2026-08-31, three days after they were played. Its bonus is
            # settled and already inside ``total_points``, so recomputing it
            # from BPS files a second copy of the same points.
            "name": "a provisionally finished fixture has settled bonus",
            "input": base(
                fixtures_payload=[fixture(1, started=True, minutes=90,
                                          provisional=True,
                                          bps={5: 40, 6: 30, 7: 20})],
                live_payload={"elements": live_els},
            ),
        },
        {
            # A5. He is a member of his own mini-league, so his own entry comes
            # back among the standings. The table prepends a synthetic "You"
            # row, so he was published twice — and the duplicate is a rival at
            # distance ZERO from himself, so he was always his own closest
            # rival, every player scored identically for both squads, and
            # ``largest_swing`` came back null on every single run.
            #
            # Same football and same real rival as "rivals, live table and the
            # biggest swing" above: the swing here must be that case's swing,
            # not None.
            "name": "your own entry is not a rival to yourself",
            "input": base(
                fixtures_payload=[fixture(1, started=True, minutes=70,
                                          bps={5: 40, 6: 30})],
                live_payload={"elements": [
                    element(p, 90, 12 if p == 9 else 2) for p in range(1, 16)]},
                rivals=[{"entry_id": 1, "name": "You, from the standings",
                         "starting": list(XI), "bench": list(BENCH),
                         "captain": 9, "vice": 10,
                         "total": 50, "hits": 4, "active_chip": None},
                        {"entry_id": 2, "name": "Rival",
                         "starting": [p for p in XI if p != 9] + [15],
                         "bench": [12, 13, 14, 9], "captain": 1, "vice": 2,
                         "total": 40, "hits": 4, "active_chip": None}],
                baseline=50, hits=4,
            ),
        },
        {
            "name": "abandoned fixture four hours after kick-off",
            "input": base(
                now="2026-08-22T19:00:00Z", as_of="2026-08-22T19:00:00Z",
                fixtures_payload=[fixture(1, started=True, minutes=30)],
                live_payload={"elements": live_els},
            ),
        },
        {
            # Team 2 — players 9-15, which is three of the XI and the whole
            # bench — has no fixture at all. Every case above gives both clubs a
            # match, and that is exactly how a gameweek that never ends went
            # unnoticed: with no fixtures, "all his fixtures are over" was false
            # forever, so the blanked captain kept the armband.
            "name": "a blank gameweek ends, and the armband moves",
            "input": base(
                squad=squad(captain=9, vice=5),
                fixtures_payload=[fixture(1, team_h=1, team_a=3, started=True,
                                          minutes=90, finished=True)],
                live_payload={"elements": [element(p, 90, 2) for p in range(1, 9)]
                              + [element(p, 0, 0) for p in range(9, 16)]},
                predictions={str(p): 3.5 for p in range(9, 16)},
            ),
        },
        {
            # Team 1 plays twice, one match done and one still to come. The
            # projection is a gameweek aggregate covering both, so half of it is
            # still ahead of them — reading their aggregate minutes as "done"
            # wrote all of it off.
            "name": "a double gameweek splits the projection across both fixtures",
            "input": base(
                fixtures_payload=[
                    fixture(1, started=True, minutes=90, finished=True),
                    fixture(2, team_h=1, team_a=3),
                ],
                live_payload={"elements": live_els},
                predictions={str(p): 4.0 for p in range(1, 16)},
            ),
        },
        {
            # B5. Aggregates were all `assemble` ever published about a rival:
            # `rivals` is a table of totals, `strategy.json`'s `owners` is a
            # count rather than a roster, and no table anywhere holds a rival's
            # picks. So "how are they doing" — asked as often as "how am I
            # doing" — could only be answered with a number nobody could check,
            # and with no way to see whose players had not kicked off.
            #
            # Three rivals at once, because the multiplier on a published row is
            # a fact about the MANAGER and not about the player. All three own
            # the same fifteen as each other and as you, and player 9 is the
            # only one with real points, so each row's `product` is visibly that
            # manager's own multiple of the very same football: 21 captains
            # someone else, 22 plays Bench Boost (all fifteen score and nothing
            # is substituted), 23 plays Triple Captain.
            "name": "every rival's fifteen are published with the multiplier "
                    "that scores them",
            "input": base(
                fixtures_payload=[fixture(1, started=True, minutes=70,
                                          bps={5: 40, 6: 30})],
                live_payload={"elements": [
                    element(p, 90, 12 if p == 9 else 2) for p in range(1, 16)]},
                rivals=[
                    {"entry_id": 21, "name": "Other armband",
                     "starting": list(XI), "bench": list(BENCH),
                     "captain": 10, "vice": 11, "total": 40, "hits": 0,
                     "active_chip": None},
                    {"entry_id": 22, "name": "Bench Boost",
                     "starting": list(XI), "bench": list(BENCH),
                     "captain": 9, "vice": 10, "total": 38, "hits": 4,
                     "active_chip": "bboost"},
                    {"entry_id": 23, "name": "Triple Captain",
                     "starting": list(XI), "bench": list(BENCH),
                     "captain": 9, "vice": 10, "total": 36, "hits": 0,
                     "active_chip": "3xc"},
                ],
                baseline=50, hits=4,
            ),
        },
        {
            # B5. A rival's eleven is not his PICKED eleven once a starter
            # blanks, and neither his substitutions nor a moved armband can be
            # derived from anything else the artifact publishes. Player 2 (DEF)
            # and the captain, player 9, both finished on zero minutes: 13 comes
            # on for one, 14 for the other, and the armband passes to the vice.
            # Every one of those is legible from his own `autosubs` block and
            # from which of his rows carries a multiplier above one.
            "name": "a rival's autosubs and moved armband reach his published rows",
            "input": base(
                fixtures_payload=[FINISHED_FIXTURE,
                                  fixture(2, team_h=2, team_a=3, started=True,
                                          minutes=90, finished=True)],
                live_payload={"elements": blanked(2, blanked(9, live_els))},
                rivals=[{"entry_id": 31, "name": "Autosubbed",
                         "starting": list(XI), "bench": list(BENCH),
                         "captain": 9, "vice": 10, "total": 30, "hits": 0,
                         "active_chip": None}],
                baseline=50, hits=0,
            ),
        },
        {
            # B5. The question this whole block exists for. A rival's total
            # means nothing on its own: nine points behind with three of his men
            # still to kick off is a different afternoon from nine behind with
            # none, and until now the artifact could only ever say how MANY of
            # his were left, never which. Team 2 has not kicked off, so seven of
            # his fifteen are still to come and each carries his share of the
            # projection.
            "name": "a rival's players still to kick off are named, not just counted",
            "input": base(
                fixtures_payload=[fixture(1, started=True, minutes=60),
                                  fixture(2, team_h=2, team_a=3, kickoff=KO)],
                live_payload={"elements": [element(p, 90, 2) for p in range(1, 9)]
                              + [element(p, 0, 0) for p in range(9, 16)]},
                predictions={str(p): 3.5 for p in range(9, 16)},
                rivals=[{"entry_id": 51, "name": "Seven still to come",
                         "starting": list(XI), "bench": list(BENCH),
                         "captain": 9, "vice": 10, "total": 40, "hits": 0,
                         "active_chip": None}],
                baseline=50, hits=0,
            ),
        },
        {
            # B5. A rival can hold a player this build knows nothing about — an
            # id the live endpoint never carried, or a signing the ingest has
            # not seen. He has no live state, so no row is published for him and
            # none is invented. An unknown player arriving as a zero would read
            # as a man who played and scored nothing, which is a different and
            # wrong claim; his absence from the rows against a squad of fifteen
            # is the honest one.
            "name": "a rival's unknown player produces no row rather than a zero",
            "input": base(
                fixtures_payload=[fixture(1, started=True, minutes=70)],
                live_payload={"elements": live_els},
                rivals=[{"entry_id": 41, "name": "Holds a stranger",
                         "starting": [p for p in XI if p != 11] + [99],
                         "bench": list(BENCH), "captain": 9, "vice": 10,
                         "total": 30, "hits": 0, "active_chip": None}],
                baseline=50, hits=0,
            ),
        },
    ]


def _to_python(inp: dict) -> dict:
    """JSON keys are strings; `gaffer.live` speaks element ids."""
    return {
        "gw": inp["gw"],
        "live_payload": inp["live_payload"],
        "fixtures_payload": inp["fixtures_payload"],
        "squad": inp["squad"],
        "positions": {int(k): v for k, v in inp["positions"].items()},
        "team_of": {int(k): v for k, v in inp["team_of"].items()},
        "now": datetime.fromisoformat(inp["now"].replace("Z", "+00:00")).astimezone(UTC),
        "predictions": {int(k): float(v) for k, v in (inp["predictions"] or {}).items()},
        "rivals": inp["rivals"],
        "names": {int(k): v for k, v in (inp["names"] or {}).items()},
        "entry_id": inp["entry_id"],
        "baseline": inp["baseline"],
        "hits": inp["hits"],
        "active_chip": inp["active_chip"],
        "as_of": inp["as_of"],
    }


def _stable(obj):
    """Round-trip through JSON so both sides compare the same value space."""
    return json.loads(json.dumps(obj, sort_keys=True))


def _load() -> list[dict]:
    """Cases for parametrisation, at collection time.

    Returns [] rather than skipping when the file is absent: this runs at import,
    and a module-level skip would also skip the regeneration test that creates
    the file in the first place.
    """
    if not CASES.exists():
        return []
    return json.loads(CASES.read_text(encoding="utf-8"))["cases"]


@pytest.mark.skipif(os.environ.get("GAFFER_REGEN_LIVE_FIXTURES") != "1",
                    reason="regeneration is deliberate, not automatic")
def test_regenerate_shared_fixtures():
    cases = []
    for case in build_cases():
        expected = _stable(live.assemble(**_to_python(case["input"])))
        cases.append({**case, "expected": expected})
    CASES.parent.mkdir(parents=True, exist_ok=True)
    CASES.write_text(
        json.dumps({
            "note": "Generated from gaffer.live, the reference implementation. "
                    "Read tests/test_live_parity.py before regenerating.",
            "cases": cases,
        }, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")


def test_the_case_file_covers_the_behaviour_that_matters():
    cases = _load()
    assert cases, (
        f"{CASES} is missing. Generate it with "
        "GAFFER_REGEN_LIVE_FIXTURES=1 python -m pytest tests/test_live_parity.py")
    names = {c["name"] for c in cases}
    for needle in ("kicked off", "provisional bonus", "armband", "triple captain",
                   "bench boost", "swing", "yet to kick off",
                   "in the live row", "provisionally finished", "own entry",
                   "multiplier that scores them", "moved armband",
                   "no row rather than a zero", "still to kick off are named"):
        assert any(needle in n for n in names), f"no case covers {needle!r}"


def _element_rows(cases):
    return [e.get("stats") or {}
            for c in cases
            for e in (c["input"].get("live_payload") or {}).get("elements") or []]


def test_the_case_file_exercises_the_element_bonus_field():
    """A2. The parity suite was blind to the field that decides double-counting.

    Not one element row in this file carried a ``bonus`` — the case named for
    bonus tests the FIXTURE-state path, not the element one. So the guard could
    land in Python, the browser could go on adding a second copy of the same
    points, and parity would stay green. It did exactly that.
    """
    rows = _element_rows(_load())
    assert rows, "no element rows at all"
    assert any(row.get("bonus") for row in rows), \
        "no element row carries a non-zero `bonus`, so nothing pins the guard"


def test_the_case_file_covers_the_manager_being_in_his_own_league():
    """A5. The duplicate that emptied `largest_swing` is a shape, not a value.

    A rivals list that never contains the manager cannot catch a filter that
    stops working, and no case here had ever contained him.
    """
    assert any(
        any(r.get("entry_id") == c["input"].get("entry_id")
            for r in c["input"].get("rivals") or [])
        for c in _load()
    ), "no case supplies the manager's own entry among the rivals"


@pytest.mark.parametrize("case", _load(), ids=lambda c: c["name"])
def test_python_still_produces_the_agreed_output(case):
    """The reference implementation has not drifted from the shared contract."""
    got = _stable(live.assemble(**_to_python(case["input"])))
    assert got == case["expected"]


def _rival_rows(cases):
    return [r for c in cases for r in (c["expected"].get("rival_squads") or [])]


def test_every_published_rival_row_reconciles_to_his_total():
    """B5. A rival's total stops being a number you have to take on trust.

    The whole reason to spend bytes on 105 rows is that they add up: multiply
    each of a manager's players by the multiplier that scores him, sum, subtract
    the hits, and you must land exactly on the total published beside his name.
    An `owners` count could never do this, and neither could a rival total read
    off the standings — which is how a live tool ends up asserting a score
    nobody, including it, can check.

    Asserted over the generated file rather than inside the code that generates
    it, so a change to either implementation has to survive it.
    """
    rows = _rival_rows(_load())
    assert rows, "no case publishes a rival's fifteen"
    for rival in rows:
        products = sum(p["product"] for p in rival["players"])
        assert round(products - rival["hits"], 2) == rival["gw_points"], (
            rival["name"], products, rival["hits"], rival["gw_points"])


def test_the_case_file_covers_the_ways_a_rival_row_can_differ():
    """B5. The multiplier on a row is a fact about the manager, not the player.

    Three managers can own one player and score him three different ways, and a
    case file where every rival plays a plain captain would pin none of it.
    """
    rows = _rival_rows(_load())
    mults = {p["multiplier"] for r in rows for p in r["players"]}
    assert {0, 1, 2, 3} <= mults, f"no rival row carries every multiplier: {mults}"
    assert any(r["autosubs"]["captain_source"] == "vice" for r in rows), \
        "no rival case moves the armband"
    assert any(r["autosubs"]["subs_in"] for r in rows), \
        "no rival case makes a substitution"
    assert any(len(r["players"]) < 15 for r in rows), \
        "no rival case holds a player with no live state"
    assert any(r["differential"] for r in rows), \
        "no rival case differs from you at all"
    assert any(p["yet_to_play"] for r in rows for p in r["players"]), \
        "no rival case names a player who has not kicked off — the whole point"
    assert any(p["predicted"] for r in rows for p in r["players"]), \
        "no rival row carries a projection for football still to be played"
