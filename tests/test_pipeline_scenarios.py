"""Batch 2 integration — the real pipeline against deterministic stubs.

Five scenarios the audited pipeline handled wrongly or not at all:
  1. pre-GW1, no readable picks
  2. between gameweeks, a readable previous squad
  3. a later run with newly revealed picks
  4. a transient API failure
  5. a player-history correction
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from gaffer import config, contract, gameweek, pipeline
from gaffer.store import db

GW1_DL = datetime(2026, 8, 21, 17, 30, tzinfo=UTC)
GW2_DL = datetime(2026, 8, 28, 17, 30, tzinfo=UTC)
GW3_DL = datetime(2026, 9, 12, 17, 30, tzinfo=UTC)

PRE_GW1 = GW1_DL - timedelta(days=15)
BETWEEN_1_2 = GW1_DL + timedelta(days=3)
BETWEEN_2_3 = GW2_DL + timedelta(days=3)

_TYPE = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}
ENTRY = 1066421


def _elements():
    out, pid = [], 1
    for pos, n in (("GKP", 6), ("DEF", 14), ("MID", 14), ("FWD", 8)):
        for i in range(n):
            out.append({
                "id": pid, "code": 1000 + pid, "web_name": f"{pos}{i}",
                "first_name": "T", "second_name": f"{pos}{i}",
                "team": (pid % 6) + 1, "element_type": _TYPE[pos],
                "now_cost": 45 + (i % 4) * 5, "status": "a",
                "chance_of_playing_next_round": None, "selected_by_percent": "5.0",
                "minutes": 2400, "starts": 28, "form": "4.0",
                "points_per_game": "4.0", "ep_next": "4.0", "ict_index": "50.0",
                "expected_goals_per_90": "0.30", "expected_assists_per_90": "0.20",
                "expected_goal_involvements_per_90": "0.50",
                "expected_goals_conceded_per_90": "1.20",
                "defensive_contribution_per_90": "6.0", "news": "",
            })
            pid += 1
    return out


def _events(finished_upto=0):
    out = []
    for i, dl in ((1, GW1_DL), (2, GW2_DL), (3, GW3_DL)):
        out.append({
            "id": i, "name": f"Gameweek {i}",
            "deadline_time": dl.isoformat().replace("+00:00", "Z"),
            "finished": i <= finished_upto,
        })
    return out


def _bootstrap(finished_upto=0):
    return {
        "teams": [
            {"id": t, "code": t, "name": f"Club{t}", "short_name": f"C{t}",
             "strength_overall_home": 3, "strength_overall_away": 3,
             "strength_attack_home": 0, "strength_attack_away": 0,
             "strength_defence_home": 0, "strength_defence_away": 0}
            for t in range(1, 7)
        ],
        "elements": _elements(),
        "events": _events(finished_upto),
        "game_settings": {
            "squad_squadsize": 15, "squad_total_spend": 1000, "squad_team_limit": 3,
            "transfers_cap": 20, "transfers_sell_on_fee": 0.5,
            "max_extra_free_transfers": 4,
        },
        "total_players": 3_124_804,
    }


def _fixtures():
    out = []
    fid = 1
    for gw in (1, 2, 3):
        for h, a in ((1, 2), (3, 4), (5, 6)):
            out.append({"id": fid, "event": gw, "team_h": h, "team_a": a,
                        "kickoff_time": None, "team_h_difficulty": 3,
                        "team_a_difficulty": 3, "finished": False})
            fid += 1
    return out


def _picks(elements, gw):
    return {
        "picks": [{"element": e, "position": i + 1, "is_captain": i == 0,
                   "is_vice_captain": i == 1, "multiplier": 1}
                  for i, e in enumerate(elements)],
        "entry_history": {"bank": 5, "value": 1002},
        "active_chip": None,
    }


class ScenarioClient:
    """Deterministic FPL stub. `picks_by_gw` maps gw -> payload or Exception."""

    def __init__(self, finished_upto=0, picks_by_gw=None, history=None,
                 transfers=None, chips=None, leagues=None):
        self.finished_upto = finished_upto
        self.picks_by_gw = picks_by_gw or {}
        self.history = history or {}
        self.transfers = transfers if transfers is not None else []
        self.chips = chips or []
        self.leagues = leagues or {}
        self.picks_calls: list[int] = []
        self.league_calls: list[int] = []

    # -- league endpoints (used by the strategy step) -----------------------
    def league_classic(self, league_id, page=1):
        self.league_calls.append(league_id)
        if league_id not in self.leagues:
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "https://x.test"),
                response=httpx.Response(404, request=httpx.Request("GET", "https://x.test")),
            )
        return self.leagues[league_id]

    def readable_squad_event(self, now=None):
        return gameweek.readable_squad_event(_events(self.finished_upto), now)

    def projection_event(self, now=None):
        return gameweek.projection_event(_events(self.finished_upto), now)

    def entry_transfers(self, entry_id):
        if isinstance(self.transfers, Exception):
            raise self.transfers
        return self.transfers

    def entry_history(self, entry_id):
        return {"chips": self.chips, "current": []}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def bootstrap(self):
        return _bootstrap(self.finished_upto)

    def fixtures(self):
        return _fixtures()

    def element_summary(self, pid):
        return {"history_past": [], "history": self.history.get(pid, [])}

    def entry_picks(self, entry_id, gw):
        self.picks_calls.append(gw)
        if gw not in self.picks_by_gw:
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "https://x.test"),
                response=httpx.Response(404, request=httpx.Request("GET", "https://x.test")),
            )
        r = self.picks_by_gw[gw]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src" / "gaffer").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.delenv("GAFFER_DATA_DIR", raising=False)
    monkeypatch.setenv("GAFFER_ENTRY_ID", str(ENTRY))
    monkeypatch.setenv("GAFFER_LEAGUE_IDS", "271619,314")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GAFFER_SKIP_ENRICH", raising=False)
    config.reload_paths()
    monkeypatch.setattr("gaffer.ingest.ingest_entry_meta", lambda *a, **k: None)
    monkeypatch.setattr("gaffer.ai.news.generate",
                        lambda *a, **k: {"count": 0, "source": "stub"})
    return root


def _run(monkeypatch, client, now, **kw):
    # Both call sites must be stubbed. The strategy step opens its own client;
    # leaving that one real makes the suite hit the live FPL API.
    monkeypatch.setattr("gaffer.ingest.FplClient", lambda *a, **k: client)
    monkeypatch.setattr("gaffer.pipeline.FplClient", lambda *a, **k: client)
    return pipeline.run(fast=False, horizon=2, now=now, **kw)


def _strategy(root):
    return json.loads((root / "data" / "strategy.json").read_text(encoding="utf-8"))


def _standings(league_id, name, results, league_type="x"):
    return {"league": {"id": league_id, "name": name, "league_type": league_type},
            "standings": {"has_next": False, "results": results},
            "new_entries": {"results": []}}


def _meta(root) -> dict:
    return json.loads((root / "data" / "meta.json").read_text(encoding="utf-8"))


def _my_team(root):
    return json.loads((root / "data" / "my_team.json").read_text(encoding="utf-8"))


def _conn(root):
    return db.connect(root / "data" / "gaffer.db")


# --------------------------------------------------------------------------
# Scenario 1 — pre-GW1, no readable picks
# --------------------------------------------------------------------------

def test_scenario_1_preseason(repo, monkeypatch):
    c = ScenarioClient(finished_upto=0)
    log = _run(monkeypatch, c, PRE_GW1)

    assert c.picks_calls == [], "must not ask for picks before any deadline"
    m = _meta(repo)
    assert m["projection_event"] == "1"
    assert m["squad_status"] == gameweek.STATUS_NO_PUBLIC_SQUAD_YET
    assert m["squad_source_event"] is None
    assert m["squad_status_reason"]
    assert _my_team(repo) is None
    # Artifacts stay inside the checkout.
    for p in log["artifacts"]:
        assert Path(p).resolve().is_relative_to(repo.resolve())
    assert contract.validate(repo / "data", min_players=42).ok


# --------------------------------------------------------------------------
# Scenario 2 — between gameweeks, a readable previous squad
# --------------------------------------------------------------------------

def test_scenario_2_between_gameweeks(repo, monkeypatch):
    squad = list(range(1, 16))
    c = ScenarioClient(finished_upto=1, picks_by_gw={1: _picks(squad, 1)})
    _run(monkeypatch, c, BETWEEN_1_2)

    # Projecting GW2 while holding the squad revealed for GW1.
    # Two reads of event 1: ingest stores the squad, and the post-gameweek
    # review re-reads the same finished event for its result. The real client
    # caches for 300s, so that is one HTTP request. What must never happen is a
    # request for the *projected* event, whose picks are private until deadline.
    assert set(c.picks_calls) == {1}, "must request the readable event, not the projected one"
    assert 2 not in c.picks_calls
    m = _meta(repo)
    assert m["projection_event"] == "2"
    assert m["squad_source_event"] == "1"
    assert m["squad_status"] == gameweek.STATUS_LOADED
    mt = _my_team(repo)
    assert mt is not None
    assert mt["source_event"] == 1 and mt["projection_event"] == 2
    assert len(mt["players"]) == 15
    assert contract.validate(repo / "data", min_players=42).ok


def test_scenario_2_solver_sees_the_holdings(repo, monkeypatch):
    """The holdings baseline must reach the solver, not vanish."""
    squad = list(range(1, 16))
    c = ScenarioClient(finished_upto=1, picks_by_gw={1: _picks(squad, 1)})
    log = _run(monkeypatch, c, BETWEEN_1_2)
    # With a squad known, the solver runs in transfer mode rather than build.
    assert log["solver"]["mode"] == "transfer"


# --------------------------------------------------------------------------
# Scenario 3 — a later run with newly revealed picks
# --------------------------------------------------------------------------

def test_scenario_3_newly_revealed_picks_replace_the_old_squad(repo, monkeypatch):
    old, new = list(range(1, 16)), list(range(6, 21))
    c1 = ScenarioClient(finished_upto=1, picks_by_gw={1: _picks(old, 1)})
    _run(monkeypatch, c1, BETWEEN_1_2)
    assert {p["id"] for p in _my_team(repo)["players"]} == set(old)

    c2 = ScenarioClient(finished_upto=2,
                        picks_by_gw={1: _picks(old, 1), 2: _picks(new, 2)})
    _run(monkeypatch, c2, BETWEEN_2_3)

    assert set(c2.picks_calls) == {2}   # ingest + the review of the same event
    assert 3 not in c2.picks_calls
    m = _meta(repo)
    assert m["projection_event"] == "3"
    assert m["squad_source_event"] == "2"
    mt = _my_team(repo)
    assert {p["id"] for p in mt["players"]} == set(new)
    # The GW1 rows must be gone — one squad is stored, never a mixture.
    conn = _conn(repo)
    assert {r["gw"] for r in conn.execute("SELECT DISTINCT gw FROM my_squad")} == {2}
    conn.close()


# --------------------------------------------------------------------------
# Scenario 4 — a transient API failure
# --------------------------------------------------------------------------

def test_scenario_4_transient_failure_keeps_squad_but_marks_it_stale(repo, monkeypatch):
    old = list(range(1, 16))
    c1 = ScenarioClient(finished_upto=1, picks_by_gw={1: _picks(old, 1)})
    _run(monkeypatch, c1, BETWEEN_1_2)

    boom = httpx.HTTPStatusError(
        "503", request=httpx.Request("GET", "https://x.test"),
        response=httpx.Response(503, request=httpx.Request("GET", "https://x.test")))
    c2 = ScenarioClient(finished_upto=2, picks_by_gw={2: boom})
    _run(monkeypatch, c2, BETWEEN_2_3)

    m = _meta(repo)
    assert m["squad_status"] == gameweek.STATUS_STALE
    # Crucially: it reports GW1 as the source, never the GW2 it failed to fetch.
    assert m["squad_source_event"] == "1"
    assert "503" in m["squad_status_reason"]
    assert {p["id"] for p in _my_team(repo)["players"]} == set(old)
    # A labelled stale squad is still publishable.
    assert contract.validate(repo / "data", min_players=42).ok


def test_scenario_4_404_after_a_deadline_is_not_benign(repo, monkeypatch):
    """No prior squad + a 404 on a readable event = a real failure."""
    c = ScenarioClient(finished_upto=1, picks_by_gw={})  # every gw 404s
    _run(monkeypatch, c, BETWEEN_1_2)
    m = _meta(repo)
    assert m["squad_status"] == gameweek.STATUS_NOT_FOUND
    assert m["squad_source_event"] is None
    # This must NOT publish: a configured entry with an unexplained missing squad.
    rep = contract.validate(repo / "data", min_players=42)
    assert not rep.ok
    assert any(v.artifact == "my_team.json" for v in rep.violations)


# --------------------------------------------------------------------------
# Scenario 5 — a player-history correction
# --------------------------------------------------------------------------

def _hist(fixture, rnd, points, bonus=0):
    return {
        "fixture": fixture, "round": rnd, "total_points": points, "minutes": 90,
        "goals_scored": 0, "assists": 0, "clean_sheets": 0, "goals_conceded": 1,
        "own_goals": 0, "penalties_saved": 0, "penalties_missed": 0,
        "yellow_cards": 0, "red_cards": 0, "saves": 0, "bonus": bonus, "bps": 20,
        "starts": 1, "defensive_contribution": 5, "expected_goals": "0.1",
        "expected_assists": "0.1", "expected_goal_involvements": "0.2",
        "expected_goals_conceded": "1.0", "value": 50, "selected": 100,
        "was_home": True, "opponent_team": 2, "kickoff_time": None,
    }


def test_scenario_5_history_is_persisted_and_corrections_applied(repo, monkeypatch):
    c1 = ScenarioClient(finished_upto=1, picks_by_gw={1: _picks(list(range(1, 16)), 1)},
                        history={1: [_hist(101, 1, 6, bonus=0)]})
    log = _run(monkeypatch, c1, BETWEEN_1_2)
    assert log["ingest"]["player_gw"] >= 1

    conn = _conn(repo)
    row = conn.execute(
        "SELECT * FROM player_gw WHERE player_id=1 AND fixture=101").fetchone()
    assert row["total_points"] == 6 and row["bonus"] == 0
    assert row["season"] == config.SEASON
    conn.close()

    # FPL revises the bonus after review.
    c2 = ScenarioClient(finished_upto=1, picks_by_gw={1: _picks(list(range(1, 16)), 1)},
                        history={1: [_hist(101, 1, 9, bonus=3)]})
    _run(monkeypatch, c2, BETWEEN_1_2)

    conn = _conn(repo)
    rows = conn.execute("SELECT * FROM player_gw WHERE player_id=1").fetchall()
    assert len(rows) == 1, "a correction must update, not duplicate"
    assert rows[0]["total_points"] == 9 and rows[0]["bonus"] == 3
    conn.close()


def test_projection_snapshots_accumulate_across_runs(repo, monkeypatch):
    c = ScenarioClient(finished_upto=0)
    _run(monkeypatch, c, PRE_GW1)
    conn = _conn(repo)
    first = conn.execute(
        "SELECT COUNT(DISTINCT as_of) AS n FROM projection_snapshots").fetchone()["n"]
    conn.close()
    assert first == 1

    _run(monkeypatch, c, PRE_GW1 + timedelta(hours=6))
    conn = _conn(repo)
    n = conn.execute(
        "SELECT COUNT(DISTINCT as_of) AS n FROM projection_snapshots").fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) AS n FROM projection_snapshots").fetchone()["n"]
    # `projections` was replaced, but both runs are retained.
    live = conn.execute("SELECT COUNT(*) AS n FROM projections").fetchone()["n"]
    conn.close()
    assert n == 2, "a second run must add a snapshot, not overwrite the first"
    assert total > live


def test_model_parameters_are_unchanged_by_the_pipeline(repo, monkeypatch):
    """Fixture-strength parameters are untouched; ownership is neutralised (T-14)."""
    from gaffer.model import features as F
    from gaffer.solver import optimize

    c = ScenarioClient(finished_upto=0)
    _run(monkeypatch, c, PRE_GW1)
    # T-12 found no evidence to move these, so they must not have moved.
    assert F.STRENGTH_GAMMA == 1.7
    assert F.STRENGTH_CLAMP == (0.5, 1.85)
    assert optimize.CEILING_WEIGHT == 0.30
    assert optimize.HORIZON_DECAY == 0.84
    # T-14: global ownership no longer weights the points objective at all.
    assert set(optimize.RISK_WEIGHTS.values()) == {0.0}


# --------------------------------------------------------------------------
# Scenario 6 — the strategy step (T-16/17/18/20) inside the real pipeline
# --------------------------------------------------------------------------

def _rival_rows():
    return [
        {"entry": ENTRY, "rank": 1, "total": 60, "event_total": 60,
         "entry_name": "Mine", "player_name": "Me"},
        {"entry": 999, "rank": 2, "total": 58, "event_total": 58,
         "entry_name": "Theirs", "player_name": "Them"},
    ]


def test_scenario_6_strategy_is_exported_and_passes_the_contract(repo, monkeypatch):
    c = ScenarioClient(
        finished_upto=1,
        picks_by_gw={1: _picks(list(range(1, 16)), 1)},
        leagues={271619: _standings(271619, "Crouch Potatoes", _rival_rows()),
                 314: _standings(314, "Overall", _rival_rows(), league_type="s")},
    )
    _run(monkeypatch, c, BETWEEN_1_2)

    s = _strategy(repo)
    assert s["strategy_version"] and s["simulation"]["n_sims"] > 0
    ids = [lg["league_id"] for lg in s["leagues"]]
    assert sorted(ids) == [314, 271619] or sorted(ids) == [271619, 314]
    assert len(ids) == len(set(ids)), "each league exactly once"
    assert s["chips"]["recommendation"] in {"wildcard", "freehit", "bboost", "3xc", "hold"}

    report = contract.validate(repo / "data", expected_entry_id=ENTRY,
                               require_personalised=True, min_players=40)
    assert report.ok, report.render()


def test_scenario_6_no_network_call_is_made_outside_the_stub(repo, monkeypatch):
    """The stub records every league call; a real client would bypass it."""
    c = ScenarioClient(
        finished_upto=1, picks_by_gw={1: _picks(list(range(1, 16)), 1)},
        leagues={271619: _standings(271619, "L", _rival_rows()),
                 314: _standings(314, "O", _rival_rows(), league_type="s")},
    )
    _run(monkeypatch, c, BETWEEN_1_2)
    assert sorted(set(c.league_calls)) == [314, 271619]


def test_scenario_6_a_dead_league_api_does_not_cost_the_recommendation(repo, monkeypatch):
    c = ScenarioClient(finished_upto=1,
                       picks_by_gw={1: _picks(list(range(1, 16)), 1)},
                       leagues={})   # every league 404s
    log = _run(monkeypatch, c, BETWEEN_1_2)

    assert log["solver"]["status"] == "Optimal"
    s = _strategy(repo)
    assert s["leagues"] == []
    assert len(s["league_errors"]) == 2
    report = contract.validate(repo / "data", expected_entry_id=ENTRY,
                               require_personalised=True, min_players=40)
    assert report.ok, report.render()


def test_skip_strategy_writes_no_strategy_artifact(repo, monkeypatch):
    c = ScenarioClient(finished_upto=0)
    log = _run(monkeypatch, c, PRE_GW1, skip_strategy=True)
    assert log["strategy"] == "skipped"
    assert not (repo / "data" / "strategy.json").exists()
    assert c.league_calls == []
    # And the run is still publishable without it.
    report = contract.validate(repo / "data", expected_entry_id=ENTRY,
                               require_personalised=True, min_players=40)
    assert report.ok, report.render()


def test_preseason_strategy_never_recommends_a_chip(repo, monkeypatch):
    c = ScenarioClient(finished_upto=0,
                       leagues={271619: _standings(271619, "L", _rival_rows())})
    _run(monkeypatch, c, PRE_GW1)
    s = _strategy(repo)
    assert s["chips"]["recommendation"] == "hold"
    assert "not readable yet" in s["chips"]["reason"]


# --------------------------------------------------------------------------
# Scenario 7 — the weekly loop (T-21..T-24) inside the real pipeline
# --------------------------------------------------------------------------

def _weekly(root):
    return json.loads((root / "data" / "decision.json").read_text(encoding="utf-8"))


def _live_artifact(root):
    return json.loads((root / "data" / "live.json").read_text(encoding="utf-8"))


def _notifications(root):
    return json.loads((root / "data" / "notifications.json").read_text(encoding="utf-8"))


def test_scenario_7_the_pipeline_publishes_a_weekly_decision(repo, monkeypatch):
    c = ScenarioClient(finished_upto=1,
                       picks_by_gw={1: _picks(list(range(1, 16)), 1)},
                       leagues={271619: _standings(271619, "L", _rival_rows()),
                                314: _standings(314, "O", _rival_rows(), "s")})
    log = _run(monkeypatch, c, BETWEEN_1_2)

    d = _weekly(repo)
    from gaffer import decision as D
    assert d["decision"]["action"] in D.ALL_ACTIONS
    assert d["decision"]["headline"] and d["decision"]["reason"]
    assert d["versions"]["model_version"] and d["versions"]["objective_version"]
    assert d["versions"]["n_sims"] > 0
    assert log["decision"].split()[0] in D.ALL_ACTIONS


def test_scenario_7_the_decision_compares_against_holding(repo, monkeypatch):
    c = ScenarioClient(finished_upto=1,
                       picks_by_gw={1: _picks(list(range(1, 16)), 1)})
    _run(monkeypatch, c, BETWEEN_1_2)
    cmp_ = _weekly(repo)["decision"]["comparison"]
    assert cmp_ is not None
    assert "hold_expected" in cmp_ and "move_expected" in cmp_
    assert 0.0 <= cmp_["p_move_beats_hold"] <= 1.0
    assert cmp_["delta_ci95"][0] <= cmp_["delta_ci95"][1]


def test_scenario_7_a_snapshot_is_recorded_before_the_deadline(repo, monkeypatch):
    """BETWEEN_1_2 is before GW2's deadline, so GW2's advice is recordable."""
    c = ScenarioClient(finished_upto=1,
                       picks_by_gw={1: _picks(list(range(1, 16)), 1)})
    log = _run(monkeypatch, c, BETWEEN_1_2)
    assert log["snapshot"] in ("written", "unchanged")
    conn = _conn(repo)
    row = conn.execute(
        "SELECT target_event, is_pre_deadline FROM decision_snapshots").fetchone()
    conn.close()
    assert row["target_event"] == 2 and row["is_pre_deadline"] == 1


def test_scenario_7_a_second_run_does_not_duplicate_the_snapshot(repo, monkeypatch):
    c = ScenarioClient(finished_upto=1,
                       picks_by_gw={1: _picks(list(range(1, 16)), 1)})
    _run(monkeypatch, c, BETWEEN_1_2)
    log = _run(monkeypatch, c, BETWEEN_1_2 + timedelta(hours=2))
    assert log["snapshot"] == "unchanged"
    conn = _conn(repo)
    n = conn.execute("SELECT COUNT(*) c FROM decision_snapshots").fetchone()["c"]
    conn.close()
    assert n == 1


def test_scenario_7_the_snapshot_stores_a_pre_deadline_distribution(repo, monkeypatch):
    """Without this, T-23 cannot say whether a result was lucky."""
    c = ScenarioClient(finished_upto=1,
                       picks_by_gw={1: _picks(list(range(1, 16)), 1)})
    _run(monkeypatch, c, BETWEEN_1_2)
    from gaffer import snapshots
    conn = _conn(repo)
    snap = snapshots.final_pre_deadline(conn, ENTRY, 2)
    conn.close()
    assert snap is not None
    assert snap.payload.get("outcome_distribution"), "needed to measure luck"


def test_scenario_7_the_published_decision_omits_the_raw_distribution(repo, monkeypatch):
    c = ScenarioClient(finished_upto=1,
                       picks_by_gw={1: _picks(list(range(1, 16)), 1)})
    _run(monkeypatch, c, BETWEEN_1_2)
    assert "outcome_distribution" not in _weekly(repo), "hundreds of floats the UI never reads"


def test_scenario_7_preseason_says_the_squad_is_unknown(repo, monkeypatch):
    c = ScenarioClient(finished_upto=0)
    _run(monkeypatch, c, PRE_GW1)
    from gaffer import decision as D
    d = _weekly(repo)["decision"]
    assert d["action"] == D.ACTION_UNAVAILABLE
    assert "do not know your squad" in d["headline"]


def test_scenario_7_live_is_an_honest_unavailable_state_preseason(repo, monkeypatch):
    c = ScenarioClient(finished_upto=0)
    _run(monkeypatch, c, PRE_GW1)
    live = _live_artifact(repo)
    assert live["available"] is False
    assert live["unavailable_reason"] in ("not_started", "no_gameweek",
                                          "no_squad", "no_live_data")
    assert live["note"]


def test_scenario_7_notifications_are_dry_run(repo, monkeypatch):
    c = ScenarioClient(finished_upto=1,
                       picks_by_gw={1: _picks(list(range(1, 16)), 1)})
    _run(monkeypatch, c, BETWEEN_1_2)
    n = _notifications(repo)
    assert n["result"]["dry_run"] is True
    for a in n["result"]["alerts"]:
        assert a["state"] in ("dry_run", "suppressed")
        assert a["deep_link"].startswith("#/")


def test_scenario_7_no_review_is_fabricated_without_results(repo, monkeypatch):
    c = ScenarioClient(finished_upto=0)
    log = _run(monkeypatch, c, PRE_GW1)
    assert log["review"] == "none"
    assert not (repo / "data" / "review.json").exists()


def test_scenario_7_every_batch5_artifact_passes_the_contract(repo, monkeypatch):
    c = ScenarioClient(finished_upto=1,
                       picks_by_gw={1: _picks(list(range(1, 16)), 1)},
                       leagues={271619: _standings(271619, "L", _rival_rows()),
                                314: _standings(314, "O", _rival_rows(), "s")})
    _run(monkeypatch, c, BETWEEN_1_2)
    report = contract.validate(repo / "data", expected_entry_id=ENTRY,
                               require_personalised=True, min_players=40)
    assert report.ok, report.render()
    assert "decision.json" in report.checked
    assert "live.json" in report.checked
    assert "notifications.json" in report.checked


def test_scenario_7_one_scenario_set_serves_the_whole_run(repo, monkeypatch):
    """The decision and the league probabilities must share one draw."""
    c = ScenarioClient(finished_upto=1,
                       picks_by_gw={1: _picks(list(range(1, 16)), 1)},
                       leagues={271619: _standings(271619, "L", _rival_rows())})
    _run(monkeypatch, c, BETWEEN_1_2)
    dec = _weekly(repo)
    strat = json.loads((repo / "data" / "strategy.json").read_text(encoding="utf-8"))
    assert dec["versions"]["seed"] == strat["simulation"]["seed"]
    assert dec["versions"]["n_sims"] == strat["simulation"]["n_sims"]


def test_a_dry_run_writes_absolutely_nothing(repo, monkeypatch, capsys):
    """Including the two artifacts the AI layer writes outside write_all.

    Those bypass the artifact writer entirely, so an early version of --dry-run
    still modified tracked news.json and verdict.json — a "dry" run that edits
    the repository is worse than no flag at all.
    """
    c = ScenarioClient(finished_upto=0)
    log = _run(monkeypatch, c, PRE_GW1, dry_run=True)
    assert log["artifacts"] == []
    assert log["verdict"] == "skipped (dry run)"
    assert log["news"] == "skipped (dry run)"
    assert list((repo / "data").glob("*.json")) == []

    out = capsys.readouterr().out
    assert "target directory" in out
    assert "DRY RUN" in out


def test_the_writer_announces_every_target_before_writing(repo, monkeypatch, capsys):
    c = ScenarioClient(finished_upto=0)
    _run(monkeypatch, c, PRE_GW1)
    out = capsys.readouterr().out
    for name in ("meta.json", "players.json", "decision.json", "live.json"):
        assert name in out, f"{name} was written without being announced"
    assert "target directory" in out


def test_a_second_run_labels_tracked_files_as_overwrites(repo, monkeypatch, capsys):
    c = ScenarioClient(finished_upto=0)
    _run(monkeypatch, c, PRE_GW1)
    capsys.readouterr()
    _run(monkeypatch, c, PRE_GW1 + timedelta(hours=1))
    out = capsys.readouterr().out
    assert "OVERWRITE (tracked)" in out, \
        "an operator must see which existing files are about to be replaced"
