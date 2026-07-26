"""Multi-gameweek transfer-path solver: legality, continuity, FT/hit accounting."""

from collections import Counter

from gaffer import config
from gaffer.solver import multiperiod
from gaffer.store import db

HORIZON = 4


def _seed_projections(conn, overrides=None):
    """Give every player a flat per-GW EP across the horizon, so the plan is
    stable unless we plant a better target. ``overrides`` = {(pid, gw): ep}."""
    overrides = overrides or {}
    pids = [r["id"] for r in conn.execute("SELECT id FROM players")]
    rows = []
    for pid in pids:
        for gw in range(1, HORIZON + 1):
            base = 3.0 + (pid % 5) * 0.1  # mild spread so the XI is determinate
            rows.append({
                "player_id": pid, "gw": gw,
                "exp_points": overrides.get((pid, gw), base),
                "p_start": 0.9, "exp_minutes": 85, "confidence": 0.6,
            })
    db.upsert(conn, "projections", rows, ["player_id", "gw"])


def _own_a_squad(conn):
    """Own a legal 15 (the first valid quota fill) so we're in transfer mode."""
    by_pos = {}
    for r in conn.execute("SELECT id, position, team_id FROM players ORDER BY id"):
        by_pos.setdefault(r["position"], []).append((r["id"], r["team_id"]))
    squad = []
    club = Counter()
    for pos, q in config.SQUAD_QUOTA.items():
        picked = 0
        for pid, tid in by_pos[pos]:
            if picked >= q:
                break
            if club[tid] < config.CLUB_LIMIT:
                squad.append(pid)
                club[tid] += 1
                picked += 1
    rows = [{"gw": 1, "player_id": pid, "is_captain": 0, "is_vice": 0,
             "multiplier": 1, "purchase_price": None, "selling_price": None}
            for pid in squad]
    db.upsert(conn, "my_squad", rows, ["gw", "player_id"])
    db.set_meta(conn, "bank", 1000)
    return set(squad)


def test_build_path_is_legal_every_week(conn):
    _seed_projections(conn)
    plan = multiperiod.optimise_path(conn, from_gw=1, horizon=HORIZON, free_transfers=1)
    assert plan.status == "Optimal"
    assert len(plan.steps) == HORIZON
    players = {r["id"]: r for r in conn.execute("SELECT id, position, team_id, price FROM players")}
    for s in plan.steps:
        assert len(s.squad) == config.SQUAD_SIZE
        assert len(s.starting) == 11
        assert Counter(players[i]["position"] for i in s.squad) == \
            {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
        assert max(Counter(players[i]["team_id"] for i in s.squad).values()) <= config.CLUB_LIMIT
        assert sum(players[i]["price"] for i in s.squad) <= config.BUDGET_TENTHS
        assert s.captain in s.starting


def test_squad_is_continuous_across_weeks(conn):
    """No player teleports: week-to-week squad changes must equal buys − sells."""
    _own_a_squad(conn)
    _seed_projections(conn)
    plan = multiperiod.optimise_path(conn, from_gw=1, horizon=HORIZON, free_transfers=1)
    assert plan.status == "Optimal"
    for a, b in zip(plan.steps, plan.steps[1:]):
        added = set(b.squad) - set(a.squad)
        removed = set(a.squad) - set(b.squad)
        assert added == set(b.transfers_in)
        assert removed == set(b.transfers_out)
        assert len(added) == len(removed)  # squad size preserved


def test_no_move_when_nothing_beats_ft_value(conn):
    """Owning the build-optimal squad, a flat projection gives no reason to
    transfer — banking the free transfer (worth FT_VALUE) beats a 0-gain move."""
    _seed_projections(conn)
    # own exactly the squad the build-mode path assembles (already optimal)
    build = multiperiod.optimise_path(conn, from_gw=1, horizon=HORIZON, free_transfers=1)
    owned = set(build.steps[0].squad)
    rows = [{"gw": 1, "player_id": pid, "is_captain": 0, "is_vice": 0,
             "multiplier": 1, "purchase_price": None, "selling_price": None}
            for pid in owned]
    db.upsert(conn, "my_squad", rows, ["gw", "player_id"])
    db.set_meta(conn, "bank", 0)

    plan = multiperiod.optimise_path(conn, from_gw=1, horizon=HORIZON, free_transfers=1)
    assert plan.status == "Optimal"
    total_transfers = sum(len(s.transfers_in) for s in plan.steps)
    assert total_transfers == 0
    assert set(plan.steps[0].squad) == owned


def test_big_upgrade_is_transferred_in(conn):
    """A hugely better unowned MID appears — the plan buys him and eats no hit
    (one free transfer covers it)."""
    owned = _own_a_squad(conn)
    _seed_projections(conn)
    target = next(
        r["id"] for r in conn.execute("SELECT id FROM players WHERE position='MID' ORDER BY id")
        if r["id"] not in owned
    )
    for gw in range(1, HORIZON + 1):
        conn.execute("UPDATE projections SET exp_points=30 WHERE player_id=? AND gw=?", (target, gw))
    plan = multiperiod.optimise_path(conn, from_gw=1, horizon=HORIZON, free_transfers=1)
    assert plan.status == "Optimal"
    assert any(target in s.squad for s in plan.steps)
    # bought with the free transfer in week 0 -> no hit that week
    assert plan.steps[0].hits == 0


def test_two_moves_on_one_ft_costs_a_hit(conn):
    """Two big upgrades but only 1 FT this week: taking both in week 0 costs -4,
    OR the plan splits them across weeks using the rolled FT (either is valid,
    so we assert the accounting is self-consistent, not the specific choice)."""
    owned = _own_a_squad(conn)
    _seed_projections(conn)
    targets = [
        r["id"] for r in conn.execute("SELECT id FROM players WHERE position='MID' ORDER BY id")
        if r["id"] not in owned
    ][:2]
    for t in targets:
        for gw in range(1, HORIZON + 1):
            conn.execute("UPDATE projections SET exp_points=30 WHERE player_id=? AND gw=?", (t, gw))
    plan = multiperiod.optimise_path(conn, from_gw=1, horizon=HORIZON, free_transfers=1)
    assert plan.status == "Optimal"
    # both eventually owned
    assert all(any(t in s.squad for s in plan.steps) for t in targets)
    # hits are consistent with transfers vs available FTs each week
    for s in plan.steps:
        assert s.hits == max(0, len(s.transfers_in) - s.free_transfers)
