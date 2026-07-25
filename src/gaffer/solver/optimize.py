"""MILP optimiser over projected points (PuLP + CBC).

Two modes, one formulation:
  * build     — pick the optimal 15 under budget (pre-season / wildcard).
  * transfer  — start from the current squad and choose 0..N transfers,
                trading expected-point gains off against -4 hits.

The objective maximises the captain-weighted starting XI over a decayed
multi-gameweek horizon, so the pick is strong now *and* holds up next few weeks.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import pulp

from gaffer import config

HORIZON_DECAY = 0.84  # weight of each future GW relative to the previous


@dataclass
class Player:
    id: int
    name: str
    position: str
    team_id: int
    price: int
    value: float          # decayed horizon expected points
    next_gw_points: float
    in_squad: bool = False  # currently owned (transfer mode)


@dataclass
class Solution:
    squad: list[int]
    starting: list[int]
    captain: int
    vice: int
    bench: list[int]        # ordered
    formation: str
    squad_value: int        # total price (tenths)
    xi_expected: float      # captain-weighted next-GW expected points
    transfers_in: list[int] = field(default_factory=list)
    transfers_out: list[int] = field(default_factory=list)
    hits: int = 0
    status: str = "optimal"
    meta: dict = field(default_factory=dict)


def load_players(conn: sqlite3.Connection, from_gw: int, horizon: int) -> dict[int, Player]:
    """Aggregate each player's decayed horizon value and next-GW points."""
    owned = {
        r["player_id"]
        for r in conn.execute("SELECT player_id FROM my_squad WHERE gw=?", (from_gw,))
    }
    players: dict[int, Player] = {}
    rows = conn.execute(
        "SELECT pr.player_id, pr.gw, pr.exp_points, pl.web_name, pl.position, "
        "pl.team_id, pl.price FROM projections pr JOIN players pl ON pl.id=pr.player_id "
        "WHERE pr.gw>=? AND pr.gw<?",
        (from_gw, from_gw + horizon),
    )
    for r in rows:
        p = players.get(r["player_id"])
        if p is None:
            p = Player(
                id=r["player_id"], name=r["web_name"], position=r["position"],
                team_id=r["team_id"], price=r["price"], value=0.0, next_gw_points=0.0,
                in_squad=r["player_id"] in owned,
            )
            players[r["player_id"]] = p
        weight = HORIZON_DECAY ** (r["gw"] - from_gw)
        p.value += r["exp_points"] * weight
        if r["gw"] == from_gw:
            p.next_gw_points = r["exp_points"]
    return players


def _formation(counts: dict[str, int]) -> str:
    return f"{counts['DEF']}-{counts['MID']}-{counts['FWD']}"


def optimise(
    conn: sqlite3.Connection,
    from_gw: int,
    horizon: int | None = None,
    max_transfers: int = 2,
    free_transfers: int = 1,
    budget: int | None = None,
) -> Solution:
    horizon = horizon or config.PROJECTION_HORIZON
    players = load_players(conn, from_gw, horizon)
    ids = list(players)
    have_squad = any(p.in_squad for p in players.values())

    prob = pulp.LpProblem("gaffer", pulp.LpMaximize)
    squad = {i: pulp.LpVariable(f"sq_{i}", cat="Binary") for i in ids}
    start = {i: pulp.LpVariable(f"st_{i}", cat="Binary") for i in ids}
    cap = {i: pulp.LpVariable(f"cp_{i}", cat="Binary") for i in ids}

    # --- objective: captain-weighted decayed value, minus transfer hits ----
    obj = pulp.lpSum(start[i] * players[i].value for i in ids)
    # Captaincy is re-chosen every week, so double on *next-GW* points, not horizon.
    obj += pulp.lpSum(cap[i] * players[i].next_gw_points for i in ids)

    hits_var = None
    if have_squad:
        # transfers = players bought (not previously owned)
        bought = pulp.lpSum(squad[i] for i in ids if not players[i].in_squad)
        prob += bought <= max_transfers
        hits_var = pulp.LpVariable("hits", lowBound=0, cat="Integer")
        prob += hits_var >= bought - free_transfers
        obj -= config.HIT_COST * hits_var
    prob += obj

    # --- squad structure ---------------------------------------------------
    prob += pulp.lpSum(squad.values()) == config.SQUAD_SIZE
    for pos, q in config.SQUAD_QUOTA.items():
        prob += pulp.lpSum(squad[i] for i in ids if players[i].position == pos) == q

    # --- starting XI + formation ------------------------------------------
    prob += pulp.lpSum(start.values()) == 11
    for i in ids:
        prob += start[i] <= squad[i]
    prob += pulp.lpSum(start[i] for i in ids if players[i].position == "GKP") == 1
    for pos, lo in config.FORMATION_MIN.items():
        prob += pulp.lpSum(start[i] for i in ids if players[i].position == pos) >= lo

    # --- captain ----------------------------------------------------------
    prob += pulp.lpSum(cap.values()) == 1
    for i in ids:
        prob += cap[i] <= start[i]

    # --- club limit -------------------------------------------------------
    teams = {players[i].team_id for i in ids}
    for t in teams:
        prob += pulp.lpSum(squad[i] for i in ids if players[i].team_id == t) <= config.CLUB_LIMIT

    # --- budget -----------------------------------------------------------
    if budget is None:
        if have_squad:
            owned_value = sum(players[i].price for i in ids if players[i].in_squad)
            bank = int(_meta_int(conn, "bank", 0))
            budget = owned_value + bank
        else:
            budget = config.BUDGET_TENTHS
    prob += pulp.lpSum(squad[i] * players[i].price for i in ids) <= budget

    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    status = pulp.LpStatus[prob.status]

    chosen = [i for i in ids if squad[i].value() and squad[i].value() > 0.5]
    starting = [i for i in ids if start[i].value() and start[i].value() > 0.5]
    captain = next((i for i in ids if cap[i].value() and cap[i].value() > 0.5), starting[0])
    bench = sorted(
        (i for i in chosen if i not in starting),
        key=lambda i: (players[i].position != "GKP", -players[i].value),
    )
    vice = max(
        (i for i in starting if i != captain), key=lambda i: players[i].value, default=captain
    )
    counts = {pos: sum(1 for i in starting if players[i].position == pos)
              for pos in config.POSITIONS}
    xi_expected = (
        sum(players[i].next_gw_points for i in starting) + players[captain].next_gw_points
    )
    t_in = [i for i in chosen if not players[i].in_squad] if have_squad else []
    t_out = [i for i in ids if players[i].in_squad and i not in chosen] if have_squad else []

    return Solution(
        squad=chosen, starting=starting, captain=captain, vice=vice, bench=bench,
        formation=_formation(counts), squad_value=sum(players[i].price for i in chosen),
        xi_expected=round(xi_expected, 2), transfers_in=t_in, transfers_out=t_out,
        hits=int(hits_var.value()) if hits_var is not None else 0,
        status=status, meta={"mode": "transfer" if have_squad else "build"},
    )


def _meta_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    try:
        return int(row["value"]) if row and row["value"] not in (None, "") else default
    except (ValueError, TypeError):
        return default
