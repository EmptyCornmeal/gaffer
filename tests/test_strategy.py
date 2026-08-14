"""The strategy layer: leagues, chips and the artifact they produce together.

The contract this file defends is that everything published — a placing
probability, a chip's value, a captain comparison — comes from ONE shared
ScenarioSet, and that a league API failure costs the run its league analysis and
nothing else.
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from gaffer import config, contract
from gaffer import league as LG
from gaffer import strategy as ST
from gaffer.export import artifacts
from gaffer.solver.optimize import Solution

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakeScen:
    """A ScenarioSet-shaped double with a known, controllable distribution."""

    sim_version = "scenarios-1.0"

    def __init__(self, means: dict[int, float], n=1000, seed=1):
        rng = np.random.default_rng(seed)
        self.n_sims, self.seed = n, seed
        self.table = {p: rng.normal(m, 2.0, n) for p, m in means.items()}

    def row(self, pid):
        return self.table.get(pid, np.zeros(self.n_sims))

    def squad_points(self, starting, captain=None, bench=None,
                     captain_multiplier=2, bench_boost=False):
        t = np.zeros(self.n_sims)
        for p in starting:
            t += self.row(p)
        if captain is not None:
            t += self.row(captain) * (captain_multiplier - 1)
        if bench_boost and bench:
            for p in bench:
                t += self.row(p)
        return t

    def as_meta(self):
        return {"sim_version": self.sim_version, "n_sims": self.n_sims,
                "seed": self.seed, "model_version": "test-model"}


class FakeClient:
    """Public endpoints only. Raises where the real API would."""

    def __init__(self, leagues=None, picks=None, chips=None, history=None,
                 fail_leagues=()):
        self._leagues = leagues or {}
        self._picks = picks or {}
        self._chips = chips or {"chips": []}
        self._history = history or {"chips": []}
        self._fail = set(fail_leagues)
        self.league_calls, self.picks_calls = [], []

    def league_classic(self, league_id, page=1):
        self.league_calls.append((league_id, page))
        if league_id in self._fail:
            raise RuntimeError(f"league {league_id} unavailable")
        return self._leagues[league_id]

    def entry_picks(self, entry_id, gw):
        self.picks_calls.append((entry_id, gw))
        if (entry_id, gw) not in self._picks:
            raise KeyError("no picks")
        return self._picks[(entry_id, gw)]

    def bootstrap(self):
        return self._chips

    def entry_history(self, entry_id):
        return self._history


def standings(league_id, name, rows, league_type="x"):
    return {
        "league": {"id": league_id, "name": name, "league_type": league_type},
        "standings": {"has_next": False, "results": rows},
        "new_entries": {"results": []},
    }


def row(entry, rank, total, name=None):
    return {"entry": entry, "rank": rank, "total": total,
            "entry_name": name or f"Team {entry}", "player_name": f"M{entry}",
            "event_total": 0}


def picks(ids, captain, vice=None, bench=None):
    bench = bench or []
    out = []
    for i, pid in enumerate(ids, start=1):
        out.append({"element": pid, "position": i,
                    "is_captain": pid == captain,
                    "is_vice_captain": pid == vice,
                    "multiplier": 2 if pid == captain else 1})
    for i, pid in enumerate(bench, start=12):
        out.append({"element": pid, "position": i, "is_captain": False,
                    "is_vice_captain": False, "multiplier": 0})
    return {"picks": out, "entry_history": {"event_transfers_cost": 0}}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
    for pid in range(1, 31):
        c.execute(
            "INSERT INTO players (id, code, web_name, team_id, position, price) "
            "VALUES (?,?,?,?,?,?)",
            (pid, 1000 + pid, f"P{pid}", 1 + pid % 5,
             ["GKP", "DEF", "MID", "FWD"][pid % 4], 50),
        )
    c.commit()
    return c


def seed_squad(conn, starting, bench, captain, vice, gw=7):
    for pid in starting + bench:
        conn.execute(
            "INSERT INTO my_squad (gw, player_id, is_captain, is_vice, multiplier, "
            "purchase_price, selling_price, price_source, price_exact) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (gw, pid, 1 if pid == captain else 0, 1 if pid == vice else 0,
             0 if pid in bench else 1, 50, 50, "transfer_in"),
        )
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('bank', '5')")
    conn.commit()


# --------------------------------------------------------------------------
# Squad state
# --------------------------------------------------------------------------

def test_stored_squad_splits_xi_bench_and_armband(conn):
    seed_squad(conn, list(range(1, 12)), [12, 13, 14, 15], captain=3, vice=4)
    s = ST.stored_squad(conn)
    assert len(s["starting"]) == 11 and len(s["bench"]) == 4
    assert s["captain"] == 3 and s["vice"] == 4
    assert s["source_event"] == 7


def test_no_stored_squad_is_none_not_empty(conn):
    assert ST.stored_squad(conn) is None


def test_wildcard_budget_is_selling_value_plus_bank(conn):
    seed_squad(conn, list(range(1, 12)), [12, 13, 14, 15], captain=3, vice=4)
    assert ST._wildcard_budget(conn) == 15 * 50 + 5


def test_wildcard_budget_is_unknown_without_a_squad(conn):
    assert ST._wildcard_budget(conn) is None


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------

def test_a_small_league_target_is_winning_it():
    st = LG.LeagueState(1, "Crouch Potatoes", "x", LG.TINY, 4, me=1,
                        entries=[LG.RivalEntry(i) for i in range(1, 5)])
    assert ST.default_target(st) == 1


def test_a_large_league_target_is_a_percentile_not_first():
    st = LG.LeagueState(2, "Big", "s", LG.GLOBAL, 900000, me=1,
                        entries=[LG.RivalEntry(i) for i in range(1, 61)])
    assert ST.default_target(st) == 6


# --------------------------------------------------------------------------
# League fetching
# --------------------------------------------------------------------------

def test_one_failing_league_does_not_lose_the_others():
    client = FakeClient(
        leagues={1: standings(1, "A", [row(100, 1, 50)]),
                 2: standings(2, "B", [row(100, 1, 50)])},
        fail_leagues={1},
    )
    states, errors = ST.fetch_leagues(client, [1, 2], 100, squad_event=None)
    assert [s.league_id for s in states] == [2]
    assert errors and errors[0]["league_id"] == 1


def test_league_count_is_bounded_and_the_truncation_is_reported():
    ids = list(range(1, 12))
    client = FakeClient(leagues={i: standings(i, str(i), [row(100, 1, 5)]) for i in ids})
    states, errors = ST.fetch_leagues(client, ids, 100, squad_event=None)
    assert len(states) == ST.MAX_LEAGUES
    assert any("were analysed" in e["error"] for e in errors)


# --------------------------------------------------------------------------
# Options and conflicts
# --------------------------------------------------------------------------

def _two_league_setup():
    """Two leagues that want different captains.

    League A: rivals already own player 1 (the best player), so captaining him is
    a shield. League B: rivals own player 2, so player 1 is the differential.
    """
    scen = FakeScen({1: 8.0, 2: 7.0, 3: 3.0, **{p: 2.0 for p in range(4, 16)}})
    a = LG.LeagueState(10, "A", "x", LG.TINY, 3, me=100, entries=[
        LG.RivalEntry(100, total=100, picks_status=LG.PICKS_OK,
                      starting=list(range(1, 12)), captain=1),
        LG.RivalEntry(200, total=100, picks_status=LG.PICKS_OK,
                      starting=list(range(1, 12)), captain=1),
    ])
    b = LG.LeagueState(20, "B", "x", LG.TINY, 3, me=100, entries=[
        LG.RivalEntry(100, total=100, picks_status=LG.PICKS_OK,
                      starting=list(range(1, 12)), captain=1),
        LG.RivalEntry(300, total=100, picks_status=LG.PICKS_OK,
                      starting=list(range(1, 12)), captain=2),
    ])
    return scen, [a, b]


def test_options_are_scored_in_every_league_at_once():
    scen, states = _two_league_setup()
    names = {p: f"P{p}" for p in range(1, 16)}
    opts = ST.build_options(scen, states, list(range(1, 12)),
                            [1, 2], names, gws_remaining=1)
    assert len(opts) == 2
    for o in opts:
        assert set(o.p_target) == {"10", "20"}
        assert all(0.0 <= v <= 1.0 for v in o.p_target.values())


def test_captain_candidates_are_ranked_by_expected_points():
    scen = FakeScen({1: 2.0, 2: 9.0, 3: 5.0, 4: 1.0}, n=20000)
    ranked = ST.captain_options(scen, [1, 2, 3, 4], {}, limit=3)
    assert ranked[0] == 2 and ranked[1] == 3


def test_a_captain_choice_that_splits_the_leagues_is_reported_as_a_conflict():
    scen, states = _two_league_setup()
    names = {p: f"P{p}" for p in range(1, 16)}
    opts = ST.build_options(scen, states, list(range(1, 12)), [1, 2], names, 1)
    from gaffer import multileague as ML
    res = ML.resolve(opts, None, ["10", "20"])
    # Either one option dominates, or the split is surfaced rather than averaged.
    assert res["default"] is not None or res["conflicts"] or res["shortlist"]
    assert res["reason"]


# --------------------------------------------------------------------------
# Chips
# --------------------------------------------------------------------------

LIVE_CHIPS = {"chips": [
    {"name": "bboost", "number": 1, "start_event": 1, "stop_event": 19,
     "chip_type": "team"},
    {"name": "3xc", "number": 1, "start_event": 1, "stop_event": 19,
     "chip_type": "team"},
]}


def test_chip_block_never_recommends_a_played_chip():
    scen = FakeScen({p: 5.0 for p in range(1, 16)})
    client = FakeClient(chips=LIVE_CHIPS,
                        history={"chips": [{"name": "bboost", "event": 3},
                                           {"name": "3xc", "event": 4}]})
    block = ST.chip_block(client, scen, 100, 7, list(range(1, 12)),
                          [12, 13, 14, 15], captain=1, free_sol=None,
                          weeks_retained=4)
    assert block["recommendation"] == "hold"
    assert set(block["used"]) == {"bboost", "3xc"}


def test_a_failed_history_fetch_does_not_empty_the_chip_ledger():
    """The defect: `except Exception: used = []` made every chip look unused, so
    an ordinary transient API error could recommend a chip already spent."""
    class NoHistory(FakeClient):
        def entry_history(self, entry_id):
            raise RuntimeError("502 from the entry endpoint")

    scen = FakeScen({p: 6.0 for p in range(1, 16)}, n=4000)
    block = ST.chip_block(NoHistory(chips=LIVE_CHIPS), scen, 100, 7,
                          list(range(1, 12)), [12, 13, 14, 15], 1, None, 4)
    assert block["state_known"] is False
    assert block["recommendation"] == "hold", \
        "a chip worth 24 points must still not be recommended on an unknown ledger"
    assert "already played" in block["reason"]


def test_a_readable_ledger_is_marked_known():
    scen = FakeScen({p: 6.0 for p in range(1, 16)}, n=4000)
    block = ST.chip_block(FakeClient(chips=LIVE_CHIPS), scen, 100, 7,
                          list(range(1, 12)), [12, 13, 14, 15], 1, None, 4)
    assert block["state_known"] is True


def test_chip_block_survives_an_api_failure():
    class Broken(FakeClient):
        def bootstrap(self):
            raise RuntimeError("down")

        def entry_history(self, entry_id):
            raise RuntimeError("down")

    scen = FakeScen({p: 5.0 for p in range(1, 16)})
    block = ST.chip_block(Broken(), scen, 100, 7, list(range(1, 12)),
                          [12, 13, 14, 15], 1, None, 4)
    assert block["recommendation"] == "hold"
    assert block["available"] == []


def test_a_strong_bench_boost_is_recommended_when_available():
    scen = FakeScen({p: 6.0 for p in range(1, 16)}, n=4000)
    client = FakeClient(chips=LIVE_CHIPS)
    block = ST.chip_block(client, scen, 100, 7, list(range(1, 12)),
                          [12, 13, 14, 15], 1, None, 4)
    # Four bench players at 6.0 each is ~24 points: well past the bar.
    assert block["recommendation"] == "bboost"
    assert block["expected_gain"] > 20


# --------------------------------------------------------------------------
# End-to-end assembly
# --------------------------------------------------------------------------

def _build(conn, monkeypatch, *, leagues=None, entry=100, league_ids=(10,),
           squad=True):
    scen = FakeScen({p: 5.0 + (p % 3) for p in range(1, 31)})
    monkeypatch.setattr(ST.SC, "simulate", lambda *a, **k: scen)
    if squad:
        seed_squad(conn, list(range(1, 12)), [12, 13, 14, 15], 3, 4)
    client = FakeClient(
        leagues=leagues or {10: standings(10, "Crouch Potatoes",
                                          [row(100, 1, 120), row(200, 2, 118)])},
        picks={(200, 7): picks(list(range(2, 13)), captain=2)},
        chips=LIVE_CHIPS,
    )
    settings = config.Settings(entry_id=entry, league_ids=list(league_ids))
    sol = Solution(squad=list(range(1, 16)), starting=list(range(1, 12)),
                   captain=3, vice=4, bench=[12, 13, 14, 15], formation="4-4-2",
                   squad_value=750, xi_expected=40.0)
    return ST.build(conn, client, settings, from_gw=8, squad_event=7, sol=sol,
                    generated_at="2026-08-06T12:00:00+00:00"), scen


def test_build_produces_a_complete_versioned_artifact(conn, monkeypatch):
    strat, scen = _build(conn, monkeypatch)
    assert strat["strategy_version"] == ST.STRATEGY_VERSION
    assert strat["simulation"]["n_sims"] == scen.n_sims
    assert strat["gameweek"] == 8
    assert len(strat["leagues"]) == 1
    assert strat["leagues"][0]["league_id"] == 10
    assert strat["chips"]["recommendation"] in {"bboost", "3xc", "hold"}
    assert strat["limitations"]


def test_build_uses_the_stored_squad_when_one_exists(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch)
    assert strat["basis"] == "your stored squad"
    assert strat["squad"]["source_event"] == 7


def test_build_falls_back_to_the_recommendation_and_says_so(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch, squad=False)
    assert "recommended squad" in strat["basis"]
    assert any("stand-in" in x for x in strat["limitations"])


def test_build_skips_leagues_entirely_without_an_entry_id(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch, entry=None)
    assert strat["leagues"] == []
    assert strat["options"] == []
    assert strat["resolution"]["default"] is None


def test_every_probability_is_a_probability(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch)
    for lg in strat["leagues"]:
        assert 0.0 <= lg["placing"]["p_first"] <= 1.0
        assert 0.0 <= lg["placing"]["p_target"] <= 1.0
    for opt in strat["options"]:
        assert all(0.0 <= v <= 1.0 for v in opt["p_target"].values())


def test_each_league_appears_exactly_once(conn, monkeypatch):
    leagues = {10: standings(10, "A", [row(100, 1, 120), row(200, 2, 118)]),
               20: standings(20, "B", [row(100, 1, 120), row(300, 2, 90)])}
    strat, _ = _build(conn, monkeypatch, leagues=leagues, league_ids=(10, 20))
    ids = [lg["league_id"] for lg in strat["leagues"]]
    assert sorted(ids) == [10, 20]
    assert len(ids) == len(set(ids))


def test_one_leagues_ownership_never_leaks_into_another(conn, monkeypatch):
    """League B's only rival owns players 20-30; league A's owns 2-12."""
    leagues = {10: standings(10, "A", [row(100, 1, 120), row(200, 2, 118)]),
               20: standings(20, "B", [row(100, 1, 120), row(300, 2, 118)])}
    scen = FakeScen({p: 5.0 for p in range(1, 31)})
    monkeypatch.setattr(ST.SC, "simulate", lambda *a, **k: scen)
    seed_squad(conn, list(range(1, 12)), [12, 13, 14, 15], 3, 4)
    client = FakeClient(
        leagues=leagues,
        picks={(200, 7): picks(list(range(2, 13)), captain=2),
               (300, 7): picks(list(range(20, 31)), captain=20)},
        chips=LIVE_CHIPS,
    )
    settings = config.Settings(entry_id=100, league_ids=[10, 20])
    sol = Solution(list(range(1, 16)), list(range(1, 12)), 3, 4,
                   [12, 13, 14, 15], "4-4-2", 750, 40.0)
    strat = ST.build(conn, client, settings, from_gw=8, squad_event=7, sol=sol)
    by_id = {lg["league_id"]: lg for lg in strat["leagues"]}
    # In A the rival owns 2..12, so those are shields; in B he owns none of mine,
    # so my whole XI is differential.
    a_shield_ids = {s["player_id"] for s in by_id[10]["shields"]}
    b_shield_ids = {s["player_id"] for s in by_id[20]["shields"]}
    assert a_shield_ids and not b_shield_ids
    assert len(by_id[20]["differentials"]) > 0


def test_a_league_with_no_published_field_never_claims_a_certain_win(conn, monkeypatch):
    """The live pre-season run reported p_first = 1.000 for five global leagues."""
    empty = {"league": {"id": 314, "name": "Overall", "league_type": "s"},
             "standings": {"has_next": False, "results": []},
             "new_entries": {"results": []}}
    scen = FakeScen({p: 5.0 for p in range(1, 31)})
    monkeypatch.setattr(ST.SC, "simulate", lambda *a, **k: scen)
    seed_squad(conn, list(range(1, 12)), [12, 13, 14, 15], 3, 4)
    client = FakeClient(leagues={314: empty}, chips=LIVE_CHIPS)
    settings = config.Settings(entry_id=100, league_ids=[314])
    sol = Solution(list(range(1, 16)), list(range(1, 12)), 3, 4,
                   [12, 13, 14, 15], "4-4-2", 750, 40.0)
    strat = ST.build(conn, client, settings, from_gw=8, squad_event=7, sol=sol)
    p = strat["leagues"][0]["placing"]
    assert p["available"] is False
    assert p["p_first"] == 0.0
    # And an unmeasurable league gets no vote in the multi-league resolution.
    assert strat["options"] == []
    assert "measurable" in strat["resolution"]["reason"]


def test_the_contract_rejects_a_certainty_with_no_field(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch)
    lg = strat["leagues"][0]
    lg["placing"].update({"p_first": 1.0, "available": True})
    lg["data_quality"]["rivals"] = 0
    report = contract.Report(data_dir=".")
    contract._check_strategy(strat, report)
    assert any("artefact, not a forecast" in v.expected for v in report.violations)


def test_the_contract_requires_an_availability_flag(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch)
    del strat["leagues"][0]["placing"]["available"]
    report = contract.Report(data_dir=".")
    contract._check_strategy(strat, report)
    assert any(v.field.endswith("placing.available") for v in report.violations)


def test_pre_season_never_recommends_a_chip(conn, monkeypatch):
    """No readable squad means the chip would be spent on a team you don't own."""
    strat, _ = _build(conn, monkeypatch, squad=False)
    assert strat["chips"]["recommendation"] == "hold"
    assert "not readable yet" in strat["chips"]["reason"]


def test_a_total_league_outage_still_produces_the_chip_plan(conn, monkeypatch):
    scen = FakeScen({p: 6.0 for p in range(1, 31)})
    monkeypatch.setattr(ST.SC, "simulate", lambda *a, **k: scen)
    seed_squad(conn, list(range(1, 12)), [12, 13, 14, 15], 3, 4)
    client = FakeClient(leagues={}, chips=LIVE_CHIPS, fail_leagues={10})
    settings = config.Settings(entry_id=100, league_ids=[10])
    sol = Solution(list(range(1, 16)), list(range(1, 12)), 3, 4,
                   [12, 13, 14, 15], "4-4-2", 750, 40.0)
    strat = ST.build(conn, client, settings, from_gw=8, squad_event=7, sol=sol)
    assert strat["leagues"] == []
    assert strat["league_errors"]
    assert strat["chips"]["recommendation"] == "bboost"


# --------------------------------------------------------------------------
# Export + contract
# --------------------------------------------------------------------------

def _players_index():
    return [{"id": p, "name": f"P{p}", "team": "ARS", "pos": "MID", "price": 5.0,
             "code": 1000 + p, "team_code": 3, "next_gw_xp": 4.0}
            for p in range(1, 31)]


def test_export_resolves_ids_into_cards(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch)
    out = artifacts.build_strategy(strat, _players_index(),
                                   generated_at="2026-08-06T12:00:00+00:00")
    assert out["squad"]["starting"][0]["name"].startswith("P")
    assert out["squad"]["captain"]["id"] == 3
    for lg in out["leagues"]:
        for s in lg["shields"]:
            assert s["player"]["id"] == s["player_id"]


def test_the_contract_accepts_a_real_strategy_artifact(tmp_path, conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch)
    out = artifacts.build_strategy(strat, _players_index(),
                                   generated_at="2026-08-06T12:00:00+00:00")
    (tmp_path / "strategy.json").write_text(json.dumps(out), encoding="utf-8")
    report = contract.Report(data_dir=str(tmp_path))
    contract._check_strategy(out, report, expected_league_ids=[10])
    assert report.ok, report.render()


def test_the_contract_rejects_a_duplicated_league(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch)
    strat["leagues"] = strat["leagues"] * 2
    report = contract.Report(data_dir=".")
    contract._check_strategy(strat, report)
    assert any("exactly once" in v.expected for v in report.violations)


def test_the_contract_rejects_an_out_of_range_probability(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch)
    strat["leagues"][0]["placing"]["p_target"] = 1.4
    report = contract.Report(data_dir=".")
    contract._check_strategy(strat, report)
    assert any(v.field.endswith("p_target") for v in report.violations)


def test_the_contract_rejects_recommending_a_spent_chip(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch)
    strat["chips"]["recommendation"] = "bboost"
    strat["chips"]["used"] = ["bboost"]
    report = contract.Report(data_dir=".")
    contract._check_strategy(strat, report)
    assert any("not already been played" in v.expected for v in report.violations)


def test_the_contract_rejects_an_unknown_version(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch)
    strat["league_version"] = "league-99"
    report = contract.Report(data_dir=".")
    contract._check_strategy(strat, report)
    assert any(v.field == "league_version" for v in report.violations)


def test_the_contract_rejects_missing_simulation_provenance(conn, monkeypatch):
    strat, _ = _build(conn, monkeypatch)
    del strat["simulation"]["seed"]
    report = contract.Report(data_dir=".")
    contract._check_strategy(strat, report)
    assert any(v.field == "simulation.seed" for v in report.violations)


def test_a_failed_strategy_build_is_publishable_only_if_it_says_so():
    report = contract.Report(data_dir=".")
    contract._check_strategy(
        {"error": "boom", "strategy_version": ST.STRATEGY_VERSION,
         "generated_at": "2026-08-06T12:00:00+00:00", "gameweek": 8}, report)
    assert report.ok
    bad = contract.Report(data_dir=".")
    contract._check_strategy({"error": "boom"}, bad)
    assert not bad.ok
