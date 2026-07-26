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

# Effective-ownership dial → template_weight. 0 = pure points-per-£ (differential,
# ignores the crowd); higher = own more of the template for rank protection.
# Tuned so 'balanced' pulls a ~74%-owned default-captain premium (Haaland) into
# the squad, which pure value drops. Balanced is the shipped default.
# High absolute weights because, with fixtures now read correctly, the model's
# honest optimum is a *no-Haaland* value build (Bruno at Hull / Gabriel vs Coventry
# out-project Haaland vs mid-table Bournemouth) — so owning the 74%-must-own is a
# deliberate rank-defence override. differential = the model's sharp value view;
# balanced = owns + captains Haaland (rank-safe default); template = max crowd.
RISK_WEIGHTS = {"differential": 0.0, "balanced": 8.0, "template": 11.0}

# Reward next-GW UPSIDE (Monte-Carlo ceiling above the mean) in the XI and the
# captain. Without this the solver maximises mean points and (a) drops elite
# forwards whose mean is modest but ceiling is elite, and (b) stacks 5 low-ceiling
# DEFCON defenders into a thin lone-striker shape at longer horizons. FPL rank is
# driven by ceiling, so this pulls premiums + a real second forward into every
# horizon consistently.
CEILING_WEIGHT = 0.30

# Budget-keeper lean: the consensus is a cheap playing GK (£4.5) + a £4.0 bench,
# spending the saving outfield — a premium keeper's ~1pt/season edge rarely beats
# what that £1.5m buys elsewhere. Penalise GK spend above the £4.5 tier so the
# solver only takes a pricier keeper when he's clearly worth it. (per £0.1m)
GK_SPEND_PENALTY = 0.10


@dataclass
class Player:
    id: int
    name: str
    position: str
    team_id: int
    price: int            # current market price (cost to buy)
    value: float          # decayed horizon expected points
    next_gw_points: float
    ownership: float = 0.0  # selected-by %, an effective-ownership proxy (rank defence)
    ceiling: float = 0.0    # 90th-pct next-GW outcome (Monte-Carlo), for chase-captaincy
    in_squad: bool = False  # currently owned (transfer mode)
    sell_value: int = 0   # what we'd recoup if sold (FPL selling price; owned only)


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
    # owned player_id -> selling price (None until we have purchase data)
    owned = {
        r["player_id"]: r["selling_price"]
        for r in conn.execute(
            "SELECT player_id, selling_price FROM my_squad WHERE gw=?", (from_gw,)
        )
    }
    players: dict[int, Player] = {}
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
            # sell value = FPL selling price when known, else current market price
            sell = owned.get(r["player_id"]) if is_owned else 0
            p = Player(
                id=r["player_id"], name=r["web_name"], position=r["position"],
                team_id=r["team_id"], price=r["price"], value=0.0, next_gw_points=0.0,
                ownership=r["selected_by_pct"] or 0.0,
                in_squad=is_owned, sell_value=int(sell) if sell else r["price"],
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
    max_transfers: int | None = None,
    free_transfers: int = 1,
    budget: int | None = None,
    template_weight: float = 0.0,
    distributions: dict[int, dict[str, float]] | None = None,
) -> Solution:
    horizon = horizon or config.PROJECTION_HORIZON
    players = load_players(conn, from_gw, horizon)
    if distributions:
        for pid, p in players.items():
            d = distributions.get(pid)
            if d:
                p.ceiling = d.get("ceiling", 0.0)
    ids = list(players)
    # How much `value` grows with the horizon (decayed GW count) — scale the
    # next-GW ceiling term by this so its pull stays proportional to value at
    # every horizon (otherwise structure drifts as the window lengthens).
    horizon_factor = sum(HORIZON_DECAY**k for k in range(horizon))
    have_squad = any(p.in_squad for p in players.values())
    # No hard cap by default — the -4 hit cost self-limits how many transfers are
    # ever worth making. (Was hardcoded to 2, which blocked profitable big moves.)
    if max_transfers is None:
        max_transfers = config.SQUAD_SIZE

    prob = pulp.LpProblem("gaffer", pulp.LpMaximize)
    squad = {i: pulp.LpVariable(f"sq_{i}", cat="Binary") for i in ids}
    start = {i: pulp.LpVariable(f"st_{i}", cat="Binary") for i in ids}
    cap = {i: pulp.LpVariable(f"cp_{i}", cat="Binary") for i in ids}

    # --- objective: captain-weighted decayed value, minus transfer hits ----
    obj = pulp.lpSum(start[i] * players[i].value for i in ids)
    # Captaincy is re-chosen every week, so double on *next-GW* points, not horizon.
    obj += pulp.lpSum(cap[i] * players[i].next_gw_points for i in ids)

    # --- effective-ownership / template term (rank defence) ----------------
    # FPL rank is zero-sum: a highly-owned player who hauls hurts you if you don't
    # own him. A pure points-per-£ optimiser is blind to this and will drop a
    # near-must-own like Haaland. This term rewards owning high-ownership,
    # high-projection players so the squad defends rank; template_weight is the
    # risk dial (0 = pure differential/value, higher = more template-safe).
    if template_weight:
        # Rank-defence overlay: a heavily-owned player is a rank risk whatever his
        # price, so weight ownership super-linearly (own**1.6) — this makes the
        # ~74%-owned must-own (Haaland) dominate a merely-popular pick, so balanced
        # owns the true template core rather than trading it for cheaper value.
        # next-GW points (not horizon value) keeps it horizon-invariant.
        obj += template_weight * pulp.lpSum(
            start[i] * (players[i].ownership / 100.0) * players[i].next_gw_points
            for i in ids
        )
        # Captaincy is the biggest rank lever, so make it EO-aware too: under a
        # template stance, captaining a heavily-owned player (the crowd's captain)
        # defends rank even at a small expected-points cost.
        obj += template_weight * pulp.lpSum(
            cap[i] * (players[i].ownership / 100.0) * players[i].next_gw_points
            for i in ids
        )

    # --- ceiling / upside term (rank is driven by ceiling, not mean) --------
    # Reward the next-GW upside (ceiling above mean) of the starting XI and, more
    # heavily, the captain (whose upside is doubled). Scaled by horizon_factor so
    # premiums + a second forward are valued the same at 1/3/5 GWs.
    upside = {i: max(0.0, players[i].ceiling - players[i].next_gw_points) for i in ids}
    if any(upside.values()):
        obj += CEILING_WEIGHT * horizon_factor * pulp.lpSum(
            start[i] * upside[i] for i in ids
        )
        obj += CEILING_WEIGHT * 2.0 * pulp.lpSum(cap[i] * upside[i] for i in ids)

    # --- budget-keeper lean (scaled by horizon so it stays proportional to value)
    obj -= GK_SPEND_PENALTY * horizon_factor * pulp.lpSum(
        squad[i] * max(0, players[i].price - 45)
        for i in ids
        if players[i].position == "GKP"
    )

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

    # Never START a £4.0 keeper — those are non-playing backups (bench fodder).
    # Without this the solver funds a template squeeze by starting a £4.0 GK that
    # scores ~0; real managers always start a £4.5+ playing keeper. (£4.0 keepers
    # can still be bought as the mandatory second/bench GK.)
    for i in ids:
        if players[i].position == "GKP" and players[i].price < 45:
            prob += start[i] == 0

    # --- captain ----------------------------------------------------------
    prob += pulp.lpSum(cap.values()) == 1
    for i in ids:
        prob += cap[i] <= start[i]

    # --- club limit -------------------------------------------------------
    # Prefer the live rule (ingested from the API's game_settings); fall back to
    # the hardcoded constant so the solver still runs on a bare DB.
    club_limit = _meta_int(conn, "rule_club_limit", config.CLUB_LIMIT)
    teams = {players[i].team_id for i in ids}
    for t in teams:
        prob += pulp.lpSum(squad[i] for i in ids if players[i].team_id == t) <= club_limit

    # --- budget -----------------------------------------------------------
    # Transfer mode models real cash: money spent buying new players can't exceed
    # the bank plus what selling the dropped players actually recoups (their FPL
    # *selling* price, not market) — so it never suggests moves you can't afford.
    if have_squad and budget is None:
        bank = int(_meta_int(conn, "bank", 0))
        spend_on_buys = pulp.lpSum(
            players[i].price * squad[i] for i in ids if not players[i].in_squad
        )
        recouped = pulp.lpSum(
            players[i].sell_value * (1 - squad[i]) for i in ids if players[i].in_squad
        )
        prob += spend_on_buys <= bank + recouped
    else:
        # build / wildcard: total squad market price under the cap (live rule
        # from game_settings, else the constant)
        if budget is None:
            budget = _meta_int(conn, "rule_budget", config.BUDGET_TENTHS)
        prob += pulp.lpSum(squad[i] * players[i].price for i in ids) <= budget

    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    status = pulp.LpStatus[prob.status]

    # Degrade gracefully rather than IndexError if the solve is infeasible/timed
    # out (e.g. an over-tight budget): keep the current squad in transfer mode.
    if status != "Optimal":
        return _degraded(conn, players, from_gw, horizon, have_squad, status)

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


def _pick_xi(players: dict[int, Player], squad_ids: list[int]) -> list[int]:
    """Best-effort legal starting XI from a fixed squad: 1 GKP + the highest-value
    outfielders that satisfy the formation minimums, filling to 11 by value."""
    by_pos: dict[str, list[int]] = {p: [] for p in config.POSITIONS}
    for i in squad_ids:
        by_pos[players[i].position].append(i)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda i: players[i].value, reverse=True)
    xi = by_pos["GKP"][:1]
    for pos, lo in config.FORMATION_MIN.items():
        if pos == "GKP":
            continue
        xi += by_pos[pos][:lo]
    remaining = sorted(
        (i for i in squad_ids if i not in xi and players[i].position != "GKP"),
        key=lambda i: players[i].value, reverse=True,
    )
    xi += remaining[: 11 - len(xi)]
    return xi


def _degraded(
    conn: sqlite3.Connection, players: dict[int, Player], from_gw: int,
    horizon: int, have_squad: bool, status: str,
) -> Solution:
    """No optimal solve — return a safe, non-crashing solution. In transfer mode
    we hold the current squad and just set a legal XI/captain; in build mode there
    is nothing to hold, so we return an empty solution flagged with the status."""
    if not have_squad:
        return Solution(
            squad=[], starting=[], captain=0, vice=0, bench=[], formation="-",
            squad_value=0, xi_expected=0.0, status=status, meta={"mode": "build"},
        )
    chosen = [i for i in players if players[i].in_squad]
    starting = _pick_xi(players, chosen)
    captain = max(starting, key=lambda i: players[i].next_gw_points, default=0)
    vice = max(
        (i for i in starting if i != captain), key=lambda i: players[i].value,
        default=captain,
    )
    bench = sorted(
        (i for i in chosen if i not in starting),
        key=lambda i: (players[i].position != "GKP", -players[i].value),
    )
    counts = {pos: sum(1 for i in starting if players[i].position == pos)
              for pos in config.POSITIONS}
    xi_expected = (
        sum(players[i].next_gw_points for i in starting)
        + (players[captain].next_gw_points if captain else 0.0)
    )
    return Solution(
        squad=chosen, starting=starting, captain=captain, vice=vice, bench=bench,
        formation=_formation(counts), squad_value=sum(players[i].price for i in chosen),
        xi_expected=round(xi_expected, 2), transfers_in=[], transfers_out=[],
        hits=0, status=status, meta={"mode": "transfer", "degraded": True},
    )


def _meta_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    try:
        return int(row["value"]) if row and row["value"] not in (None, "") else default
    except (ValueError, TypeError):
        return default
