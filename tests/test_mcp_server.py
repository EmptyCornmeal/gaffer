"""The MCP server is an interface, and its authority boundaries are the feature.

A conversational front door to a decision engine is a place where "read-only"
has to be enforced rather than intended, because the caller is a language model
and the arguments are whatever it decides to send. So these tests spend most of
their effort on what the server *cannot* do: no arbitrary SQL, no filesystem
path, no URL, no shell, no write, no network, no notification.

The rest check that every failure is a named state. A tool that returns an empty
success when the season has not started teaches a model to invent the answer.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from pathlib import Path

import pytest

from gaffer import config
from gaffer import mcp_server as M

SRC = Path(M.__file__)


# --- every tool answers in the same shape ------------------------------------

@pytest.mark.parametrize("name", sorted(M.TOOLS))
def test_every_tool_returns_a_recognised_envelope(name):
    args = {"find_players": {"query": "a", "limit": 3},
            "get_player_outlook": {"player": "1"},
            "compare_players": {"players": ["1", "2"]}}.get(name, {})
    r = M.call(name, **args)
    assert r["mcp_schema_version"] == M.MCP_SCHEMA_VERSION
    assert r["status"] in M.ALL_STATUSES
    if r["status"] == M.STATUS_OK:
        assert "source_artifact" in r and "freshness" in r
        assert "season" in r


@pytest.mark.parametrize("name", sorted(M.TOOLS))
def test_no_tool_ever_raises(name):
    """A stack trace reaching an MCP client is worse than a stated failure."""
    r = M.call(name)  # deliberately no arguments, including where required
    assert isinstance(r, dict) and r["status"] in M.ALL_STATUSES


def test_an_unknown_tool_is_a_named_failure():
    r = M.call("drop_everything")
    assert r["status"] == M.STATUS_INVALID
    assert "tools" in r


def test_never_a_silent_empty_success():
    """Every 'ok' carries something; every non-ok says why."""
    for name in sorted(M.TOOLS):
        args = {"find_players": {"query": "zzzzzzzz"},
                "get_player_outlook": {"player": "1"},
                "compare_players": {"players": ["1", "2"]}}.get(name, {})
        r = M.call(name, **args)
        if r["status"] != M.STATUS_OK:
            assert r.get("detail") or r.get("unavailable_reason"), \
                f"{name} failed without saying why"


def test_repeated_calls_are_deterministic():
    a = M.call("get_model_evidence")
    b = M.call("get_model_evidence")
    for k in ("status", "schema_version", "season_tested", "model_candidates"):
        assert a.get(k) == b.get(k)


def test_every_result_agrees_with_the_published_season():
    declared = json.loads(
        (config.DATA_DIR / "meta.json").read_text(encoding="utf-8"))["season"]
    for name in sorted(M.TOOLS):
        args = {"find_players": {"query": "a"},
                "get_player_outlook": {"player": "1"},
                "compare_players": {"players": ["1", "2"]}}.get(name, {})
        r = M.call(name, **args)
        if r.get("season") is not None:
            assert r["season"] == declared, f"{name} reports another season"


# --- failure states are distinct ---------------------------------------------

def test_a_missing_artifact_is_distinguished_from_an_empty_one(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "data_dir", lambda: tmp_path)
    r = M.call("get_weekly_decision")
    assert r["status"] == M.STATUS_MISSING
    assert "pipeline" in r["detail"]


def test_a_malformed_artifact_is_reported_as_malformed(tmp_path, monkeypatch):
    (tmp_path / "meta.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(M, "data_dir", lambda: tmp_path)
    r = M.call("gaffer_status")
    assert r["status"] == M.STATUS_MALFORMED


def test_a_future_schema_is_refused_rather_than_guessed(tmp_path, monkeypatch):
    (tmp_path / "meta.json").write_text(
        json.dumps({"season": "2026-27", "generated_at": "2026-08-07T00:00:00+00:00"}),
        encoding="utf-8")
    (tmp_path / "backtest.json").write_text(
        json.dumps({"schema_version": 999}), encoding="utf-8")
    monkeypatch.setattr(M, "data_dir", lambda: tmp_path)
    r = M.call("get_model_evidence")
    assert r["status"] == M.STATUS_UNSUPPORTED


def test_a_stale_artifact_is_flagged_not_hidden(tmp_path, monkeypatch):
    (tmp_path / "meta.json").write_text(
        json.dumps({"season": "2026-27",
                    "generated_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
    monkeypatch.setattr(M, "data_dir", lambda: tmp_path)
    r = M.call("gaffer_status")
    assert r["freshness"]["stale"] is True
    assert r["freshness"]["reason"]


def test_data_that_is_legitimately_unavailable_says_so():
    """Pre-season there is no review. That is an answer, not an error."""
    r = M.call("get_decision_review")
    assert r["status"] in (M.STATUS_OK, M.STATUS_UNAVAILABLE)
    if r["status"] == M.STATUS_UNAVAILABLE:
        assert "gameweek" in r["detail"]


# --- player resolution --------------------------------------------------------

def test_an_unknown_player_is_not_found():
    r = M.call("get_player_outlook", player="Zzzzzz Nobody")
    assert r["status"] == M.STATUS_NOT_FOUND


def test_an_ambiguous_name_lists_candidates_rather_than_picking_one():
    """Guessing between two players is the one thing worse than failing."""
    r = M.call("get_player_outlook", player="a")
    assert r["status"] == M.STATUS_AMBIGUOUS
    assert len(r["candidates"]) >= 2
    assert all("id" in c and "name" in c for c in r["candidates"])


def test_results_are_bounded():
    r = M.call("find_players", query="a", limit=9999)
    assert len(r["players"]) <= M.MAX_SEARCH_RESULTS


def test_an_over_long_query_is_refused():
    r = M.call("find_players", query="x" * (M.MAX_QUERY_LENGTH + 1))
    assert r["status"] == M.STATUS_INVALID


def test_compare_is_bounded_at_both_ends():
    assert M.call("compare_players", players=["1"])["status"] == M.STATUS_INVALID
    assert M.call("compare_players",
                  players=[str(i) for i in range(1, 9)])["status"] == M.STATUS_INVALID


def test_compare_reports_differences_and_recommends_nothing():
    r = M.call("compare_players", players=["1", "2"])
    if r["status"] != M.STATUS_OK:
        pytest.skip("player ids 1/2 absent from this artifact set")
    assert "differences" in r
    blob = json.dumps(r).lower()
    for word in ("you should", "recommend", "better buy", "must own"):
        assert word not in blob


# --- authority boundaries -----------------------------------------------------

def test_the_database_is_opened_read_only():
    src = inspect.getsource(M.read_only_db)
    assert "mode=ro" in src and "uri=True" in src
    if not Path(config.DB_PATH).exists():
        pytest.skip("no local database")
    conn = M.read_only_db()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE mcp_should_not_exist (x INT)")
    finally:
        conn.close()


def test_nothing_in_the_server_can_run_a_shell_or_open_a_socket():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    banned_modules = {"subprocess", "os", "socket", "shutil", "httpx",
                      "requests", "urllib", "anthropic"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] not in banned_modules, \
                    f"mcp_server imports {a.name}"
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            assert node.module.split(".")[0] not in banned_modules, \
                f"mcp_server imports from {node.module}"


def test_no_tool_takes_a_path_a_url_or_raw_sql():
    """The only free text a caller controls is a player-name fragment."""
    for name, fn in M.TOOLS.items():
        for param in inspect.signature(fn).parameters:
            assert param not in ("path", "file", "url", "sql", "query_sql",
                                 "command", "cmd", "db", "data_dir"), \
                f"{name} exposes {param}"


def test_paths_come_from_config_not_from_the_caller(tmp_path, monkeypatch):
    """Where the client launches the server must not change what it reads."""
    monkeypatch.chdir(tmp_path)
    assert M.data_dir() == Path(config.DATA_DIR)
    assert M.data_dir() != tmp_path


def test_a_traversal_attempt_cannot_reach_outside_the_data_directory():
    for evil in ("../../etc/passwd", "..\\..\\windows\\win.ini",
                 "/etc/passwd", "C:/Windows/win.ini"):
        r = M.call("get_player_outlook", player=evil)
        assert r["status"] in (M.STATUS_NOT_FOUND, M.STATUS_INVALID,
                               M.STATUS_AMBIGUOUS)


def test_the_server_never_writes(tmp_path, monkeypatch):
    """Hashes and row counts identical after calling everything."""
    before = {p.name: p.stat().st_mtime_ns for p in config.DATA_DIR.glob("*.json")}
    counts = {}
    if Path(config.DB_PATH).exists():
        conn = M.read_only_db()
        try:
            for (t,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        finally:
            conn.close()

    for name in sorted(M.TOOLS):
        M.call(name, **{"find_players": {"query": "a"},
                        "get_player_outlook": {"player": "1"},
                        "compare_players": {"players": ["1", "2"]}}.get(name, {}))

    after = {p.name: p.stat().st_mtime_ns for p in config.DATA_DIR.glob("*.json")}
    assert before == after, "an artifact changed during read-only tool calls"
    if counts:
        conn = M.read_only_db()
        try:
            for t, n in counts.items():
                assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n
        finally:
            conn.close()


def _identifiers() -> set[str]:
    """Names the module actually references, ignoring prose in docstrings."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
            out |= {a.name for a in node.names}
    return out


def test_no_fpl_client_and_no_notification_sink_is_reachable():
    names = _identifiers()
    for banned in ("FplClient", "Engine", "WebhookSink", "ConsoleSink",
                   "send", "deliver", "launchctl", "notify"):
        assert banned not in names, f"mcp_server references {banned}"


def test_no_write_call_is_reachable():
    names = _identifiers()
    for banned in ("write_text", "write_bytes", "write_json_atomic", "mkdir",
                   "unlink", "rmtree", "commit", "executescript"):
        assert banned not in names, f"mcp_server can call {banned}"


def test_no_llm_call_inside_the_server():
    names = {n.lower() for n in _identifiers()}
    for banned in ("anthropic", "complete", "messages", "openai", "llm"):
        assert banned not in names, f"mcp_server references {banned}"


def test_no_secret_shaped_value_in_any_result():
    for name in sorted(M.TOOLS):
        blob = json.dumps(M.call(name, **{
            "find_players": {"query": "a"},
            "get_player_outlook": {"player": "1"},
            "compare_players": {"players": ["1", "2"]}}.get(name, {})))
        for marker in ("sk-ant-", "ANTHROPIC", "Bearer ", "password",
                       "Traceback", "gho_"):
            assert marker not in blob, f"{name} leaks {marker}"


def test_the_server_has_no_http_transport():
    text = SRC.read_text(encoding="utf-8")
    assert 'transport="stdio"' in text
    for banned in ("streamable-http", "run_sse", "run_streamable", "0.0.0.0",
                   "uvicorn", "bind("):
        assert banned not in text


# --- specific content the client relies on ------------------------------------

def test_the_decision_tool_publishes_the_unfitted_threshold():
    r = M.call("get_weekly_decision")
    if r["status"] != M.STATUS_OK or r.get("threshold_status") is None:
        pytest.skip("decision artifact predates the threshold_status field")
    assert r["threshold_status"]["fitted"] is False


def test_the_model_evidence_separates_the_candidates():
    r = M.call("get_model_evidence")
    assert r["status"] == M.STATUS_OK
    decisions = {c["candidate"]: c["decision"]
                 for c in r["model_candidates"]["candidates"]}
    assert decisions["gbm"] == "rejected"
    assert decisions["ridge"] == "inconclusive"
    assert r["ep_next_blend"]["fitted"] is False
    assert "fpl_xp" in r["withdrawn_baselines"]


def test_the_player_outlook_keeps_the_model_and_ep_next_apart():
    r = M.call("get_player_outlook", player="1")
    if r["status"] != M.STATUS_OK:
        pytest.skip("player id 1 absent")
    proj = r["player"]["projection"]
    assert "model_only" in proj and "fpl_ep_next" in proj
    assert proj["blend_is_fitted"] is False


def test_league_strategy_never_presents_global_ownership_as_league_ownership():
    r = M.call("get_league_strategy")
    if r["status"] != M.STATUS_OK:
        pytest.skip("no strategy artifact")
    assert any("league-scoped" in lim for lim in r["limitations"])
    for lg in r["leagues"]:
        if lg.get("placing", {}).get("available"):
            assert "rival_coverage_pct" in lg["placing"], \
                "a placing probability must carry its rival coverage"


def test_live_keeps_confirmed_provisional_and_predicted_apart():
    r = M.call("get_live_gameweek")
    if r["status"] != M.STATUS_OK:
        assert r["status"] == M.STATUS_UNAVAILABLE
        assert r.get("unavailable_reason") or r.get("detail")
        return
    assert any("PROVISIONAL" in lim for lim in r["limitations"])


def test_what_changed_states_when_there_is_no_prior_snapshot():
    r = M.call("what_changed")
    assert r["status"] in (M.STATUS_OK, M.STATUS_UNAVAILABLE)
    if r["status"] == M.STATUS_UNAVAILABLE:
        assert r["compared"] is False
        assert r["detail"]
    else:
        assert "changed_fields" in r


# --- the SDK wiring -----------------------------------------------------------

def test_the_server_builds_and_registers_every_tool():
    server = M.build_server()
    assert server.name == M.SERVER_NAME
    assert server.instructions and "read-only" in server.instructions.lower()


def test_self_test_terminates_without_reading_stdin():
    """A self-test that blocks on stdio is useless in CI.

    Checked on the parse tree, not the text: the docstring legitimately contains
    the word stdin, and a substring match on prose is not a guarantee.
    """
    tree = ast.parse(inspect.getsource(M.self_test))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            assert not (isinstance(fn, ast.Name) and fn.id == "input")
            assert not (isinstance(fn, ast.Attribute) and fn.attr in
                        ("read", "readline", "readlines"))
    assert M.self_test() in (0, 1, 2)


def test_module_name_does_not_shadow_the_sdk():
    import mcp
    assert Path(mcp.__file__).parent != SRC.parent
    assert SRC.name == "mcp_server.py"


def test_what_changed_compares_like_with_like():
    """The snapshot stores player ids; the artifact stores resolved cards.

    Comparing `426` against `"B.Fernandes"` reported the captain as changed on
    every run — found by driving the server through a real MCP client rather
    than by calling the function.
    """
    r = M.call("what_changed")
    if r["status"] != M.STATUS_OK or not r.get("compared"):
        pytest.skip("no prior snapshot to compare against")
    for entry in r["changed_fields"]:
        was, now = entry["was"], entry["now"]
        if was is None or now is None:
            continue
        assert type(was) is type(now), (
            f"{entry['field']} compares {type(was).__name__} against "
            f"{type(now).__name__}: {was!r} vs {now!r}")
