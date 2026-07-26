"""Multi-gameweek transfer-path optimiser (PuLP MILP).

Where ``optimize.py`` picks the single best squad for a decayed horizon, this
plans a *sequence* of moves across GW ``w = 0..H-1``: which players to hold, when
to transfer, when to take a -4, and when to bank a free transfer. It is the
"planner" half of the engine — the thing FPL Review's solver is famous for.

Formulation (all binaries unless noted):
  sq[i,w]  — player i in the 15-man squad in week w
  st[i,w]  — player i starts (in the XI) in week w
  cap[i,w] — player i captained in week w
  buy[i,w] / sell[i,w] — transfers into/out of the squad entering week w
Squad continuity ties them together: sq[i,w] = sq[i,w-1] + buy[i,w] - sell[i,w].

Free transfers are valued (a banked FT ≈ ``FT_VALUE`` pts, so a move must beat
that, not zero), hits cost -4, and leftover bank is mildly rewarded (``ITB_VALUE``)
so the plan doesn't over-commit cash. Prices are treated as static across the
short planning horizon (Gaffer doesn't project price changes), which keeps the
budget linear. Chips are out of scope here (the Chips page handles timing).

The player pool is pre-filtered to a solvable core (top EP per position + the
current squad) so the multi-week binary model stays fast on CBC.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import pulp

from gaffer import config
from gaffer.solver.optimize import _meta_int

HORIZON_DECAY = 0.84       # weight of each future GW relative to the previous
FT_VALUE = 1.5             # points value of a banked free transfer (Solio default)
FT_CAP = 5                 # max rollable free transfers (2026/27 rule)
# Tiny per-transfer friction: a move must produce a *real* EP gain to be worth it,
# so the plan doesn't churn the squad for zero benefit (e.g. in the final week,
# where a banked FT has no future value). Far below any genuine transfer gain.
TRANSFER_EPS = 0.05
# Pool sizes per position — enough to find real moves, small enough to solve fast.
POOL = {"GKP": 6, "DEF": 40, "MID": 45, "FWD": 25}


@dataclass
class PlanPlayer:
    id: int
    name: str
    position: str
    team_id: int
    price: int
    ownership: float
    in_squad: bool
    sell_value: int
    ep: dict[int, float] = field(default_factory=dict)  # gw -> expected points


@dataclass
class GwStep:
    gw: int
    squad: list[int]
    starting: list[int]
    captain: int
    transfers_in: list[int]
    transfers_out: list[int]
    hits: int
    free_transfers: int      # available entering this GW
    xi_expected: float       # captain-weighted XI points this GW


@dataclass
class Plan:
    steps: list[GwStep]
    total_expected: float     # decayed, net of hits, across the horizon
    status: str
    meta: dict = field(default_factory=dict)


def _load_pool(
    conn: sqlite3.Connection, from_gw: int, horizon: int
) -> dict[int, PlanPlayer]:
    """Per-GW EP for a pre-filtered core pool (top EP per position + owned)."""
    owned = {
        r["player_id"]: r["selling_price"]
        for r in conn.execute(
            "SELECT player_id, selling_price FROM my_squad WHERE gw=?", (from_gw,)
        )
    }
    players: dict[int, PlanPlayer] = {}
    rows = conn.execute(
        "SELECT pr.player_id, pr.gw, pr.exp_points, pl.web_name, pl.position, "
        "pl.team_id, pl.price, pl.selected_by_pct FROM projections pr "
        "JOIN players pl ON pl.id=pr.player_id WHERE pr.gw>=? AND pr.gw<?",
        (from_gw, from_gw + horizon),
    )
    for r in rows:
        p = players.get(r["player_id"])
        if p is None:
            is_owned = r["player_id"] in owned
            sell = owned.get(r["player_id"]) if is_owned else 0
            p = PlanPlayer(
                id=r["player_id"], name=r["web_name"], position=r["position"],
                team_id=r["team_id"], price=r["price"],
                ownership=r["selected_by_pct"] or 0.0, in_squad=is_owned,
                sell_value=int(sell) if sell else r["price"],
            )
            players[r["player_id"]] = p
        p.ep[r["gw"]] = r["exp_points"]

    # Pre-filter: keep the top-EP core per position, but never drop an owned
    # player (the plan must be able to reason about selling him).
    def horizon_ep(p: PlanPlayer) -> float:
        return sum(p.ep.values())

    kept: dict[int, PlanPlayer] = {}
    for pos, k in POOL.items():
        pool = sorted(
            (p for p in players.values() if p.position == pos),
            key=horizon_ep, reverse=True,
        )
        for p in pool[:k]:
            kept[p.id] = p
    for p in players.values():
        if p.in_squad:
            kept[p.id] = p
    return kept


def optimise_path(
    conn: sqlite3.Connection,
    from_gw: int,
    horizon: int = 5,
    free_transfers: int = 1,
    budget: int | None = None,
) -> Plan:
    """Plan the optimal transfer sequence across ``horizon`` gameweeks."""
    players = _load_pool(conn, from_gw, horizon)
    ids = list(players)
    gws = list(range(from_gw, from_gw + horizon))
    W = list(range(horizon))  # week index 0..H-1
    have_squad = any(p.in_squad for p in players.values())
    club_limit = _meta_int(conn, "rule_club_limit", config.CLUB_LIMIT)
    budget = budget if budget is not None else _meta_int(conn, "rule_budget", config.BUDGET_TENTHS)
    teams = {players[i].team_id for i in ids}

    prob = pulp.LpProblem("gaffer_path", pulp.LpMaximize)
    sq = {(i, w): pulp.LpVariable(f"sq_{i}_{w}", cat="Binary") for i in ids for w in W}
    st = {(i, w): pulp.LpVariable(f"st_{i}_{w}", cat="Binary") for i in ids for w in W}
    cap = {(i, w): pulp.LpVariable(f"cp_{i}_{w}", cat="Binary") for i in ids for w in W}
    buy = {(i, w): pulp.LpVariable(f"by_{i}_{w}", cat="Binary") for i in ids for w in W}
    sell = {(i, w): pulp.LpVariable(f"sl_{i}_{w}", cat="Binary") for i in ids for w in W}
    # free transfers available entering week w, and paid (hit) transfers in week w
    ft = {w: pulp.LpVariable(f"ft_{w}", lowBound=1, upBound=FT_CAP, cat="Integer") for w in W}
    paid = {w: pulp.LpVariable(f"pd_{w}", lowBound=0, cat="Integer") for w in W}

    def ep(i: int, w: int) -> float:
        return players[i].ep.get(gws[w], 0.0)

    # ---- objective: decayed (XI + captain) − hits + banked-FT + ITB ----
    obj = []
    for w in W:
        decay = HORIZON_DECAY ** w
        obj.append(decay * pulp.lpSum(st[i, w] * ep(i, w) for i in ids))
        obj.append(decay * pulp.lpSum(cap[i, w] * ep(i, w) for i in ids))  # captain doubles
        obj.append(-config.HIT_COST * paid[w])
        obj.append(decay * FT_VALUE * ft[w])  # value carrying a free transfer
        obj.append(-TRANSFER_EPS * decay * pulp.lpSum(buy[i, w] for i in ids))  # friction
    prob += pulp.lpSum(obj)

    # ---- squad structure each week ----
    for w in W:
        prob += pulp.lpSum(sq[i, w] for i in ids) == config.SQUAD_SIZE
        for pos, q in config.SQUAD_QUOTA.items():
            prob += pulp.lpSum(sq[i, w] for i in ids if players[i].position == pos) == q
        # starting XI + formation
        prob += pulp.lpSum(st[i, w] for i in ids) == 11
        prob += pulp.lpSum(st[i, w] for i in ids if players[i].position == "GKP") == 1
        for pos, lo in config.FORMATION_MIN.items():
            prob += pulp.lpSum(st[i, w] for i in ids if players[i].position == pos) >= lo
        for i in ids:
            prob += st[i, w] <= sq[i, w]
            if players[i].position == "GKP" and players[i].price < 45:
                prob += st[i, w] == 0  # never start a £4.0 backup keeper
        # captain
        prob += pulp.lpSum(cap[i, w] for i in ids) == 1
        for i in ids:
            prob += cap[i, w] <= st[i, w]
        # club limit + budget
        for t in teams:
            prob += pulp.lpSum(sq[i, w] for i in ids if players[i].team_id == t) <= club_limit
        prob += pulp.lpSum(sq[i, w] * players[i].price for i in ids) <= budget

    # ---- transfer continuity + FT accounting ----
    # week-0 free transfers are given (rolled from before the horizon)
    prob += ft[0] == max(1, min(FT_CAP, free_transfers))
    for w in W:
        for i in ids:
            prev = (sq[i, w - 1] if w > 0 else (1 if players[i].in_squad else 0))
            # sq[i,w] = prev + buy - sell ; can't buy what you own / sell what you don't
            prob += sq[i, w] == prev + buy[i, w] - sell[i, w]
            prob += buy[i, w] + sell[i, w] <= 1
        tm = pulp.lpSum(buy[i, w] for i in ids)  # transfers made entering week w
        build_assembly = (w == 0 and not have_squad)
        if build_assembly:
            # Building the initial 15 from scratch isn't a set of -4 transfers, so
            # it consumes no free transfers and takes no hit; FTs just roll +1.
            prob += paid[w] == 0
            if w + 1 < horizon:
                prob += ft[w + 1] <= ft[w] + 1
                prob += ft[w + 1] <= FT_CAP
            continue
        # paid = transfers beyond the free allotment (>=0). Solver minimises it
        # (each -4), so it uses all free transfers first.
        prob += paid[w] >= tm - ft[w]
        # FT rollover: next week's FTs = this week's minus those consumed, +1,
        # capped. Consumed = min(tm, ft[w]); with paid pushed to its lower bound
        # (tm - ft when tm>ft), (ft[w] - (tm - paid)) + 1 gives the carry.
        if w + 1 < horizon:
            prob += ft[w + 1] <= ft[w] - (tm - paid[w]) + 1
            prob += ft[w + 1] <= FT_CAP

    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    status = pulp.LpStatus[prob.status]
    if status != "Optimal":
        return Plan(steps=[], total_expected=0.0, status=status, meta={"reason": "infeasible"})

    def chosen(vardict, w: int) -> list[int]:
        return [i for i in ids if vardict[i, w].value() and vardict[i, w].value() > 0.5]

    steps: list[GwStep] = []
    for w in W:
        squad_w = chosen(sq, w)
        start_w = chosen(st, w)
        cap_w = next((i for i in ids if cap[i, w].value() and cap[i, w].value() > 0.5), start_w[0])
        t_in = chosen(buy, w)
        t_out = chosen(sell, w)
        # week 0 in build mode is the initial assembly, not "transfers"
        if w == 0 and not have_squad:
            t_in, t_out = [], []
        xi = sum(ep(i, w) for i in start_w) + ep(cap_w, w)
        steps.append(GwStep(
            gw=gws[w], squad=squad_w, starting=start_w, captain=cap_w,
            transfers_in=t_in, transfers_out=t_out,
            hits=int(round(paid[w].value() or 0)),
            free_transfers=int(round(ft[w].value() or 0)),
            xi_expected=round(xi, 2),
        ))
    total = round(sum(
        HORIZON_DECAY ** w * s.xi_expected - config.HIT_COST * s.hits
        for w, s in enumerate(steps)
    ), 2)
    return Plan(
        steps=steps, total_expected=total, status=status,
        meta={"mode": "transfer" if have_squad else "build", "horizon": horizon},
    )
