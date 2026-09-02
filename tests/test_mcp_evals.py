"""Run the checked-in MCP evaluation set against the real tools.

The eval checks *facts*, not prose. Asking "does the answer read well" grades a
confident invention as highly as a correct one; asking "does the route exist,
does the result carry these fields, and does `ep_next_blend.fitted` equal false"
does not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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
