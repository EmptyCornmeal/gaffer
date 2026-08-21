"""The weekly decision (T-21).

The audited home page opened on a solver table. A user landing on it before a
deadline had to read a fifteen-row grid to work out whether they were supposed to
do anything at all. This module produces the answer instead: **one** action —
transfer, roll, or "we cannot tell you" — with the value of that action measured
against the only honest baseline, which is doing nothing.

Two properties matter more than anything else here.

**Like for like.** The move and the hold are scored with the *same* projection
version, the same objective params, the same scenario draws, the same team-state
assumptions, the same horizon and the same chip state. A comparison that changes
any of those is not a comparison, it is two different questions.

**A small edge is not a recommendation.** The threshold below is uncertainty-
aware: a move must clear both an absolute points bar *and* a
probability-of-beating-the-hold bar before it is called an action. Everything in
between is reported as ``too_close``, which is a real answer.

Both bars are **conservative policy choices, not fitted parameters** — see
``THRESHOLD_STATUS``. They are set to keep sub-point noise from being presented
as a transfer recommendation, not because a measurement put them there.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from gaffer import config

DECISION_VERSION = "decision-1.0"

# --- actions ---------------------------------------------------------------
ACTION_TRANSFER = "transfer"
ACTION_ROLL = "roll"
ACTION_TOO_CLOSE = "too_close"
ACTION_UNAVAILABLE = "unavailable"
ALL_ACTIONS = frozenset({ACTION_TRANSFER, ACTION_ROLL, ACTION_TOO_CLOSE,
                         ACTION_UNAVAILABLE})

# --- the minimum actionable edge ------------------------------------------
# These two numbers were originally justified by a one-week rank correlation of
# ~0.76 and ~85 legal-XI points per gameweek. Both figures came from baselines
# that T-26 withdrew (see backtest.WITHDRAWN_BASELINES), so that justification is
# gone and is not replaced by another measurement — there is no admissible one.
#
# The values are UNCHANGED and now carry their real status: a conservative policy
# floor. Lowering them on withdrawn evidence would be as unfounded as the
# original reasoning; raising them would suppress genuine moves. What they do is
# narrow and defensible on its own terms: stop a sub-point projected difference,
# which is well inside any honest model's error, from being published as "make
# this transfer".

#: A move must beat holding by at least this many expected points before it is
#: called an action. POLICY, NOT FITTED.
MIN_ACTIONABLE_POINTS = 1.0

#: ...and it must also win more often than it loses by a clear margin. A move
#: with +2.0 mean but a 48% chance of beating the hold is a coin flip with a
#: nice-looking average. POLICY, NOT FITTED.
MIN_ACTIONABLE_PROBABILITY = 0.55

#: Published with the decision so the screen, the artifact and this module cannot
#: drift apart about whether these bars were measured.
THRESHOLD_STATUS = {
    "fitted": False,
    "basis": "policy",
    "min_actionable_points": MIN_ACTIONABLE_POINTS,
    "min_actionable_probability": MIN_ACTIONABLE_PROBABILITY,
    "rationale": "A conservative floor that stops a sub-point projected edge — "
                 "well inside any honest model's error — from being published as "
                 "a transfer recommendation. It is not derived from a "
                 "measurement.",
    "withdrawn_justification": "The original 1.0 was justified by a ~0.76 rank "
                               "correlation and ~85 legal-XI points per gameweek. "
                               "Both came from baselines withdrawn in T-26 and "
                               "are no longer cited.",
    "reassess_after": "~6 completed gameweeks of immutable decision snapshots "
                      "joined to their reviews — the first sample in which the "
                      "realised gain from acting on an edge of size x can be "
                      "measured against holding.",
}

#: Above this many points, the edge is large enough that the probability gate is
#: waived — a genuinely big projected gain should not be blocked by a wide
#: distribution.
DECISIVE_POINTS = 6.0

# Below this, nothing is waived. The decisive shortcut exists to tolerate a WIDE
# distribution around a large mean, not to green-light a move that loses more
# often than it wins: at 0.50 the two are the same coin, and under it the move is
# simply worse than holding most of the time, however fat its tail.
WAIVER_MIN_PROBABILITY = 0.50


@dataclass
class Comparison:
    """Move versus hold, measured in one shared set of scenarios."""

    move_expected: float
    hold_expected: float
    delta: float
    delta_ci95: tuple[float, float]
    p_move_beats_hold: float
    n_sims: int
    #: Next-gameweek delta only, so a horizon-driven move is visible as such.
    short_term_delta: float
    #: Multi-week delta, or None when horizon values were not supplied. It must
    #: NOT default to 0.0: `max(delta, 0.0)` floors every comparison at zero and
    #: turns a clear loss into "too close to call".
    horizon_delta: float | None
    hit_cost: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "move_expected": round(self.move_expected, 2),
            "hold_expected": round(self.hold_expected, 2),
            "delta": round(self.delta, 2),
            "delta_ci95": [round(self.delta_ci95[0], 2), round(self.delta_ci95[1], 2)],
            "p_move_beats_hold": round(self.p_move_beats_hold, 4),
            "simulations": self.n_sims,
            "short_term_delta": round(self.short_term_delta, 2),
            "horizon_delta": (None if self.horizon_delta is None
                              else round(self.horizon_delta, 2)),
            "hit_cost": self.hit_cost,
        }


@dataclass
class Executability:
    """Can this actually be done with the money and transfers you have?"""

    affordable: bool
    bank_before: int | None          # tenths; None = unknown, NOT zero
    bank_after: int | None
    cost: int                        # tenths, buys
    recouped: int                    # tenths, sells at FPL selling price
    free_transfers_before: int
    free_transfers_after: int
    paid_transfers: int
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "affordable": self.affordable,
            "bank_before": self.bank_before,
            "bank_after": self.bank_after,
            "bank_before_m": None if self.bank_before is None else round(self.bank_before / 10, 1),
            "bank_after_m": None if self.bank_after is None else round(self.bank_after / 10, 1),
            "cost_m": round(self.cost / 10, 1),
            "recouped_m": round(self.recouped / 10, 1),
            "free_transfers_before": self.free_transfers_before,
            "free_transfers_after": self.free_transfers_after,
            "paid_transfers": self.paid_transfers,
            "reason": self.reason,
        }


@dataclass
class Decision:
    action: str
    headline: str
    reason: str
    transfers_out: list[int] = field(default_factory=list)
    transfers_in: list[int] = field(default_factory=list)
    captain: int | None = None
    vice: int | None = None
    starting: list[int] = field(default_factory=list)
    bench: list[int] = field(default_factory=list)
    comparison: Comparison | None = None
    executability: Executability | None = None
    chip: dict[str, Any] | None = None
    league_note: str = ""
    confidence: str = "unknown"
    biggest_risk: str = ""
    assumptions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items()
             if k not in ("comparison", "executability")}
        d["comparison"] = self.comparison.as_dict() if self.comparison else None
        d["executability"] = (
            self.executability.as_dict() if self.executability else None)
        # Travels with every decision so the screen cannot present the bars as
        # measured while this module knows they are not.
        d["threshold_status"] = dict(THRESHOLD_STATUS)
        return d


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _ci(diff: np.ndarray) -> tuple[float, float]:
    if diff.size == 0:
        return (0.0, 0.0)
    se = float(diff.std(ddof=1)) / max(np.sqrt(diff.size), 1.0)
    m = float(diff.mean())
    return (m - 1.96 * se, m + 1.96 * se)


def compare(
    scen: Any, *, move_xi: list[int], move_captain: int | None,
    hold_xi: list[int], hold_captain: int | None, hit_cost: int = 0,
    move_horizon: float | None = None, hold_horizon: float | None = None,
) -> Comparison:
    """Score the move and the hold in the SAME scenarios.

    Both squads are drawn from one ``ScenarioSet``, so a goal that lifts the move
    is the same goal that lifts the hold. Scoring them independently would let
    simulation noise masquerade as an edge.

    ``hit_cost`` is subtracted from the move in every scenario, not from the mean
    afterwards — a -4 is certain, and it must reduce the win probability too.
    """
    horizon_delta = (
        None if move_horizon is None or hold_horizon is None
        else float(move_horizon - hold_horizon - hit_cost))
    n = int(getattr(scen, "n_sims", 0) or 0)
    if n == 0 or not move_xi or not hold_xi:
        return Comparison(0.0, 0.0, 0.0, (0.0, 0.0), 0.0, n, 0.0,
                          horizon_delta, hit_cost)
    move = scen.squad_points(move_xi, captain=move_captain) - float(hit_cost)
    hold = scen.squad_points(hold_xi, captain=hold_captain)
    diff = move - hold
    return Comparison(
        move_expected=float(move.mean()),
        hold_expected=float(hold.mean()),
        delta=float(diff.mean()),
        delta_ci95=_ci(diff),
        p_move_beats_hold=float((diff > 0).mean()),
        n_sims=n,
        short_term_delta=float(diff.mean()),
        horizon_delta=horizon_delta,
        hit_cost=hit_cost,
    )


def classify(
    cmp_: Comparison, *,
    min_points: float = MIN_ACTIONABLE_POINTS,
    min_probability: float = MIN_ACTIONABLE_PROBABILITY,
    decisive: float = DECISIVE_POINTS,
) -> tuple[str, str]:
    """Action and reason, from the comparison alone.

    Uses the *horizon* delta as the decision quantity when a move is worth more
    later than now — a transfer bought for a fixture swing three weeks out is a
    real move — but the probability gate is always the next gameweek, which is
    the only horizon Gaffer projects well, and it applies to every path through
    this function. Nothing clears the bar on mean alone.
    """
    # The best case for the move across the timescales we can actually measure.
    # An absent horizon contributes nothing rather than a zero floor.
    best = cmp_.delta if cmp_.horizon_delta is None else max(
        cmp_.delta, cmp_.horizon_delta)

    # The decisive waiver exists so a genuinely large edge is not blocked by a
    # wide distribution around it. It is deliberate — but as written it tested
    # `best`, i.e. max(this gameweek, horizon), and skipped the probability gate
    # entirely.
    #
    # At GW2 2026-27 that combination published "(-20) — make this transfer" at
    # high confidence for a move worth -12.4 points in the only week Gaffer
    # projects well, ahead in 13% of 2000 scenarios, on the strength of a
    # horizon mean the same artifact calls "materially weaker".
    #
    # So the waiver now requires the edge to be decisive THIS gameweek, and
    # refuses to waive a losing bet. A wide distribution is what it was for; a
    # move that loses more often than it wins is not "wide", it is bad.
    decisive_now = cmp_.delta >= decisive
    if decisive_now and cmp_.p_move_beats_hold >= WAIVER_MIN_PROBABILITY:
        return ACTION_TRANSFER, (
            f"the move projects {cmp_.delta:+.1f} points this gameweek — far "
            f"past the {min_points:.0f}-point bar, and ahead in "
            f"{100 * cmp_.p_move_beats_hold:.0f}% of scenarios")
    if best < min_points:
        if best <= -min_points:
            return ACTION_ROLL, (
                f"every available move loses to holding ({best:+.1f} points): "
                "roll the transfer")
        return ACTION_TOO_CLOSE, (
            f"the best move is worth {best:+.1f} points against holding, inside "
            f"the {min_points:.1f}-point bar. Gaffer cannot tell these apart; "
            "rolling keeps the option")
    if cmp_.p_move_beats_hold < min_probability:
        return ACTION_TOO_CLOSE, (
            f"the move averages {best:+.1f} points but only beats holding in "
            f"{100 * cmp_.p_move_beats_hold:.0f}% of scenarios — a coin flip with "
            "a good-looking mean")
    return ACTION_TRANSFER, (
        f"+{best:.1f} points over holding, ahead in "
        f"{100 * cmp_.p_move_beats_hold:.0f}% of scenarios")


# ---------------------------------------------------------------------------
# Executability
# ---------------------------------------------------------------------------

def executability(
    conn: sqlite3.Connection, transfers_in: list[int], transfers_out: list[int],
    free_transfers: int, bank: int | None,
) -> Executability:
    """Money and transfers, in the FPL units and the FPL rules.

    An unknown bank stays unknown. Treating it as £0.0m is the failure mode that
    produced unaffordable recommendations, and treating it as generous is worse.
    """
    market = {r["id"]: r["price"] for r in conn.execute("SELECT id, price FROM players")}
    sell = {
        r["player_id"]: r["selling_price"]
        for r in conn.execute(
            "SELECT player_id, selling_price FROM my_squad "
            "WHERE gw = (SELECT MAX(gw) FROM my_squad)")
    }
    cost = sum(int(market.get(p, 0) or 0) for p in transfers_in)
    recouped = sum(int(sell.get(p) or market.get(p, 0) or 0) for p in transfers_out)
    made = len(transfers_in)
    paid = max(0, made - free_transfers)
    used = min(made, free_transfers)
    ft_after = min(config.MAX_FREE_TRANSFERS,
                   max(0, free_transfers - used) + 1)

    if bank is None:
        return Executability(
            affordable=False, bank_before=None, bank_after=None, cost=cost,
            recouped=recouped, free_transfers_before=free_transfers,
            free_transfers_after=ft_after, paid_transfers=paid,
            reason="your bank balance is unknown, so this cannot be confirmed "
                   "as affordable — set it in config or the sidebar",
        )
    after = bank + recouped - cost
    return Executability(
        affordable=after >= 0, bank_before=bank, bank_after=after, cost=cost,
        recouped=recouped, free_transfers_before=free_transfers,
        free_transfers_after=ft_after, paid_transfers=paid,
        reason="" if after >= 0 else
        f"short by £{abs(after) / 10:.1f}m at FPL selling prices",
    )


# ---------------------------------------------------------------------------
# Confidence and the biggest risk
# ---------------------------------------------------------------------------

def confidence_band(cmp_: Comparison, coverage: float | None = None) -> str:
    """A word, not a number. The interval is published alongside it."""
    if cmp_.n_sims == 0:
        return "unknown"
    lo, hi = cmp_.delta_ci95
    width = hi - lo
    if width > 4.0 or cmp_.n_sims < 500:
        return "low"
    if abs(cmp_.delta) >= 2 * width:
        return "high"
    return "medium"


def biggest_risk(
    conn: sqlite3.Connection, transfers_in: list[int], captain: int | None,
    horizon_driven: bool,
) -> str:
    """The single most likely way this recommendation is wrong.

    Deliberately one sentence and deliberately specific. A list of caveats is
    read as boilerplate; one named risk is read.
    """
    def row(pid: int | None):
        if pid is None:
            return None
        return conn.execute(
            "SELECT pl.web_name, pl.status, pl.chance_playing, pr.p_start, pl.news "
            "FROM players pl LEFT JOIN projections pr "
            "ON pr.player_id = pl.id AND pr.gw = (SELECT MIN(gw) FROM projections) "
            "WHERE pl.id = ?", (pid,)).fetchone()

    # 1. A flagged player anywhere in the move is the most concrete risk there is.
    for pid in list(transfers_in) + ([captain] if captain else []):
        r = row(pid)
        if r is None:
            continue
        if (r["status"] or "a") != "a":
            note = (r["news"] or "").strip()
            return (f"{r['web_name']} is flagged"
                    + (f" ({note})" if note else "")
                    + " — if he misses, this move loses most of its value")

    # 2. Minutes are the crudest part of the model and gate every projection.
    worst = None
    for pid in list(transfers_in) + ([captain] if captain else []):
        r = row(pid)
        if r is None or r["p_start"] is None:
            continue
        if worst is None or r["p_start"] < worst["p_start"]:
            worst = r
    if worst is not None and (worst["p_start"] or 0) < 0.6:
        return (f"{worst['web_name']}'s start probability is only "
                f"{100 * worst['p_start']:.0f}% — minutes are the weakest part of "
                "the model, and every projection is gated by them")

    # 3. A horizon-driven move rests on the projections Gaffer is worst at.
    if horizon_driven:
        return ("this move is justified by gameweeks 2-6, where Gaffer's mean "
                "projections are materially weaker than its one-week ones")

    if captain is not None:
        r = row(captain)
        if r is not None:
            return (f"captaincy is the biggest single swing of the week: if "
                    f"{r['web_name']} blanks, the gain disappears regardless of "
                    "the transfer")
    return ("the model's one-week ordering is good but not exact; a single "
            "unexpected lineup change can invert this")
