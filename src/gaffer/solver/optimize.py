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
import time
from dataclasses import dataclass, field
from typing import Any

import pulp

from gaffer import config
from gaffer.solver import objective as OBJ

HORIZON_DECAY = 0.84  # weight of each future GW relative to the previous

# Ownership weighting is NEUTRALISED (T-14).
#
# These weights previously added `w * (selected_by_percent/100) * next_gw_points`
# to the objective. At the shipped 'balanced' setting that term was ~70% of the
# whole objective, so the optimiser was maximising global popularity rather than
# expected points: it sacrificed 2.11 xP on the armband and changed 11 of 15
# squad players relative to the pure-points solve.
#
# Global ownership is also the wrong quantity. What moves your position in a
# mini-league is ownership *within that league*, which is directly observable
# from a handful of API calls — not a percentage across millions of managers.
#
# Rather than invent a proxy, the dial is set to zero until T-17 supplies real
# league-specific placing objectives. It is kept as a named, single-purpose knob
# so the reinstatement has an obvious home, and so the artifact shape (which the
# Planner's risk toggle reads) does not change.
NEUTRAL_RISK_WEIGHT = 0.0
RISK_WEIGHTS = {
    "differential": NEUTRAL_RISK_WEIGHT,
    "balanced": NEUTRAL_RISK_WEIGHT,
    "template": NEUTRAL_RISK_WEIGHT,
}
#: Why the three stances currently coincide, surfaced in the artifact.
RISK_NOTE = (
    "Ownership weighting is neutralised. The three stances are identical until "
    "league-specific placing objectives land (T-17); global selected-by% is not "
    "a substitute for ownership inside your league."
)

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

# --- near-optimal margins (G-O) -------------------------------------------
#
# A margin answers the question the squad table could not: *how much does this
# individual pick actually matter?* It is the objective cost of the best legal
# squad that does NOT contain him — an exact forced-out re-solve, not an
# estimate. On the shipped 2026-27 GW1 build the fifteen run from 0.097
# (Kelleher, Fredricson — free swaps) to 8.288 (B.Fernandes), and five of the
# fifteen sit under 0.5. Presenting all fifteen with equal implied confidence,
# as the product did before this, is dishonest in exactly the way this project
# refuses to be elsewhere.
#
# Cost, measured on the real 587-player GW1 pool at horizon 6: 2.51s for all
# fifteen (~0.17s per re-solve) against 0.18s for the headline solve itself.
# Cheap enough to run every pipeline, so it is on by default.
#
# Three implementations were measured, all agreeing to within 1e-6 on every one
# of the fifteen. Rebuilding the whole problem each time took 2.97s. Building it
# once and adding/removing a single `squad[i] == 0` constraint took 2.42s.
# Building it once and forcing the variable through its OWN BOUNDS took 2.46s —
# the same wall clock as the constraint, but it leaves the constraint matrix
# untouched and avoids PuLP 3.3's deprecated `prob.constraints[...]` dict access,
# which is removed in 4.0. Bounds it is. Reuse beats rebuilding on both counts:
# faster, and it cannot drift from the model that produced the recommendation,
# because it *is* that model.
#
# `budget_s` bounds the whole sweep. A pathological pool must degrade to "not
# computed" rather than stall a run that has a deadline to make.
MARGIN_TIME_BUDGET_S = 30.0

# Margins are published to three decimals, not the two used by the neighbouring
# xP fields. Two decimals collapses the entire free-swap band — 0.097, 0.101 and
# 0.097 all become "0.1" — and that band is the most actionable thing a margin
# has to say.
MARGIN_DP = 3

# A forced-out re-solve can never beat the unconstrained optimum, so a small
# negative delta is CBC's tolerance rather than information. Anything larger
# than this is NOT quietly zeroed: it would mean the baseline solve was not
# actually optimal, which is a defect the artifact should show, not hide.
MARGIN_TOLERANCE = 1e-4

#: DO NOT replace the re-solve with LP reduced costs. This was tried and
#: measured, twice. On the shipped GW1 pool the correlation between `squad[i].dj`
#: from the continuous relaxation and the exact margin is **+0.011** (an earlier
#: test build measured +0.072 — both are noise). B.Fernandes' `dj` is exactly
#: 0.0000 while forcing him out of the squad costs 8.288 points; the largest `dj`
#: in the squad belongs to Thiaw (+0.4033), whose exact margin is 0.621, i.e.
#: thirteenth of fifteen. The reason is structural, not a tuning problem: the
#: `dj` of a binary sitting at its bound in a degenerate MILP relaxation prices
#: a marginal continuous nudge, while the margin prices a *discrete swap* whose
#: replacement must simultaneously satisfy budget, positional quota and the
#: three-per-club limit — which is precisely what the relaxation drops.
REDUCED_COST_IS_NOT_A_MARGIN = (
    "LP reduced costs do not approximate the exact margin (measured correlation "
    "+0.011); each margin here is a full forced-out MILP re-solve."
)


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
    ceiling: float = 0.0    # 90th-pct next-GW outcome (Monte-Carlo)
    sim_mean: float = 0.0   # mean of that SAME distribution — the ceiling's own baseline
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


@dataclass
class Margin:
    """What one squad slot is worth, in objective points over the horizon.

    ``points`` is ``None`` whenever no number is honest. Infeasibility is the
    important case: if forcing a player out leaves no legal squad at all under
    the budget, quota and club limits, that is a *meaningful* answer — he is
    structurally required — and reporting it as 0.0, or as an error, would both
    be lies. It gets its own status instead.
    """

    player_id: int
    points: float | None
    #: optimal    — a measured number.
    #: required   — forcing him OUT is infeasible; the squad cannot exist without him.
    #: impossible — forcing a candidate IN is infeasible; he cannot be fitted.
    #: not_computed — no projection, or the time budget ran out.
    #: anomaly    — a negative delta beyond solver tolerance (see MARGIN_TOLERANCE).
    status: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"points": self.points, "status": self.status}
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class MarginReport:
    """Every margin from one sweep, plus the provenance to audit it."""

    margins: dict[int, Margin] = field(default_factory=dict)
    baseline_objective: float | None = None
    horizon: int | None = None
    elapsed_s: float = 0.0
    status: str = "ok"                 # ok | truncated | unavailable
    note: str = ""
    #: False means the baseline replay picked a DIFFERENT squad from the one
    #: being published — an alternate optimum, or a genuine divergence. Either
    #: way the reader is told rather than left to assume.
    baseline_matches_solution: bool = True

    @classmethod
    def unavailable(cls, note: str, horizon: int | None = None) -> MarginReport:
        return cls(status="unavailable", note=note, horizon=horizon)

    def get(self, player_id: int) -> Margin | None:
        return self.margins.get(player_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "method": "exact-forced-resolve",
            "objective_version": OBJ.OBJECTIVE_VERSION,
            "horizon": self.horizon,
            "baseline_objective": (None if self.baseline_objective is None
                                   else round(self.baseline_objective, 6)),
            "baseline_matches_solution": self.baseline_matches_solution,
            "elapsed_s": round(self.elapsed_s, 2),
            "note": self.note or REDUCED_COST_IS_NOT_A_MARGIN,
            "by_player": {str(pid): m.as_dict()
                          for pid, m in sorted(self.margins.items())},
        }


@dataclass
class _Model:
    """The built MILP plus every handle needed to read or perturb it.

    Extracted so the headline solve and the margin re-solves come out of ONE
    function. A margin measured against a different objective than the one that
    picked the squad — a simplification that drops the ceiling term, say, or the
    goalkeeper spend penalty — would be worse than no margin at all, and the
    only durable way to prevent that is to make a second formulation impossible
    to write by accident.
    """

    prob: pulp.LpProblem
    squad: dict[int, pulp.LpVariable]
    start: dict[int, pulp.LpVariable]
    cap: dict[int, pulp.LpVariable]
    hits: pulp.LpVariable | None
    players: dict[int, Player]
    ids: list[int]
    have_squad: bool


def load_players(conn: sqlite3.Connection, from_gw: int, horizon: int) -> dict[int, Player]:
    """Aggregate each player's decayed horizon value and next-GW points."""
    # owned player_id -> selling price (None until we have purchase data)
    owned = {
        r["player_id"]: r["selling_price"]
        # The holdings baseline comes from the last *readable* event, which is
        # never the event being projected — keying this on `from_gw` is what
        # made transfer mode silently collapse to build mode.
        for r in conn.execute(
            "SELECT player_id, selling_price FROM my_squad "
            "WHERE gw = (SELECT MAX(gw) FROM my_squad)"
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
    # Default the simulated mean to the point estimate, so a player with no
    # distribution contributes zero upside rather than his whole ceiling.
    for p in players.values():
        p.sim_mean = p.next_gw_points
    return players


def _formation(counts: dict[str, int]) -> str:
    return f"{counts['DEF']}-{counts['MID']}-{counts['FWD']}"


def _priced_players(
    conn: sqlite3.Connection, from_gw: int, horizon: int,
    distributions: dict[int, dict[str, float]] | None,
) -> dict[int, Player]:
    """Horizon values plus the Monte-Carlo overlay the ceiling term reads."""
    players = load_players(conn, from_gw, horizon)
    if distributions:
        for pid, p in players.items():
            d = distributions.get(pid)
            if d:
                p.ceiling = d.get("ceiling", 0.0)
                p.sim_mean = d.get("mean", p.next_gw_points)
    return players


def optimise(
    conn: sqlite3.Connection,
    from_gw: int,
    horizon: int | None = None,
    max_transfers: int | None = None,
    free_transfers: int = 1,
    budget: int | None = None,
    template_weight: float = 0.0,
    distributions: dict[int, dict[str, float]] | None = None,
    params: OBJ.ObjectiveParams | None = None,
) -> Solution:
    horizon = horizon or config.PROJECTION_HORIZON
    players = _priced_players(conn, from_gw, horizon, distributions)
    params = params or OBJ.DEFAULT
    OBJ.assert_no_ft_arbitrage(params)
    # No hard cap by default — the -4 hit cost self-limits how many transfers are
    # ever worth making. (Was hardcoded to 2, which blocked profitable big moves.)
    if max_transfers is None:
        max_transfers = config.SQUAD_SIZE

    m = _build_model(
        conn, players, horizon, max_transfers=max_transfers,
        free_transfers=free_transfers, budget=budget,
        template_weight=template_weight, params=params,
    )
    prob, squad, start, cap = m.prob, m.squad, m.start, m.cap
    ids, hits_var, have_squad = m.ids, m.hits, m.have_squad

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
    # The vice is the next-best armband, so it is chosen on the same quantity as
    # the armband: next-gameweek expected points. Ranking it by decayed horizon
    # value instead made `recommendation.json` name a different vice from the
    # weekly decision and the client's own lineup, on identical data.
    vice = max(
        (i for i in starting if i != captain),
        key=lambda i: players[i].next_gw_points, default=captain
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
        status=status,
        meta={
            "mode": "transfer" if have_squad else "build",
            "objective": round(pulp.value(prob.objective), 6),
            # Everything needed to rebuild THIS model exactly. `squad_margins`
            # measures each pick against the objective that chose it, so the
            # parameters travel with the solution rather than being re-guessed at
            # the call site — including the objective params, which are compared
            # rather than trusted.
            "solve": {
                "from_gw": from_gw, "horizon": horizon,
                "max_transfers": max_transfers, "free_transfers": free_transfers,
                "budget": budget, "template_weight": template_weight,
                "objective_params": params.as_dict(),
            },
        },
    )


def _build_model(
    conn: sqlite3.Connection,
    players: dict[int, Player],
    horizon: int,
    *,
    max_transfers: int,
    free_transfers: int,
    budget: int | None,
    template_weight: float,
    params: OBJ.ObjectiveParams,
) -> _Model:
    """Build the shipped MILP. The single source of the decision objective."""
    ids = list(players)
    # How much `value` grows with the horizon (decayed GW count) — scale the
    # next-GW ceiling term by this so its pull stays proportional to value at
    # every horizon (otherwise structure drifts as the window lengthens).
    horizon_factor = sum(HORIZON_DECAY**k for k in range(horizon))
    have_squad = any(p.in_squad for p in players.values())

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
        # There is deliberately NO ownership term on the armband. Captaincy
        # optimises pure expected points, by decision, so that the solver, the
        # weekly decision and every screen name the same captain. Ownership may
        # be shown next to that captain as context; it may not choose him.

    # --- bench value (T-19) ------------------------------------------------
    # This term was absent: bench players carried ZERO objective weight, so the
    # choice among minimum-cost legal fills was an arbitrary CBC tie-break —
    # which produced a £17.5m bench worth 4.43 xP. Position-aware, and using the
    # same weights as the multi-period planner so the two solvers agree.
    for _pos in config.POSITIONS:
        obj += params.bench(_pos) * horizon_factor * pulp.lpSum(
            (squad[i] - start[i]) * players[i].next_gw_points
            for i in ids if players[i].position == _pos
        )
    # --- ceiling / upside term (squad structure, not captaincy) -------------
    # Reward the next-GW upside of the starting XI: without it the solver
    # maximises mean points and (a) drops elite forwards whose mean is modest but
    # ceiling is elite, and (b) stacks low-ceiling DEFCON defenders into a thin
    # lone-striker shape at longer horizons. Scaled by horizon_factor so the
    # structure is the same at 1/3/5 GWs.
    #
    # Upside is measured against the mean of the SAME distribution the ceiling
    # came from. It used to be `ceiling - next_gw_points`, subtracting a
    # (possibly blended) point estimate from an unblended Monte-Carlo percentile,
    # so the term was inflated by exactly the blend gap — largest for precisely
    # the players the blend hit hardest.
    #
    # The captain carries no upside term: the armband is decided on expected
    # points alone, so every surface agrees on it.
    upside = {i: max(0.0, players[i].ceiling - players[i].sim_mean) for i in ids}
    if any(upside.values()):
        obj += CEILING_WEIGHT * horizon_factor * pulp.lpSum(
            start[i] * upside[i] for i in ids
        )

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

    return _Model(prob=prob, squad=squad, start=start, cap=cap, hits=hits_var,
                  players=players, ids=ids, have_squad=have_squad)


def squad_margins(
    conn: sqlite3.Connection,
    sol: Solution,
    *,
    distributions: dict[int, dict[str, float]] | None = None,
    params: OBJ.ObjectiveParams | None = None,
    candidates: list[int] | tuple[int, ...] = (),
    budget_s: float = MARGIN_TIME_BUDGET_S,
) -> MarginReport:
    """Exact near-optimal margin for every player in ``sol.squad``.

    For each squad member: re-solve the SAME MILP with that player forced out,
    and take the objective delta. The number is "points you give up over the
    horizon by overruling the solver on this one pick" — a free swap reads near
    zero, the spine of the team reads several points.

    ``candidates`` names players NOT in the squad; each is forced *in* instead,
    and the delta is reported on the same sign convention (positive = what
    owning him costs you).

    See ``REDUCED_COST_IS_NOT_A_MARGIN``: the LP `.dj` shortcut is measured
    noise and must not be substituted here.
    """
    args = (sol.meta or {}).get("solve")
    if sol.status != "Optimal" or not sol.squad or not args:
        return MarginReport.unavailable(
            f"no optimal solution to measure against (status {sol.status!r})")
    params = params or OBJ.DEFAULT
    # The recorded params are compared, never assumed. A caller that solved with
    # custom weights and then asked for margins under the defaults would get
    # numbers describing a squad nobody picked, and they would look plausible.
    if args.get("objective_params") != params.as_dict():
        return MarginReport.unavailable(
            "objective params differ from the solve being measured; a margin "
            "against a different objective is worse than no margin",
            horizon=args.get("horizon"))

    horizon = int(args["horizon"])
    t0 = time.time()
    players = _priced_players(conn, int(args["from_gw"]), horizon, distributions)
    m = _build_model(
        conn, players, horizon, max_transfers=int(args["max_transfers"]),
        free_transfers=int(args["free_transfers"]), budget=args["budget"],
        template_weight=float(args["template_weight"]), params=params,
    )
    cmd = pulp.PULP_CBC_CMD(msg=False)
    m.prob.solve(cmd)
    if pulp.LpStatus[m.prob.status] != "Optimal":
        return MarginReport.unavailable(
            f"baseline replay was {pulp.LpStatus[m.prob.status]!r}", horizon=horizon)
    base = pulp.value(m.prob.objective)
    replayed = {i for i in m.ids if m.squad[i].value() and m.squad[i].value() > 0.5}
    # The replay must reproduce BOTH the squad and its objective value. The
    # objective half is what catches the mistake a caller will actually make:
    # solving WITH `distributions` and then asking for margins without them. The
    # ceiling term silently vanishes, every margin is measured against an
    # objective that did not pick this squad, and the numbers still look
    # perfectly reasonable. Compared, therefore, rather than assumed.
    shipped_obj = (sol.meta or {}).get("objective")
    matches = replayed == set(sol.squad) and (
        shipped_obj is None or abs(base - float(shipped_obj)) <= 1e-4)

    owned = set(sol.squad)
    wanted = list(sol.squad) + [c for c in candidates if c not in owned]
    margins: dict[int, Margin] = {}
    truncated = False
    for pid in wanted:
        if pid not in m.squad:
            margins[pid] = Margin(pid, None, "not_computed",
                                  "no projection for this player in this window")
            continue
        if time.time() - t0 > budget_s:
            truncated = True
            margins[pid] = Margin(pid, None, "not_computed",
                                  f"time budget of {budget_s}s exhausted")
            continue
        force_in = pid not in owned
        var = m.squad[pid]
        low, up = var.lowBound, var.upBound
        var.lowBound, var.upBound = (1, 1) if force_in else (0, 0)
        m.prob.solve(cmd)
        st = pulp.LpStatus[m.prob.status]
        if st == "Optimal":
            # Verify the bound actually bit. An LP writer that emitted binaries
            # without their bounds would hand back the UNCONSTRAINED optimum
            # every time, making all fifteen margins 0.000 — a number that reads
            # as a perfectly plausible answer and is in fact no answer at all.
            # Checked rather than assumed, because the failure is silent.
            if ((var.value() or 0.0) > 0.5) != force_in:
                margins[pid] = Margin(
                    pid, None, "anomaly",
                    "the solver ignored the forced bound on this variable; the "
                    "margin cannot be measured this way on this backend")
                var.lowBound, var.upBound = low, up
                continue
            delta = base - pulp.value(m.prob.objective)
            if -MARGIN_TOLERANCE <= delta < 0:
                delta = 0.0          # CBC tolerance, not information
            if delta < 0:
                margins[pid] = Margin(
                    pid, round(delta, MARGIN_DP), "anomaly",
                    "constrained solve beat the unconstrained optimum — the "
                    "baseline solve was not optimal")
            else:
                margins[pid] = Margin(pid, round(delta, MARGIN_DP), "optimal")
        elif force_in:
            margins[pid] = Margin(
                pid, None, "impossible",
                "no legal squad contains this player under the budget, "
                "positional quota and club limits")
        else:
            # Infeasible is an ANSWER, not an error: no legal 15 exists without
            # him, so the pick is structurally required rather than merely good.
            margins[pid] = Margin(
                pid, None, "required",
                "no legal squad exists without this player under the budget, "
                "positional quota and club limits")
        var.lowBound, var.upBound = low, up

    return MarginReport(
        margins=margins, baseline_objective=base, horizon=horizon,
        elapsed_s=time.time() - t0,
        status="truncated" if truncated else "ok",
        note="" if not truncated else "time budget exhausted part-way",
        baseline_matches_solution=matches,
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
        (i for i in starting if i != captain),
        key=lambda i: players[i].next_gw_points,
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
