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
from gaffer.solver import objective as OBJ
from gaffer.solver.optimize import _meta_int

HORIZON_DECAY = 0.84       # weight of each future GW relative to the previous
FT_VALUE = 1.5             # points value of a banked free transfer (Solio default)
FT_CAP = 5                 # max rollable free transfers (2026/27 rule)
# TRANSFER_EPS / BENCH_WEIGHT / VICE_WEIGHT used to live here. T-19 moved the
# transfer friction and the bench/vice weights into solver/objective.py so the
# single-window optimiser and this planner cannot disagree; these copies were
# left behind, read by nothing, and free to drift away from the values actually
# in force. Removed by T-27 — see gaffer.solver.objective.


def _pick_solver():
    """Prefer HiGHS (faster on the multi-week binary model); fall back to CBC."""
    try:
        s = pulp.HiGHS_CMD(msg=False)
        if s.available():
            return s
    except Exception:  # HiGHS not installed / not on PATH
        pass
    return pulp.PULP_CBC_CMD(msg=False)
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
    vice: int
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
            # See optimize.load_players: holdings come from the last readable
            # event, not the projected one.
            "SELECT player_id, selling_price FROM my_squad "
            "WHERE gw = (SELECT MAX(gw) FROM my_squad)"
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
    params: OBJ.ObjectiveParams | None = None,
) -> Plan:
    """Plan the optimal transfer sequence across ``horizon`` gameweeks."""
    params = params or OBJ.DEFAULT
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
    vic = {(i, w): pulp.LpVariable(f"vc_{i}_{w}", cat="Binary") for i in ids for w in W}
    buy = {(i, w): pulp.LpVariable(f"by_{i}_{w}", cat="Binary") for i in ids for w in W}
    sell = {(i, w): pulp.LpVariable(f"sl_{i}_{w}", cat="Binary") for i in ids for w in W}
    # Free transfers entering week w; `used` are the free ones actually consumed
    # and `paid` are the hits. `used` is what removes the arbitrage: making it an
    # explicit min(transfers, ft) lets `paid` be an exact consequence rather than
    # a variable the solver can inflate to manufacture future free transfers.
    ft = {w: pulp.LpVariable(f"ft_{w}", lowBound=1, upBound=FT_CAP, cat="Integer") for w in W}
    used = {w: pulp.LpVariable(f"us_{w}", lowBound=0, upBound=FT_CAP, cat="Integer")
            for w in W}
    paid = {w: pulp.LpVariable(f"pd_{w}", lowBound=0, cat="Integer") for w in W}
    # Terminal state, valued so the last modelled week is not treated as the end
    # of the world (which caused transfer dumping) nor as worthless (hoarding).
    ft_end = pulp.LpVariable("ft_end", lowBound=0, upBound=FT_CAP, cat="Integer")

    def ep(i: int, w: int) -> float:
        return players[i].ep.get(gws[w], 0.0)

    # ---- objective: the shared definition in solver.objective --------------
    OBJ.assert_ownership_neutral(params)
    OBJ.assert_no_ft_arbitrage(params)
    obj = []
    for w in W:
        decay = params.decay(w)
        obj.append(decay * pulp.lpSum(st[i, w] * ep(i, w) for i in ids))
        obj.append(decay * pulp.lpSum(cap[i, w] * ep(i, w) for i in ids))  # captain doubles
        obj.append(decay * params.vice_weight * pulp.lpSum(vic[i, w] * ep(i, w) for i in ids))
        # Position-aware bench: a backup keeper is near-worthless, an outfield
        # sub can be auto-subbed in.
        for pos in config.POSITIONS:
            obj.append(decay * params.bench(pos) * pulp.lpSum(
                (sq[i, w] - st[i, w]) * ep(i, w)
                for i in ids if players[i].position == pos))
        # Hits share the gains' time basis: a week-4 hit costs 4 * 0.84^4, not 4.
        obj.append(-params.hit_cost_at(w) * paid[w])
        if params.ft_value:  # 0 by design; see ObjectiveParams.ft_value
            obj.append(decay * params.ft_value * ft[w])
        obj.append(-params.transfer_friction * decay
                   * pulp.lpSum(buy[i, w] for i in ids))
        # Budget-keeper lean, matching optimize.py. Without it the two solvers
        # disagreed on the backup goalkeeper for the same one-week problem.
        obj.append(-params.gk_spend_penalty * decay * pulp.lpSum(
            sq[i, w] * max(0, players[i].price - 45)
            for i in ids if players[i].position == "GKP"))
    # Terminal value: what the plan leaves behind still matters.
    last = horizon - 1
    obj.append(params.terminal_ft_value * ft_end)
    obj.append(params.terminal_squad_value
               * pulp.lpSum(st[i, last] * ep(i, last) for i in ids))
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
        # captain + vice (distinct starters)
        prob += pulp.lpSum(cap[i, w] for i in ids) == 1
        prob += pulp.lpSum(vic[i, w] for i in ids) == 1
        for i in ids:
            prob += cap[i, w] <= st[i, w]
            prob += vic[i, w] <= st[i, w]
            prob += cap[i, w] + vic[i, w] <= 1  # captain ≠ vice
        # club limit
        for t in teams:
            prob += pulp.lpSum(sq[i, w] for i in ids if players[i].team_id == t) <= club_limit
        if not have_squad:
            # Build/wildcard: assemble under the market-price cap.
            prob += pulp.lpSum(sq[i, w] * players[i].price for i in ids) <= budget

    # ---- cash flow through the horizon (T-11) ----------------------------
    # A transfer plan is only useful if you can pay for it. Track the bank week
    # by week: you buy at market price and sell at FPL's selling price, and the
    # bank may never go negative. Previously each week was merely capped at
    # £100.0m of market prices, so a sequence could spend money the squad could
    # not raise. `sell_value` was loaded and never referenced.
    if have_squad:
        start_bank = _meta_int(conn, "bank", 0)
        itb = {
            w: pulp.LpVariable(f"itb_{w}", lowBound=0, cat="Continuous")
            for w in range(horizon + 1)
        }
        prob += itb[0] == start_bank
        for w in W:
            raised = pulp.lpSum(players[i].sell_value * sell[i, w] for i in ids)
            spent = pulp.lpSum(players[i].price * buy[i, w] for i in ids)
            prob += itb[w + 1] == itb[w] + raised - spent
            # Non-negativity of itb[w+1] is implied by its lowBound, which is
            # what makes every step of the sequence executable.
        # Money left over has value too: it buys future flexibility.
        obj.append(params.terminal_bank_value * itb[horizon])
        prob.setObjective(pulp.lpSum(obj))

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
            prob += used[w] == 0
            nxt = ft_end if w + 1 == horizon else ft[w + 1]
            prob += nxt <= ft[w] + 1
            prob += nxt <= FT_CAP
            continue
        # used = min(transfers made, free transfers available). Both bounds plus
        # the solver's incentive to avoid hits pin it exactly, and `paid` is then
        # an exact consequence rather than a free variable.
        prob += used[w] <= tm
        prob += used[w] <= ft[w]
        prob += paid[w] == tm - used[w]
        # Rollover: carry the unused free transfers, add one, cap at five.
        # Because `used` cannot exceed `tm`, inflating `paid` can no longer
        # manufacture a free transfer — the horizon>=6 arbitrage is closed.
        nxt = ft_end if w + 1 == horizon else ft[w + 1]
        prob += nxt <= ft[w] - used[w] + 1
        prob += nxt <= FT_CAP

    prob.solve(_pick_solver())
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
        vic_w = next(
            (i for i in ids if vic[i, w].value() and vic[i, w].value() > 0.5),
            next((i for i in start_w if i != cap_w), cap_w),
        )
        t_in = chosen(buy, w)
        t_out = chosen(sell, w)
        # week 0 in build mode is the initial assembly, not "transfers"
        if w == 0 and not have_squad:
            t_in, t_out = [], []
        xi = sum(ep(i, w) for i in start_w) + ep(cap_w, w)
        steps.append(GwStep(
            gw=gws[w], squad=squad_w, starting=start_w, captain=cap_w, vice=vic_w,
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
