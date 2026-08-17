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


def element(pid, minutes=0, points=0):
    return {"id": pid, "stats": {"minutes": minutes, "total_points": points,
                                 "bps": 0}}


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
                   "bench boost", "swing", "yet to kick off"):
        assert any(needle in n for n in names), f"no case covers {needle!r}"


@pytest.mark.parametrize("case", _load(), ids=lambda c: c["name"])
def test_python_still_produces_the_agreed_output(case):
    """The reference implementation has not drifted from the shared contract."""
    got = _stable(live.assemble(**_to_python(case["input"])))
    assert got == case["expected"]
