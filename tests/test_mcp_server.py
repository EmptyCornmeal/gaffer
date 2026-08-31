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


# ---------------------------------------------------------------------------
# Batch 7.1 — the three defects the read-only acceptance test found
# ---------------------------------------------------------------------------

DEFAULT_ARGS = {"find_players": {"query": "a"},
                "get_player_outlook": {"player": "12"},
                "compare_players": {"players": ["12", "426"]}}


# --- D1: every default response fits the budget ------------------------------

def test_every_default_response_is_within_the_serialized_budget():
    """`get_transfer_plan` returned 74 KB and the MCP client refused it outright,
    so the tool was unusable however correct its contents were."""
    over = []
    for name in sorted(M.TOOLS):
        n = M.serialized_bytes(M.call(name, **DEFAULT_ARGS.get(name, {})))
        if n > M.MAX_RESULT_BYTES:
            over.append(f"{name}: {n:,} bytes")
    assert over == [], f"over the {M.MAX_RESULT_BYTES:,}-byte budget: {over}"


#: Smallest headroom any tool may keep before it counts as at risk. The budget
#: test below only fails once a tool is *already* unusable; this one fails while
#: there is still room to act. `get_model_evidence` reached 19,452 of 20,000 —
#: 548 bytes — and nothing noticed, because being under the cap was the only
#: thing asserted.
MIN_HEADROOM_BYTES = 1_500


def test_every_tool_keeps_usable_headroom_under_the_budget():
    """Early warning. Being *just* under the cap is how the cap gets breached."""
    tight = []
    for name in sorted(M.TOOLS):
        n = M.serialized_bytes(M.call(name, **DEFAULT_ARGS.get(name, {})))
        head = M.MAX_RESULT_BYTES - n
        if head < MIN_HEADROOM_BYTES:
            tight.append(f"{name}: {n:,} bytes, only {head:,} spare")
    assert tight == [], (
        f"under {MIN_HEADROOM_BYTES:,} bytes of headroom: {tight}. "
        "Project the payload rather than raising MAX_RESULT_BYTES.")


def test_the_model_evidence_summary_keeps_every_decision_and_reason():
    """The projection may drop duplicated numbers, never a decision."""
    summary = M.call("get_model_evidence")
    full = M.call("get_model_evidence", detail="full")
    if summary["status"] != M.STATUS_OK:
        pytest.skip("no backtest artifact")

    s_c = {c["candidate"]: c for c in summary["model_candidates"]["candidates"]}
    f_c = {c["candidate"]: c for c in full["model_candidates"]["candidates"]}
    assert set(s_c) == set(f_c), "the projection lost a candidate"
    for name, c in s_c.items():
        assert c["decision"] == f_c[name]["decision"]
        assert c.get("reason") == f_c[name].get("reason"), "a reason was trimmed"

    assert M.serialized_bytes(summary) < M.serialized_bytes(full)
    assert summary["detail"] == "summary"
    assert "full" in summary["detail_available"]


def test_the_model_evidence_summary_still_supports_its_own_prose():
    """`reason` claims "worse at every horizon" and counts intervals; the summary
    has to keep both checkable, or it is prose without evidence."""
    r = M.call("get_model_evidence")
    if r["status"] != M.STATUS_OK:
        pytest.skip("no backtest artifact")
    for c in r["model_candidates"]["candidates"]:
        ph = c.get("per_horizon_summary")
        if ph is None:
            continue
        assert ph["horizons_measured"] >= 1
        assert "intervals_excluding_zero" in ph
        if c.get("worse_at_every_horizon") is True:
            assert ph["best_diff"] is not None and ph["best_diff"] < 0, (
                f"{c['candidate']} claims it loses everywhere but its best "
                f"horizon is {ph['best_diff']}")


def test_an_unknown_evidence_detail_is_refused():
    r = M.call("get_model_evidence", detail="everything")
    assert r["status"] == M.STATUS_INVALID


def test_the_transfer_plan_summary_is_small_and_still_decision_shaped():
    r = M.call("get_transfer_plan")
    if r["status"] != M.STATUS_OK:
        pytest.skip("no plan artifact")
    assert M.serialized_bytes(r) < M.MAX_RESULT_BYTES
    for field in ("plan", "initial_state", "first_move", "steps",
                  "detail_available", "limitations"):
        assert field in r, f"the summary dropped {field}"
    for field in ("status", "mode", "horizon", "total_expected"):
        assert field in r["plan"]
    step = r["steps"][0]
    for field in ("gw", "transfers_in", "transfers_out", "hits",
                  "free_transfers", "bank", "xi_expected", "captain", "vice",
                  "starting_ids", "bench_ids_in_order"):
        assert field in step, f"the step dropped {field}"
    assert len(step["starting_ids"]) == 11
    assert all(isinstance(i, int) for i in step["starting_ids"])
    assert all(isinstance(i, int) for i in step["bench_ids_in_order"])


def test_the_plan_summary_carries_no_full_player_cards():
    """The 74 KB came from repeating fifteen 15-field cards per gameweek."""
    r = M.call("get_transfer_plan")
    if r["status"] != M.STATUS_OK:
        pytest.skip("no plan artifact")
    blob = json.dumps(r)
    for heavy in ("rationale", "xmins_badge", "fixtures", "tags", "team_code"):
        assert heavy not in blob, f"the summary still carries {heavy}"


def test_gameweek_detail_returns_one_week_in_full_and_stays_bounded():
    summary = M.call("get_transfer_plan")
    if summary["status"] != M.STATUS_OK:
        pytest.skip("no plan artifact")
    gw = summary["steps"][0]["gw"]
    r = M.call("get_transfer_plan", detail="gameweek", gameweek=gw)
    assert r["status"] == M.STATUS_OK
    assert r["gameweek"] == gw
    assert len(r["step"]["starting"]) == 11
    assert all(isinstance(p, dict) and "name" in p for p in r["step"]["starting"])
    assert M.serialized_bytes(r) < M.MAX_RESULT_BYTES


@pytest.mark.parametrize("kwargs,expected", [
    ({"detail": "everything"}, M.STATUS_INVALID),
    ({"detail": "gameweek"}, M.STATUS_INVALID),          # no gameweek given
    ({"detail": "gameweek", "gameweek": "abc"}, M.STATUS_INVALID),
    ({"detail": "gameweek", "gameweek": 99}, M.STATUS_NOT_FOUND),
])
def test_the_plan_selector_is_validated(kwargs, expected):
    r = M.call("get_transfer_plan", **kwargs)
    if r["status"] == M.STATUS_MISSING:
        pytest.skip("no plan artifact")
    assert r["status"] == expected
    assert r["detail"]


def test_the_default_detail_is_summary():
    a = M.call("get_transfer_plan")
    b = M.call("get_transfer_plan", detail="summary")
    assert a.get("detail") == "summary"
    # `freshness.age_seconds` is measured against the clock at call time, so two
    # calls either side of a second tick differ by 1 and this compared unequal at
    # random. It is a real flake with a real cost: the refresh workflow runs the
    # suite before publishing, so a red run here stops a gameweek's data going
    # out. Compare everything the argument actually controls.
    assert a.keys() == b.keys()
    assert {k: v for k, v in a.items() if k != "freshness"} == \
           {k: v for k, v in b.items() if k != "freshness"}
    assert a["freshness"]["generated_at"] == b["freshness"]["generated_at"]
    assert a["freshness"]["stale"] == b["freshness"]["stale"]


# --- D2: provenance comes from the artifact the tool actually read ----------

def _versions(name):
    return M.call(name, **DEFAULT_ARGS.get(name, {}))["versions"]


@pytest.mark.parametrize("name", sorted(M.TOOLS))
def test_every_response_declares_where_its_versions_came_from(name):
    v = _versions(name)
    assert v["source"], f"{name} does not name its provenance source"
    for field in M.VERSION_FIELDS:
        assert field in v
        if v[field] is None:
            assert v["unavailable"].get(field) in (M.NOT_APPLICABLE, M.NOT_AVAILABLE), \
                f"{name}.{field} is null with no reason — indistinguishable " \
                f"from an accidental omission"
        else:
            assert field not in v["unavailable"]


@pytest.mark.parametrize("name", sorted(M.TOOLS))
def test_no_response_contradicts_a_version_it_carries_elsewhere(name):
    """The defect verbatim: the envelope said sim_version null while the same
    payload carried simulation.sim_version = 'scenarios-1.0'."""
    result = M.call(name, **DEFAULT_ARGS.get(name, {}))
    env = result["versions"]
    found: list[tuple[str, str, str]] = []

    def walk(node, path="$"):
        if isinstance(node, dict):
            for k, val in node.items():
                if k in M.VERSION_FIELDS and isinstance(val, str):
                    found.append((k, val, f"{path}.{k}"))
                walk(val, f"{path}.{k}")
        elif isinstance(node, list):
            for i, val in enumerate(node[:20]):
                walk(val, f"{path}[{i}]")

    walk({k: v for k, v in result.items() if k != "versions"})
    for field, value, where in found:
        assert env[field] == value, (
            f"{name}: envelope {field}={env[field]!r} contradicts "
            f"{where}={value!r}")


def test_the_league_strategy_envelope_matches_its_simulation_block():
    r = M.call("get_league_strategy")
    if r["status"] != M.STATUS_OK:
        pytest.skip("no strategy artifact")
    assert r["versions"]["sim_version"] == r["simulation"]["sim_version"]
    assert r["versions"]["model_version"] == r["simulation"]["model_version"]


def test_the_weekly_decision_exposes_its_real_objective_and_scenario_versions():
    r = M.call("get_weekly_decision")
    if r["status"] != M.STATUS_OK:
        pytest.skip("no decision artifact")
    v = r["versions"]
    assert v["objective_version"], "the decision comes out of the shared objective"
    assert v["sim_version"], "the hold comparison is scored in the scenario set"
    assert v["unavailable"] == {}


def test_tools_over_the_same_calculation_agree_on_versions():
    """`what_changed` reads a decision snapshot; both describe the same solve."""
    if M.call("what_changed")["status"] != M.STATUS_OK:
        pytest.skip("no prior snapshot")
    a, b = _versions("get_weekly_decision"), _versions("what_changed")
    for field in M.VERSION_FIELDS:
        assert a[field] == b[field], f"{field} disagrees: {a[field]} vs {b[field]}"


def test_a_version_is_never_borrowed_from_an_unrelated_artifact():
    """players.json is a bare list with no solve behind it."""
    v = _versions("get_player_outlook")
    assert v["objective_version"] is None
    assert v["unavailable"]["objective_version"] == M.NOT_APPLICABLE
    assert v["sim_version"] is None


def test_an_applicable_but_unrecorded_version_is_not_available_not_inapplicable():
    """plan.json genuinely comes from the objective and the scenarios; it just
    does not record either. That distinction is the whole point."""
    r = M.call("get_transfer_plan")
    if r["status"] != M.STATUS_OK:
        pytest.skip("no plan artifact")
    v = r["versions"]
    assert v["objective_version"] is None
    assert v["unavailable"]["objective_version"] == M.NOT_AVAILABLE
    assert v["unavailable"]["sim_version"] == M.NOT_AVAILABLE


# --- D3: components are real or explicitly unavailable ----------------------

def test_the_blend_components_are_the_stored_values():
    r = M.call("get_player_outlook", player="12")
    if r["status"] != M.STATUS_OK:
        pytest.skip("player 12 absent")
    proj = r["player"]["projection"]
    if not proj["components_available"]:
        pytest.skip("no local database in this environment")
    gw = int(json.loads(
        (config.DATA_DIR / "meta.json").read_text(encoding="utf-8"))["current_gw"])
    conn = M.read_only_db()
    try:
        row = conn.execute(
            "SELECT exp_points_model, exp_points_ep_next FROM projections "
            "WHERE player_id = 12 AND gw = ?", (gw,)).fetchone()
    finally:
        conn.close()
    assert proj["model_only"] == row["exp_points_model"]
    assert proj["fpl_ep_next"] == row["exp_points_ep_next"]


def test_a_component_is_never_derived_backwards_from_the_blend():
    """The blend is (1-w)*model + w*ep_next with w scaled by availability, so it
    is invertible on paper. The values must still come from the record.

    Checked on the parse tree with the docstring stripped: the docstring
    legitimately names `next_gw_xp` while explaining that it is not used, and a
    substring scan cannot tell prose from code.
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "stored_components")
    body = fn.body[1:] if ast.get_docstring(fn) else fn.body
    code = " ; ".join(ast.unparse(n) for n in body)
    assert "SELECT" in code and "exp_points_model" in code, (
        "the components must come from a query, not a calculation")
    for banned in ("EP_NEXT_BLEND_WEIGHT", "next_gw_xp"):
        assert banned not in code, f"stored_components computes with {banned!r}"
    # No arithmetic at all on the returned values.
    for node in ast.walk(fn):
        if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Sub, ast.Mult, ast.Div)):
            raise AssertionError("stored_components does arithmetic on a component")


def test_zero_is_a_value_not_an_absence():
    """42 players have a stored ep_next of exactly 0.0 — the blend is skipped for
    them. That is a component, not a missing one."""
    assert M._component_block(
        {"exp_points_model": 4.0, "exp_points_ep_next": 0.0}, have_db=True
    ) == {"model_only": 4.0, "fpl_ep_next": 0.0,
          "components_available": True, "unavailable_reason": None}


@pytest.mark.parametrize("row,have_db,available,reason", [
    ({"exp_points_model": 5.1, "exp_points_ep_next": 3.2}, True, True, None),
    ({"exp_points_model": 5.1, "exp_points_ep_next": None}, True, False,
     M.COMPONENTS_NOT_STORED),
    ({"exp_points_model": None, "exp_points_ep_next": None}, True, False,
     M.COMPONENTS_NOT_STORED),
    (None, True, False, M.COMPONENTS_NO_ROW),
    (None, False, False, M.COMPONENTS_NO_DB),
])
def test_component_availability_is_always_explicit(row, have_db, available, reason):
    b = M._component_block(row, have_db=have_db)
    assert b["components_available"] is available
    assert b["unavailable_reason"] == reason
    if not available:
        assert b["model_only"] is None or b["fpl_ep_next"] is None


def test_one_missing_component_names_which():
    b = M._component_block({"exp_points_model": 5.1, "exp_points_ep_next": None},
                           have_db=True)
    assert b["missing_components"] == ["fpl_ep_next"]


def test_the_limitation_matches_what_was_actually_returned():
    """The defect: 'kept separate' printed beside two nulls."""
    r = M.call("get_player_outlook", player="12")
    if r["status"] != M.STATUS_OK:
        pytest.skip("player 12 absent")
    available = r["player"]["projection"]["components_available"]
    text = " ".join(r["limitations"])
    if available:
        assert "read from the stored projection" in text
        assert "NOT available" not in text
    else:
        assert "NOT available" in text
        assert "kept separate" not in text


def test_outlook_and_comparison_report_the_same_components():
    a = M.call("get_player_outlook", player="12")
    b = M.call("compare_players", players=["12", "426"])
    if a["status"] != M.STATUS_OK or b["status"] != M.STATUS_OK:
        pytest.skip("players absent")
    theirs = next(p for p in b["players"] if p["id"] == 12)
    assert a["player"]["projection"] == theirs["projection"]
    assert a["player"]["minutes"] == theirs["minutes"]
    assert a["player"]["uncertainty"] == theirs["uncertainty"]


def test_the_distribution_is_read_from_where_it_is_stored():
    """floor/ceiling/boom were read from top-level keys that do not exist, so
    every one came back null beside a claim they were provided."""
    r = M.call("get_player_outlook", player="12")
    if r["status"] != M.STATUS_OK:
        pytest.skip("player 12 absent")
    u = r["player"]["uncertainty"]
    if not u["distribution_available"]:
        pytest.skip("this artifact carries no dist block")
    for field in ("floor", "ceiling", "boom_pct"):
        assert u[field] is not None, f"{field} is still null"


def test_no_nullable_field_is_promised_without_a_reason():
    """Every null in the projection/minutes blocks is accompanied by a code."""
    r = M.call("get_player_outlook", player="12")
    if r["status"] != M.STATUS_OK:
        pytest.skip("player 12 absent")
    proj, minutes = r["player"]["projection"], r["player"]["minutes"]
    if proj["model_only"] is None or proj["fpl_ep_next"] is None:
        assert proj["unavailable_reason"]
    if minutes["exp_minutes"] is None:
        assert minutes["exp_minutes_source"] != "projections"


# ---------------------------------------------------------------------------
# A8 — the league view publishes all four of its answers, not the two that fit
# ---------------------------------------------------------------------------

def test_the_league_view_publishes_all_four_of_its_answers():
    """`shields` was computed and dropped by the MCP; `threats` and the
    captain's effective ownership were computed and dropped by the export."""
    r = M.call("get_league_strategy")
    if r["status"] != M.STATUS_OK:
        pytest.skip("no strategy artifact")
    for lg in r["leagues"]:
        assert "shields" in lg, "the tool dropped shields entirely"
        assert "differentials" in lg
        assert "threats" in lg or lg.get("threats_unavailable"), \
            "threats are neither published nor explained"
        assert "my_captain_eo_pct" in lg or lg.get("my_captain_eo_pct_unavailable")


def test_a_league_ownership_row_names_the_player_rather_than_only_numbering_him():
    r = M.call("get_league_strategy")
    if r["status"] != M.STATUS_OK:
        pytest.skip("no strategy artifact")
    rows = [row for lg in r["leagues"]
            for row in lg["shields"] + lg["differentials"] + lg.get("threats", [])]
    if not rows:
        pytest.skip("no rival picks are readable yet")
    for row in rows:
        assert row["player_id"] is not None
        assert row["name"], f"row {row['player_id']} names nobody"
        assert row["effective_ownership_pct"] is not None


def _fake_data(tmp_path, strategy):
    (tmp_path / "meta.json").write_text(json.dumps(
        {"season": "2026-27", "generated_at": "2026-08-31T00:00:00+00:00",
         "current_gw": 3}), encoding="utf-8")
    (tmp_path / "strategy.json").write_text(json.dumps(strategy),
                                            encoding="utf-8")
    return tmp_path


def test_a_strategy_without_threats_says_so_rather_than_publishing_an_empty_list(
        tmp_path, monkeypatch):
    """An empty list reads as 'your rivals own nothing you do not'."""
    monkeypatch.setattr(M, "data_dir", lambda: _fake_data(tmp_path, {
        "leagues": [{"league_id": 1, "name": "L", "shields": [],
                     "differentials": []}]}))
    lg = M.call("get_league_strategy")["leagues"][0]
    assert "threats" not in lg
    assert lg["threats_unavailable"]
    assert lg["my_captain_eo_pct_unavailable"]


def _row(pid):
    return {"player_id": pid, "owners": 2, "n_rivals": 4, "ownership_pct": 50.0,
            "effective_ownership_pct": 75.0, "captain_eo_pct": 25.0,
            "player": {"id": pid, "name": f"Player {pid}", "team": "ARS",
                       "pos": "MID", "price": 7.5, "next_gw_xp": 4.2}}


def test_the_league_view_thins_its_rows_rather_than_exceeding_the_budget(
        tmp_path, monkeypatch):
    """Five leagues of thirty ownership rows is a payload a client refuses."""
    leagues = [{"league_id": i, "name": f"L{i}",
                "shields": [_row(p) for p in range(1, 11)],
                "differentials": [_row(p) for p in range(11, 21)],
                "threats": [_row(p) for p in range(21, 31)],
                "my_captain_eo_pct": 50.0} for i in range(1, 6)]
    monkeypatch.setattr(M, "data_dir",
                        lambda: _fake_data(tmp_path, {"leagues": leagues}))
    r = M.call("get_league_strategy")
    assert r["status"] == M.STATUS_OK
    assert M.serialized_bytes(r) <= M.MAX_RESULT_BYTES
    assert r["ownership_rows_per_list"] < M.MAX_OWNERSHIP_ROWS
    assert r["ownership_rows_thinned"], "it thinned silently"
    assert all(lg["threats"] for lg in r["leagues"]), "a whole list was lost"


# ---------------------------------------------------------------------------
# B1 — the fields the feed computes and the conversation never saw
# ---------------------------------------------------------------------------

def test_the_outlook_publishes_the_defensive_contribution_against_its_threshold():
    """Defensive contribution is a new scoring mechanic and the floor-versus-
    spike test was being done by hand every session."""
    r = M.call("get_player_outlook", player="12")
    if r["status"] != M.STATUS_OK:
        pytest.skip("player 12 absent")
    dc = r["player"]["defensive_contribution"]
    assert "per90" in dc
    if dc["scored_by_position"]:
        assert dc["threshold"] in (10, 12)
    else:
        assert dc["threshold"] is None and dc["unavailable_reason"]


def test_a_goalkeeper_is_told_the_mechanic_does_not_apply():
    """A null threshold with no reason is indistinguishable from a bug."""
    keepers = M.call("find_players", position="GKP", limit=1)
    if keepers["status"] != M.STATUS_OK:
        pytest.skip("no goalkeepers in this artifact")
    r = M.call("get_player_outlook", player=str(keepers["players"][0]["id"]))
    dc = r["player"]["defensive_contribution"]
    assert dc["scored_by_position"] is False
    assert dc["unavailable_reason"] == M.DEFCON_NO_THRESHOLD


def test_the_outlook_publishes_both_blend_components_as_the_artifact_recorded_them():
    """`model_only`/`fpl_ep_next` come from the database; these come from the
    same run as `next_gw_xp`, which is the point of carrying both."""
    r = M.call("get_player_outlook", player="12")
    if r["status"] != M.STATUS_OK:
        pytest.skip("player 12 absent")
    proj = r["player"]["projection"]
    for field in ("model_xp", "ep_next_xp", "xp_window", "breakdown"):
        assert field in proj, f"the outlook still drops {field}"
    published = json.loads(
        (config.DATA_DIR / "players.json").read_text(encoding="utf-8"))
    row = next(p for p in published if p["id"] == 12)
    assert proj["model_xp"] == row["model_xp"]
    assert proj["ep_next_xp"] == row["ep_next_xp"]


def test_the_projection_regime_travels_with_every_projection_claim():
    """Whether the published number is a blend or the model alone is a fact
    about the run, and a caller cannot read `model_xp` without it."""
    for name, args in (("gaffer_status", {}),
                       ("get_player_outlook", {"player": "12"}),
                       ("compare_players", {"players": ["12", "426"]})):
        r = M.call(name, **args)
        if r["status"] != M.STATUS_OK:
            continue
        block = r["projection_regime"]
        assert "regime" in block and "nominal_blend_weight" in block
        assert block["blend_is_fitted"] is False


def test_the_underlying_rates_are_fields_rather_than_prose():
    """xGI/90, form and last season were reachable only inside `rationale`,
    where they cannot be sorted or compared."""
    r = M.call("get_player_outlook", player="12")
    if r["status"] != M.STATUS_OK:
        pytest.skip("player 12 absent")
    for field in ("xgi90", "form", "ict", "last_season"):
        assert field in r["player"]["underlying"]


@pytest.mark.parametrize("note,expected", [
    ("pens #1, FK #2, corners #1", (1, 2, 1)),
    ("corners #2", (None, None, 2)),
    ("", (None, None, None)),
    ("nonsense", (None, None, None)),
])
def test_set_pieces_are_an_order_rather_than_a_label(note, expected):
    got = M._set_piece_order(note)
    assert (got["penalties"], got["free_kicks"], got["corners"]) == expected
    assert got["on_any"] is any(v is not None for v in expected)
    assert got["recorded"], "a null must say what it means"


def test_the_price_signal_declares_that_it_is_an_estimate():
    r = M.call("get_player_outlook", player="12")
    if r["status"] != M.STATUS_OK:
        pytest.skip("player 12 absent")
    signal = r["player"]["price_signal"]
    for field in ("change_this_gw", "net_transfers_this_gw", "direction",
                  "progress_to_change"):
        assert field in signal
    assert "estimated" in signal["basis"]


def test_a_component_stored_by_another_run_is_flagged_not_presented_as_this_one():
    """`data/*.json` is refreshed on a schedule and `data/gaffer.db` only by a
    local pipeline run, so the stored components can belong to an older run
    than the `next_gw_xp` they are shown beside."""
    stale = M._component_block(
        {"exp_points": 4.7, "exp_points_model": 4.7, "exp_points_ep_next": 4.0},
        have_db=True, published_next_gw_xp=10.38)
    assert stale["same_run_as_published"] is False
    assert stale["provenance"] == M.COMPONENTS_OTHER_RUN
    assert stale["stored_blend"] == 4.7
    text = " ".join(M._component_limitations([stale]))
    assert "EARLIER pipeline run" in text and "model_xp" in text

    fresh = M._component_block(
        {"exp_points": 4.7, "exp_points_model": 4.7, "exp_points_ep_next": 4.0},
        have_db=True, published_next_gw_xp=4.7)
    assert fresh["same_run_as_published"] is True
    assert "EARLIER pipeline run" not in " ".join(
        M._component_limitations([fresh]))


def test_the_blend_limitation_describes_what_the_code_actually_does():
    """The weight is scaled by availability AND start probability, and is zero
    for everyone when FPL's ep_next fails the degeneracy test."""
    text = " ".join(M._component_limitations([M._component_block(
        {"exp_points": 4.7, "exp_points_model": 4.7, "exp_points_ep_next": 4.0},
        have_db=True)]))
    assert "start probability" in text
    assert "degeneracy" in text


# ---------------------------------------------------------------------------
# B4 — the derived quantities that existed and were never offered
# ---------------------------------------------------------------------------

def test_the_fixture_window_is_defined_over_gameweeks_not_over_fixtures():
    """A blank must contribute nothing and a double must contribute twice."""
    fixtures = [{"gw": 3, "difficulty": 2, "home": True},
                {"gw": 3, "difficulty": 5, "home": False},
                {"gw": 5, "difficulty": 1, "home": True}]
    gw_xp = [{"gw": 3, "xp": 4.0}, {"gw": 5, "xp": 2.0}, {"gw": 9, "xp": 99.0}]
    w = M._window(fixtures, gw_xp, 3, 3)
    assert w["gameweeks"] == [3, 4, 5]
    assert w["fixtures"] == 3
    assert w["doubles"] == [3] and w["blanks"] == [4]
    assert w["difficulty_sum"] == 8
    assert w["xp_sum"] == 6.0, "a gameweek outside the window was counted"


def test_a_fixture_window_without_a_gameweek_says_so():
    w = M._window([], [], None, 3)
    assert w["available"] is False and w["unavailable_reason"]


def test_the_outlook_offers_the_three_gameweek_sum_that_orders_transfers():
    r = M.call("get_player_outlook", player="12")
    if r["status"] != M.STATUS_OK:
        pytest.skip("player 12 absent")
    windows = r["player"]["fixture_outlook"]
    assert set(windows) == {f"next{n}" for n in M.FIXTURE_WINDOWS}
    three = windows["next3"]
    if three["available"] and three["fixtures"]:
        assert three["difficulty_sum"] is not None
        assert len(three["gameweeks"]) == 3


def test_the_selling_price_is_purchase_plus_half_the_rise_rounded_down():
    """FPL's rule, applied through the app's own function — a second
    implementation of a rounding rule is how they start disagreeing by 0.1m."""
    squad = {"rows": {}, "event": 1, "unavailable_reason": None}
    risen = M._holding_block(
        {"purchase_price": 60, "selling_price": 60, "price_source": "season_start",
         "price_exact": 1, "gw": 1}, 6.5, squad)
    assert risen["owned"] is True
    assert risen["purchase_price"] == 6.0
    assert risen["selling_price"] == 6.2
    assert risen["locked_in"] == pytest.approx(0.3)
    assert risen["stored_selling_price"] == 6.0
    assert risen["stored_differs_because"]

    fallen = M._holding_block(
        {"purchase_price": 60, "selling_price": 57, "price_source": "transfer_in",
         "price_exact": 1, "gw": 1}, 5.7, squad)
    assert fallen["selling_price"] == 5.7, "a fall is taken in full"
    assert fallen["locked_in"] == 0.0


def test_a_player_you_do_not_own_is_not_owned_rather_than_unavailable():
    squad = {"rows": {}, "event": 2, "unavailable_reason": None}
    assert M._holding_block(None, 6.0, squad) == {
        "owned": False, "squad_event": 2, "reason": M.SQUAD_NOT_OWNED}
    unknown = M._holding_block(None, 6.0,
                               {"rows": {}, "event": None,
                                "unavailable_reason": M.SQUAD_NONE_STORED})
    assert unknown["owned"] is None and unknown["unavailable_reason"]


def test_status_publishes_the_budget_a_transfer_has_to_be_paid_from():
    r = M.call("gaffer_status")
    assert r["status"] == M.STATUS_OK
    budget = r["budget"]
    for field in ("bank", "team_value", "free_transfers",
                  "squad_selling_value", "selling_price_confidence"):
        assert field in budget, f"status still drops {field}"
    if budget["squad_selling_value"] is None:
        assert budget["squad_value_unavailable"]
    else:
        assert budget["squad_value_event"] is not None


# ---------------------------------------------------------------------------
# B1 — a search that can answer "who is about to rise" in one call
# ---------------------------------------------------------------------------

def test_a_search_can_rank_on_any_published_signal():
    # A position keeps sort="next_gw_xp" — the default, and on its own not a
    # criterion — a legal call, so this test is about ordering and nothing else.
    for field in M.SORTABLE:
        r = M.call("find_players", position="MID", sort=field, limit=5)
        assert r["status"] in (M.STATUS_OK, M.STATUS_NOT_FOUND), field
        values = [row[field] for row in r.get("players", [])
                  if isinstance(row.get(field), (int, float))]
        assert values == sorted(values, reverse=True), f"{field} came out unsorted"


def test_a_search_refuses_a_sort_or_an_order_it_cannot_honour():
    assert M.call("find_players", sort="vibes")["status"] == M.STATUS_INVALID
    assert M.call("find_players", sort="price",
                  order="sideways")["status"] == M.STATUS_INVALID
    assert M.call("find_players", team="ARS",
                  min_price="cheap")["status"] == M.STATUS_INVALID


@pytest.mark.parametrize("order", ["desc", "asc"])
def test_a_search_never_ranks_an_unknown_above_a_measured_value(order):
    """Sorting nulls as zero puts 'we do not know' above a measured low value
    in one direction and below it in the other."""
    r = M.call("find_players", sort="defcon_p_hit", order=order, limit=25)
    if r["status"] != M.STATUS_OK:
        pytest.skip("no players artifact")
    seen_unknown = False
    for row in r["players"]:
        if row["defcon_p_hit"] is None:
            seen_unknown = True
        else:
            assert not seen_unknown, "a null outranked a measured value"


def test_a_filter_is_applied_rather_than_only_described():
    r = M.call("find_players", position="DEF", min_defcon90=3.0, limit=25)
    if r["status"] != M.STATUS_OK:
        pytest.skip("no defender clears that rate in this artifact")
    assert all(p["pos"] == "DEF" for p in r["players"])
    assert all(p["defcon90"] >= 3.0 for p in r["players"])
    assert r["query"]["filters"]["min_defcon90"] == 3.0


def test_who_is_about_to_rise_is_one_call():
    r = M.call("find_players", price_direction="up", sort="price_progress",
               limit=10)
    if r["status"] != M.STATUS_OK:
        pytest.skip("nobody is rising in this artifact")
    assert all(p["price_direction"] == "up" for p in r["players"])
    progress = [p["price_progress"] for p in r["players"]
                if p["price_progress"] is not None]
    assert progress == sorted(progress, reverse=True)


def test_a_search_with_no_criterion_at_all_is_refused():
    r = M.call("find_players")
    assert r["status"] == M.STATUS_INVALID
    assert r["detail"]


def test_a_search_result_carries_the_signal_it_was_ranked_on():
    r = M.call("find_players", query="a", limit=5)
    if r["status"] != M.STATUS_OK:
        pytest.skip("no players artifact")
    for row in r["players"]:
        for field in ("defcon90", "xgi90", "form", "fdr3", "price_direction"):
            assert field in row, f"a ranked row cannot be checked on {field}"


# ---------------------------------------------------------------------------
# B2 — the plan is argued in conversation, so its rows carry evidence
# ---------------------------------------------------------------------------

def test_the_plan_rows_carry_the_evidence_the_decision_is_argued_from():
    r = M.call("get_transfer_plan")
    if r["status"] != M.STATUS_OK:
        pytest.skip("no plan artifact")
    captain = r["steps"][0].get("captain")
    if not isinstance(captain, dict):
        pytest.skip("this plan names no captain")
    assert "next_gw_xp" in captain, "a plan row still says only who moved"
    assert M.serialized_bytes(r) < M.MAX_RESULT_BYTES
