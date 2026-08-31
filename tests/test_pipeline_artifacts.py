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
from types import SimpleNamespace

import pytest

from gaffer import config, pipeline
from gaffer.export import artifacts
from gaffer.store import db

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

    def live_event(self, now=None):
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


# ---------------------------------------------------------------------------
# What the live view does when one of its reads does not land.
#
# The same live state is computed twice — here from the pipeline, and in the
# browser by web/src/lib/live/source.ts — and the page renders whichever one
# answered. So a failed read has to produce the same answer on both sides, or
# the honesty of the page depends on which half of the system you got. These
# mirror `fetchLive` in web/src/lib/live/source.test.ts, case for case.
# ---------------------------------------------------------------------------

GW = 5
KICKOFF = "2026-09-19T14:00:00+00:00"
NOW = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)
AS_OF = "2026-09-19T15:00:00+00:00"

# A legal XI and bench out of the conftest pool: GKP 1, four DEF, five MID, one
# FWD, and a bench with the reserve keeper first.
LIVE_XI = [1, 7, 8, 9, 10, 19, 20, 21, 22, 23, 31]
LIVE_BENCH = [2, 11, 24, 32]

# GW5 in progress: 211 banked through GW4, 30 scored this week, a -4 taken for
# it. The cumulative 237 therefore ALREADY contains what the live view computes.
LIVE_HISTORY = {"current": [
    {"event": 1, "points": 62, "total_points": 62, "event_transfers_cost": 0},
    {"event": 2, "points": 51, "total_points": 109, "event_transfers_cost": 4},
    {"event": 3, "points": 70, "total_points": 179, "event_transfers_cost": 0},
    {"event": 4, "points": 40, "total_points": 211, "event_transfers_cost": 8},
    {"event": 5, "points": 30, "total_points": 237, "event_transfers_cost": 4},
]}
# The same gameweek as the picks endpoint reports it, in one row.
PICKS_ROW = {"points": 30, "total_points": 237, "event_transfers_cost": 4}


class LiveClient:
    """Only the reads `_build_live` makes, each independently breakable."""

    def __init__(self, *, live_ids=None, history=None, picks=None):
        self.live_ids = LIVE_XI + LIVE_BENCH if live_ids is None else live_ids
        self.history = history
        self.picks = picks

    def fixtures(self):
        pairs = ((1, 2), (3, 4), (5, 6))   # every club the squad plays for
        return [{"id": 100 + h, "event": GW, "team_h": h, "team_a": a,
                 "minutes": 60, "started": True, "finished": False,
                 "finished_provisional": False, "kickoff_time": KICKOFF,
                 "stats": []} for h, a in pairs]

    def event_live(self, gw):
        return {"elements": [
            {"id": pid, "stats": {"minutes": 90, "total_points": 2, "bps": 0}}
            for pid in self.live_ids]}

    def entry_history(self, entry_id):
        if self.history is None:
            raise RuntimeError("history -> 500")
        return self.history

    def entry_picks(self, entry_id, gw):
        if self.picks is None:
            raise RuntimeError("picks -> 500")
        return self.picks


def _live_settings(league_ids=None):
    return SimpleNamespace(entry_id=7, league_ids=list(league_ids or []))


def _seed_live_squad(conn):
    db.upsert(conn, "my_squad", [
        {"gw": GW, "player_id": pid,
         "is_captain": int(pid == LIVE_XI[0]),
         "is_vice": int(pid == LIVE_XI[1]),
         "multiplier": 1 if pid in LIVE_XI else 0}
        for pid in LIVE_XI + LIVE_BENCH], ["gw", "player_id"])
    db.upsert(conn, "projections", [
        {"player_id": pid, "gw": GW, "exp_points": 3.0}
        for pid in LIVE_XI + LIVE_BENCH], ["player_id", "gw"])


def test_a_readable_history_scores_on_the_total_carried_in(conn):
    _seed_live_squad(conn)
    state = pipeline._build_live(
        conn, LiveClient(history=LIVE_HISTORY), _live_settings(), GW, NOW, AS_OF)
    assert state["available"] is True
    assert state["baseline_source"] == "entry_history"
    assert state["squad"]["season_total_before"] == 211
    assert state["squad"]["hits"] == 4
    assert state["incomplete"] is None


def test_a_dead_history_read_never_falls_back_to_the_season_total(conn):
    """C9. `overall_points` is the season total INCLUDING the gameweek being
    scored — `_live_baseline`'s own docstring said as much — so falling back to
    it added the live score to a figure that already held it, and every screen
    counted this week twice. The browser failed the same moment the opposite
    way, caching a baseline of 0 for the session. The picks payload carries the
    same arithmetic in one row and is a read this path can make anyway, so both
    now recover the exact number instead of inventing a plausible one.
    """
    _seed_live_squad(conn)
    db.set_meta(conn, "overall_points", 237)
    state = pipeline._build_live(
        conn, LiveClient(history=None, picks={"entry_history": PICKS_ROW}),
        _live_settings(), GW, NOW, AS_OF)
    assert state["baseline_source"] == "picks_entry_history"
    assert state["squad"]["season_total_before"] == 211
    assert state["squad"]["hits"] == 4


def test_an_unreadable_season_total_is_withheld_rather_than_guessed(conn):
    """Nothing could supply it, so nothing is shown. A zero baseline renders the
    season total as this gameweek's score, which is not a smaller answer than
    the truth but a different and wrong one."""
    _seed_live_squad(conn)
    db.set_meta(conn, "overall_points", 237)
    state = pipeline._build_live(
        conn, LiveClient(history=None, picks={"picks": []}),
        _live_settings(), GW, NOW, AS_OF)
    assert state["baseline_source"] == "unavailable"
    assert state["squad"]["season_total_before"] is None
    assert state["squad"]["season_total_projected"] is None
    assert "your season total so far" in state["incomplete"]


def test_a_squad_player_missing_from_the_live_feed_is_named(conn):
    """C13. `player_live` invents a row for a squad member the live endpoint did
    not carry, and the invented row holds his full PRE-MATCH projection against
    zero confirmed points — so a truncated payload at 70 minutes reads as "yet
    to kick off" for a man who may already have scored. The invention stays (it
    is what lets a squad render before kick-off) but it is no longer silent."""
    _seed_live_squad(conn)
    absent = LIVE_XI[4]
    state = pipeline._build_live(
        conn,
        LiveClient(history=LIVE_HISTORY,
                   live_ids=[p for p in LIVE_XI + LIVE_BENCH if p != absent]),
        _live_settings(), GW, NOW, AS_OF)
    assert state["missing_players"] == [absent]
    assert "1 of your players missing from the live feed" in state["incomplete"]


def test_a_rival_baseline_excludes_the_gameweek_it_already_contains(monkeypatch):
    """C10. The standings `total` moves during the gameweek, so handing it to
    the scorer as a season baseline adds this week's live points to a figure
    that already contains them. `event_total` is FPL's own account of what this
    gameweek contributed to `total`, so the difference is what was carried in
    and it holds still while the matches run. Mirrors `gatherRivals` in
    web/src/lib/live/source.ts, which subtracts the same pair."""
    from gaffer import league as LG

    rival = LG.RivalEntry(
        entry_id=999, entry_name="Theirs", manager="Them", total=400,
        event_total=60, starting=list(range(1, 12)), bench=[12, 13, 14, 15],
        captain=1, vice=2, picks_status=LG.PICKS_OK, hits=8)
    monkeypatch.setattr(LG, "fetch_league", lambda *a, **k: LG.LeagueState(
        league_id=7, name="L", league_type="x", classification="c", size=2,
        me=7, entries=[rival]))

    rivals = pipeline._live_rivals(object(), _live_settings([7]), GW)
    assert rivals[0]["total"] == 340, "400 already contains this week's 60"
    assert rivals[0]["hits"] == 8, "a -8 read eight points better than reality"


def test_pipeline_labels_a_generic_build(stub_pipeline, monkeypatch):
    import json

    root = stub_pipeline
    monkeypatch.delenv("GAFFER_ENTRY_ID", raising=False)
    pipeline.run(fast=True, horizon=2, now=PRE_GW1)
    meta = json.loads((root / "data" / "meta.json").read_text(encoding="utf-8"))
    assert meta["build_mode"] == "generic"
    assert meta["entry_id"] is None


def test_the_meta_export_carries_the_evidence_for_refusing_the_blend(conn):
    """`build_meta` is an ALLOWLIST. A key the projection layer stamps and
    this list does not name never reaches `meta.json` at all, so the evidence
    for switching the h=1 regime would have stayed stranded in the database."""
    stamped = {"projection_regime": "component_only",
               "ep_next_form_match": "93.1",
               "ep_next_form_sample": "355",
               "ep_next_blend_weight_applied_mean": "0.0"}
    for key, value in stamped.items():
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (key, value))
    meta = artifacts.build_meta(conn, "test-1", settings=config.Settings())
    for key, value in stamped.items():
        assert meta[key] == value, f"{key} never reached the artifact"
