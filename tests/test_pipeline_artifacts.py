"""T-01 integration — a full pipeline run must write inside the repository.

This is the test that would have caught the defect that froze the live site for
11 days across 37 green scheduled runs: the pipeline succeeded while writing
every artifact into the Python installation directory.

Runs the real ``pipeline.run()`` against a stubbed FPL client — no network, no
AI credits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gaffer import config, pipeline
from gaffer.export import artifacts

# Fixed clock: before the GW1 deadline of 2026-08-21T17:30Z. Never Date.now().
PRE_GW1 = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

N_PER_POS = {"GKP": 6, "DEF": 14, "MID": 14, "FWD": 8}
_TYPE = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}


def _elements():
    """A pool big enough for a legal 15 and a fast MILP solve."""
    out, pid = [], 1
    for pos, n in N_PER_POS.items():
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


BOOTSTRAP = {
    "teams": [
        {"id": t, "code": t, "name": f"Club{t}", "short_name": f"C{t}",
         "strength_overall_home": 3, "strength_overall_away": 3,
         "strength_attack_home": 0, "strength_attack_away": 0,
         "strength_defence_home": 0, "strength_defence_away": 0}
        for t in range(1, 7)
    ],
    "elements": _elements(),
    "events": [
        {"id": 1, "name": "Gameweek 1", "is_next": True, "finished": False,
         "deadline_time": "2026-08-21T17:30:00Z"},
    ],
    "game_settings": {
        "squad_squadsize": 15, "squad_total_spend": 1000, "squad_team_limit": 3,
        "transfers_cap": 20, "transfers_sell_on_fee": 0.5,
        "max_extra_free_transfers": 4,
    },
    "total_players": 3_124_804,
}

FIXTURES = [
    {"id": 1, "event": 1, "team_h": 1, "team_a": 2, "kickoff_time": None,
     "team_h_difficulty": 3, "team_a_difficulty": 3, "finished": False},
    {"id": 2, "event": 1, "team_h": 3, "team_a": 4, "kickoff_time": None,
     "team_h_difficulty": 3, "team_a_difficulty": 3, "finished": False},
    {"id": 3, "event": 1, "team_h": 5, "team_a": 6, "kickoff_time": None,
     "team_h_difficulty": 3, "team_a_difficulty": 3, "finished": False},
]


class StubClient:
    """Stands in for FplClient — no HTTP, no cache, no credentials."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def bootstrap(self):
        return BOOTSTRAP

    def fixtures(self):
        return FIXTURES

    def current_gw(self):
        return 1

    def last_finished_gw(self):
        return None

    def element_summary(self, pid):  # pragma: no cover - enrichment is skipped
        return {"history_past": [], "history": []}

    def entry_picks(self, entry_id, gw):  # pragma: no cover - never reached pre-GW1
        raise AssertionError(
            "entry_picks must not be called before any deadline has passed"
        )

    # -- used by the strategy step (T-17); no HTTP here either ---------------
    def readable_squad_event(self, now=None):
        return None

    def projection_event(self, now=None):
        return 1

    def entry_history(self, entry_id):
        return {"chips": [], "current": []}

    def league_classic(self, league_id, page=1):
        return {"league": {"id": league_id, "name": f"League {league_id}",
                           "league_type": "x"},
                "standings": {"has_next": False, "results": []},
                "new_entries": {"results": []}}


@pytest.fixture
def stub_pipeline(tmp_path, monkeypatch):
    """Point Gaffer at a throwaway checkout and stub every external call."""
    root = tmp_path / "repo"
    (root / "src" / "gaffer").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='gaffer'\n", encoding="utf-8")

    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.delenv("GAFFER_DATA_DIR", raising=False)
    monkeypatch.setenv("GAFFER_ENTRY_ID", "1066421")
    monkeypatch.setenv("GAFFER_LEAGUE_IDS", "271619,314")
    monkeypatch.setenv("GAFFER_SKIP_ENRICH", "1")
    # No AI: force the deterministic template path, spend nothing.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    config.reload_paths()

    monkeypatch.setattr("gaffer.ingest.FplClient", StubClient)
    # The strategy step opens its own client; without this the suite would make
    # real calls to the live FPL API.
    monkeypatch.setattr("gaffer.pipeline.FplClient", StubClient)
    # ingest_entry_meta hits the network for a configured entry.
    # ingest_my_squad is NOT stubbed: with a pre-GW1 clock the real code resolves
    # squad_gw=None and never calls the API, which is the behaviour under test.
    monkeypatch.setattr("gaffer.ingest.ingest_entry_meta", lambda *a, **k: None)
    # The news digest fetches live RSS; stub it out.
    monkeypatch.setattr(
        "gaffer.ai.news.generate",
        lambda *a, **k: {"count": 0, "source": "stub"},
    )
    return root


def test_pipeline_writes_every_artifact_inside_the_repo(stub_pipeline):
    root = stub_pipeline
    log = pipeline.run(fast=True, horizon=2, now=PRE_GW1)

    written = [Path(p).resolve() for p in log["artifacts"]]
    assert written, "pipeline reported no artifacts"

    # (3)(4) Every generated path resolves inside the repository root.
    for path in written:
        assert path.is_relative_to(root.resolve()), (
            f"artifact escaped the checkout: {path} not under {root}"
        )
        assert path.is_file(), f"reported but not written: {path}"

    # And specifically under <repo>/data, which is what the workflow commits.
    for path in written:
        assert path.parent == (root / "data").resolve()

    names = {p.name for p in written}
    assert {"meta.json", "players.json", "recommendation.json", "plan.json"} <= names


def test_pipeline_output_satisfies_the_artifact_contract(stub_pipeline):
    """T-04 end-to-end: a real run must pass the gate that guards publishing."""
    from gaffer import contract

    root = stub_pipeline
    pipeline.run(fast=True, horizon=2, now=PRE_GW1)
    # min_players is lowered only because the stub pool is small; every other
    # rule is the production one.
    report = contract.validate(root / "data", min_players=len(BOOTSTRAP["elements"]))
    assert report.ok, report.render()


def test_pipeline_rejects_a_site_packages_data_dir(stub_pipeline, tmp_path):
    """(5) The production failure shape must abort, not silently succeed."""
    site = (
        tmp_path / "hostedtoolcache" / "Python" / "3.12.13" / "x64"
        / "lib" / "python3.12" / "data"
    )
    site.mkdir(parents=True)
    import os

    os.environ["GAFFER_DATA_DIR"] = str(site)
    try:
        config.reload_paths()
        assert config.DATA_DIR == site.resolve()
        with pytest.raises(config.PathResolutionError) as exc:
            pipeline.run(fast=True, horizon=2, now=PRE_GW1)
        assert "not inside" in str(exc.value)
        # Nothing may have been written to the rejected location.
        assert not list(site.glob("*.json"))
    finally:
        os.environ.pop("GAFFER_DATA_DIR", None)
        config.reload_paths()


def test_write_all_refuses_an_out_dir_outside_the_repo(conn, tmp_path, monkeypatch):
    """The guard sits in write_all too, so any caller is protected."""
    root = tmp_path / "repo"
    (root / "src" / "gaffer").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    config.reload_paths()

    from gaffer.model import projection
    from gaffer.solver import optimize

    projection.project(conn, 1, 1)
    sol = optimize.optimise(conn, 1, 1, free_transfers=1)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(config.PathResolutionError):
        artifacts.write_all(conn, sol, 1, 1, "test", out_dir=outside)
    assert not list(outside.glob("*.json"))


def test_pipeline_artifacts_share_one_timestamp(stub_pipeline):
    """T-03 — meta/recommendation/plan must agree they came from one run."""
    import json

    root = stub_pipeline
    log = pipeline.run(fast=True, horizon=2, now=PRE_GW1)
    data = root / "data"
    meta = json.loads((data / "meta.json").read_text(encoding="utf-8"))
    rec = json.loads((data / "recommendation.json").read_text(encoding="utf-8"))
    plan = json.loads((data / "plan.json").read_text(encoding="utf-8"))

    stamp = meta["generated_at"]
    assert stamp == log["generated_at"]
    assert rec["generated_at"] == stamp
    if plan is not None:
        assert plan["generated_at"] == stamp
    # ISO 8601 with an explicit offset, not a naive local time.
    assert stamp.endswith("+00:00") or stamp.endswith("Z")


def test_pipeline_labels_a_personalised_build(stub_pipeline):
    """T-02 — a configured run must be explicitly labelled, with the real ids."""
    import json

    root = stub_pipeline
    pipeline.run(fast=True, horizon=2, now=PRE_GW1)
    meta = json.loads((root / "data" / "meta.json").read_text(encoding="utf-8"))
    assert meta["build_mode"] == "personalised"
    assert meta["entry_id"] == 1066421
    assert meta["league_ids"] == [271619, 314]  # multi-league preserved


def test_pipeline_labels_a_generic_build(stub_pipeline, monkeypatch):
    import json

    root = stub_pipeline
    monkeypatch.delenv("GAFFER_ENTRY_ID", raising=False)
    pipeline.run(fast=True, horizon=2, now=PRE_GW1)
    meta = json.loads((root / "data" / "meta.json").read_text(encoding="utf-8"))
    assert meta["build_mode"] == "generic"
    assert meta["entry_id"] is None
