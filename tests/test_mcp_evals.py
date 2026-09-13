"""Run the checked-in MCP evaluation set against the real tools.

The eval checks *facts*, not prose. Asking "does the answer read well" grades a
confident invention as highly as a correct one; asking "does the route exist,
does the result carry these fields, and does `ep_next_blend.fitted` equal false"
does not.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gaffer import config, live
from gaffer import mcp_server as M

EVALS = json.loads(
    (Path(__file__).parent / "mcp_evals.json").read_text(encoding="utf-8"))
CASES = EVALS["cases"]

#: Arguments a client would plausibly send for each tool in a case.
ARGS: dict[str, dict[str, Any]] = {
    "find_players": {"query": "a", "limit": 5},
    "get_player_outlook": {"player": "1"},
    "compare_players": {"players": ["1", "2"]},
}


def _dig(blob: Any, path: str) -> Any:
    """`a.0.b` -> blob['a'][0]['b'], or the sentinel when absent."""
    cur = blob
    for part in path.split("."):
        if isinstance(cur, list):
            if not part.isdigit() or int(part) >= len(cur):
                return _MISSING
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        else:
            return _MISSING
    return cur


_MISSING = object()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_eval_case(case):
    check_case(case)


def check_case(case: dict[str, Any]) -> None:
    """One eval case, against whatever data directory the tools resolve to."""
    for tool in case["expect_tools"]:
        assert tool in M.TOOLS, f"{case['id']} routes to a tool that does not exist"

    tool = case["expect_tools"][-1]
    result = M.call(tool, **ARGS.get(tool, {}))

    assert result["status"] in case["allowed_statuses"], (
        f"{case['id']}: {tool} returned {result['status']!r}, which this case "
        f"does not allow ({case['allowed_statuses']}). "
        f"{result.get('detail', '')}")

    if result["status"] != "ok":
        # A refusal must still be legible: the model has to be able to say why.
        assert result.get("detail") or result.get("unavailable_reason")
        return

    for field in case.get("require_fields", []):
        # Dotted, because the answer to "what should I do?" is now one
        # canonical object rather than a dozen sibling keys (4.5). A case that
        # named a top-level key still reads the same; one that names
        # `card.strength.label` follows it into the card.
        assert _dig(result, field) is not _MISSING, (
            f"{case['id']}: {tool} result lacks {field!r}")

    for path, expected in (case.get("require_facts") or {}).items():
        got = _dig(result, path)
        assert got is not _MISSING, f"{case['id']}: {path} is absent"
        if expected == "present":
            continue
        assert got == expected, f"{case['id']}: {path} is {got!r}, expected {expected!r}"

    for name, decision in (case.get("require_candidates") or {}).items():
        cands = {c["candidate"]: c["decision"]
                 for c in _dig(result, "model_candidates.candidates") or []}
        assert cands.get(name) == decision, (
            f"{case['id']}: candidate {name} is {cands.get(name)!r}, "
            f"expected {decision!r}")


def test_every_named_tool_exists_and_every_tool_is_covered():
    named = {t for c in CASES for t in c["expect_tools"]}
    assert named <= set(M.TOOLS), f"unknown tools in evals: {named - set(M.TOOLS)}"
    missing = set(M.TOOLS) - named
    assert not missing, f"no eval covers {sorted(missing)}"


def test_the_evals_are_about_facts_not_prose():
    """A guard on the guard: an eval with no factual assertion checks nothing."""
    for case in CASES:
        assert case.get("require_fields") or case.get("require_facts") \
            or case.get("require_candidates") or case.get("must_not_invent"), \
            f"{case['id']} asserts nothing"


# ---------------------------------------------------------------------------
# W16 -- every eval that reads live.json must hold in EVERY live state
# ---------------------------------------------------------------------------
#
# `test_eval_case` above runs against the committed artifacts, so it only ever
# sees the state the last refresh happened to publish. On 2026-09-12 that was
# the ninety minutes between the GW4 deadline and the first kickoff:
# `live.json` was `available: false`, `get_gameweek_brief` answered `ok` with
# `you: null`, and the `gameweek-brief` case failed on every refresh for more
# than a day. The case had passed every run before it only because no run had
# yet published that state.
#
# So the live evals are also run here against every state `live.assemble` can
# produce, built from payloads rather than read from `data/`. A state no eval
# has seen is a state the feed can go dark in.

_NOW = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
_XI = list(range(1, 12))
_BENCH = [12, 13, 14, 15]
_POS = {
    **{p: "GKP" for p in (1, 12)},
    **{p: "DEF" for p in (2, 3, 4, 13)},
    **{p: "MID" for p in (5, 6, 7, 8, 14)},
    **{p: "FWD" for p in (9, 10, 11, 15)},
}
_ME, _RIVAL = 1066421, 3557534


def _fixture(*, started: bool, finished: bool = False, minutes: int = 0) -> dict:
    return {"id": 1, "event": 4, "team_h": 1, "team_a": 2, "minutes": minutes,
            "started": started, "finished": finished,
            "finished_provisional": finished,
            "kickoff_time": "2026-09-12T14:00:00Z", "stats": []}


def _assemble(**over: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        gw=4,
        live_payload={"elements": [
            {"id": p, "stats": {"minutes": 90, "total_points": 2, "bps": 10}}
            for p in _XI + _BENCH]},
        fixtures_payload=[_fixture(started=True, minutes=70)],
        squad={"starting": _XI, "bench": _BENCH, "captain": 9, "vice": 10},
        positions=_POS, team_of={p: 1 for p in _XI + _BENCH}, now=_NOW,
        names={p: f"P{p}" for p in _XI + _BENCH}, entry_id=_ME, baseline=216,
        hits=0, as_of=_NOW.isoformat(),
        rivals=[{"entry_id": _RIVAL, "name": "Rival", "starting": _XI,
                 "bench": _BENCH, "captain": 10, "vice": 9, "total": 212,
                 "hits": 0, "active_chip": None}],
    )
    kwargs.update(over)
    return live.assemble(**kwargs)


#: Every state a published `live.json` can be in, keyed by its
#: `unavailable_reason` where it has one.
LIVE_STATES = {
    live.UNAVAILABLE_NO_GAMEWEEK: lambda: _assemble(fixtures_payload=[]),
    live.UNAVAILABLE_NOT_STARTED: lambda: _assemble(
        fixtures_payload=[_fixture(started=False)]),
    live.UNAVAILABLE_NO_SQUAD: lambda: _assemble(squad=None),
    live.UNAVAILABLE_NO_LIVE_DATA: lambda: _assemble(live_payload=None),
    "in_play": lambda: _assemble(),
    "in_play_without_a_league": lambda: _assemble(rivals=[]),
    "all_finished": lambda: _assemble(
        fixtures_payload=[_fixture(started=True, finished=True, minutes=90)]),
}

#: Tools whose answer depends on `live.json`, found from their source so a new
#: one cannot join without being run through every state.
LIVE_TOOLS = sorted(name for name, fn in M.TOOLS.items()
                    if '"live.json"' in inspect.getsource(fn))
LIVE_CASES = [c for c in CASES if c["expect_tools"][-1] in LIVE_TOOLS]


def _publish_live_state(tmp_path: Path, monkeypatch, state: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    meta = {"season": "2026-27", "generated_at": _NOW.isoformat(),
            "deadline": "2026-09-12T12:30:00+00:00", "current_gw": "5",
            "last_finished_gw": "3", "projection_event": "5",
            "squad_source_event": "4", "entry_id": _ME}
    (data / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (data / "strategy.json").write_text(json.dumps({"leagues": []}),
                                        encoding="utf-8")
    (data / "live.json").write_text(json.dumps(LIVE_STATES[state]()),
                                    encoding="utf-8")
    monkeypatch.setenv("GAFFER_DATA_DIR", str(data))
    config.reload_paths()


@pytest.mark.parametrize("state", sorted(LIVE_STATES))
@pytest.mark.parametrize("case", LIVE_CASES, ids=[c["id"] for c in LIVE_CASES])
def test_a_live_eval_holds_in_every_live_state(case, state, tmp_path, monkeypatch):
    _publish_live_state(tmp_path, monkeypatch, state)
    check_case(case)


def test_the_matrix_covers_every_state_live_can_publish():
    declared = {value for name, value in vars(live).items()
                if name.startswith("UNAVAILABLE_")}
    assert declared, "live.py no longer names its unavailable states"
    missing = declared - set(LIVE_STATES)
    assert not missing, (
        f"live.py can publish {sorted(missing)} and no eval has been run "
        "against it; add a builder to LIVE_STATES")


def test_the_matrix_found_the_live_tools():
    """A guard on the discovery: if it silently matched nothing, the matrix
    above would pass by running zero cases."""
    assert {"get_gameweek_brief", "get_live_gameweek",
            "get_live_scorecard"} <= set(LIVE_TOOLS)
    assert {c["id"] for c in LIVE_CASES} >= {"gameweek-brief", "live-final",
                                            "live-recompute"}


def test_the_brief_between_deadline_and_kickoff_says_why_it_cannot_answer(
        tmp_path, monkeypatch):
    """The exact artifact published at 2026-09-12T13:51Z."""
    _publish_live_state(tmp_path, monkeypatch, live.UNAVAILABLE_NOT_STARTED)
    r = M.call("get_gameweek_brief")
    assert r["status"] == M.STATUS_UNAVAILABLE, (
        "an answer to 'how am I doing?' with no position in it is not ok")
    assert r["unavailable_reason"] == live.UNAVAILABLE_NOT_STARTED
    assert r["detail"] and r["where_to_look"]
    assert "you" not in r


def test_an_available_brief_always_carries_a_position(tmp_path, monkeypatch):
    _publish_live_state(tmp_path, monkeypatch, "in_play")
    r = M.call("get_gameweek_brief")
    assert r["status"] == "ok"
    assert r["you"]["position"] in (1, 2)
    assert sum(1 for row in r["table"] if row["you"]) == 1
