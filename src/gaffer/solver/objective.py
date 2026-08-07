"""One decision objective, shared by both solvers.

The single-gameweek optimiser and the multi-period planner previously carried
similar-but-different formulas. They disagreed on the same input: different
captain, 5/15 squad overlap, 60.93 vs 67.83 expected points. One had a bench
term and the other did not; one decayed the hit cost and the other did not.

Everything either solver weighs is declared here once, so a change cannot be
applied to one and forgotten in the other, and so the two can be reconciled
term by term.

Global ownership is deliberately absent (T-14). It returns only through the
league layer's placing objective, never as a weight on expected points.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from gaffer import config

#: Bumped whenever the shipped decision objective changes shape or weights.
OBJECTIVE_VERSION = "objective-1.0"


def _default_bench_weights() -> dict[str, float]:
    """Position-aware bench value.

    A bench outfielder can be auto-subbed in and score; a backup keeper almost
    never plays. Weighting them equally (or at zero, as the single-GW solver
    did) is what produced a £17.5m bench worth 4.43 xP.
    """
    return {"GKP": 0.04, "DEF": 0.18, "MID": 0.16, "FWD": 0.14}


@dataclass(frozen=True)
class ObjectiveParams:
    """Every weight in the decision objective. One source of truth."""

    # --- time ------------------------------------------------------------
    horizon_decay: float = 0.84
    #: Hits are discounted on the SAME basis as the gains they buy. Leaving the
    #: cost undecayed made a week-4 hit need 8.03 raw points instead of 4.
    decay_hit_cost: bool = True

    # --- squad terms -----------------------------------------------------
    bench_weight: dict[str, float] = field(default_factory=_default_bench_weights)
    vice_weight: float = 0.10
    ceiling_weight: float = 0.30
    gk_spend_penalty: float = 0.10

    # --- transfers -------------------------------------------------------
    hit_cost: int = config.HIT_COST
    #: Per-week reward for holding a free transfer. MUST be 0: rewarding `ft[w]`
    #: in every week double-counts one option. Carrying a single transfer across
    #: four weeks earned 1.5*(1+.84+.71+.59) = 4.7 points — more than the 4-point
    #: hit it costs to keep it — so the planner paid real hits to hoard. A free
    #: transfer is worth what it lets you do later, which is the terminal term.
    ft_value: float = 0.0
    transfer_friction: float = 0.05
    max_free_transfers: int = config.MAX_FREE_TRANSFERS

    # --- terminal treatment ----------------------------------------------
    #: Without these the planner dumps every saved transfer in the final week
    #: (a horizon artefact, not a real plan) or hoards them for no reason.
    #: Value of each free transfer left at the horizon's end. Must stay strictly
    #: below `hit_cost`, or banking becomes worth more than the hit that buys it.
    terminal_ft_value: float = 1.2
    terminal_bank_value: float = 0.004      # per tenth of a million
    terminal_squad_value: float = 0.35      # per point of final-week XI strength

    # --- neutrality ------------------------------------------------------
    #: MUST remain 0.0 until a league-specific placing objective exists (T-14).
    ownership_weight: float = 0.0

    def decay(self, week: int) -> float:
        return self.horizon_decay ** week

    def bench(self, position: str) -> float:
        return self.bench_weight.get(position, 0.15)

    def hit_cost_at(self, week: int) -> float:
        """Cost of one paid transfer in week ``week``, on the gains' time basis."""
        d = self.decay(week) if self.decay_hit_cost else 1.0
        return self.hit_cost * d

    def horizon_factor(self, horizon: int) -> float:
        return sum(self.decay(k) for k in range(horizon))

    def with_(self, **kw: Any) -> ObjectiveParams:
        return replace(self, **kw)

    def as_dict(self) -> dict[str, Any]:
        return {
            "objective_version": OBJECTIVE_VERSION,
            "horizon_decay": self.horizon_decay,
            "decay_hit_cost": self.decay_hit_cost,
            "bench_weight": dict(self.bench_weight),
            "vice_weight": self.vice_weight,
            "ceiling_weight": self.ceiling_weight,
            "gk_spend_penalty": self.gk_spend_penalty,
            "hit_cost": self.hit_cost,
            "ft_value": self.ft_value,
            "transfer_friction": self.transfer_friction,
            "terminal_ft_value": self.terminal_ft_value,
            "terminal_bank_value": self.terminal_bank_value,
            "terminal_squad_value": self.terminal_squad_value,
            "ownership_weight": self.ownership_weight,
        }


#: The shipped parameters. Both solvers take this unless a caller overrides.
DEFAULT = ObjectiveParams()


def assert_no_ft_arbitrage(params: ObjectiveParams) -> None:
    """A banked free transfer must never be worth more than a hit.

    Otherwise the optimal plan pays -4 to preserve an option worth more than -4,
    which is not a real FPL decision — it is a modelling artefact.
    """
    if params.ft_value != 0.0:
        raise ValueError(
            "ft_value must be 0.0: a per-week reward double-counts one option "
            "across every week it is held"
        )
    if params.terminal_ft_value >= params.hit_cost:
        raise ValueError(
            f"terminal_ft_value ({params.terminal_ft_value}) must be below "
            f"hit_cost ({params.hit_cost}), or hoarding beats acting"
        )


def assert_ownership_neutral(params: ObjectiveParams) -> None:
    """Guard the T-14 invariant at the point of use."""
    if params.ownership_weight != 0.0:
        raise ValueError(
            "ownership_weight must be 0.0 in the base points objective; league "
            "preference belongs in the placing objective (gaffer.league), not "
            "as a weight on expected points"
        )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

@dataclass
class TermBreakdown:
    """Objective contributions for one solution, term by term."""

    terms: dict[str, float] = field(default_factory=dict)

    def add(self, name: str, value: float) -> None:
        self.terms[name] = self.terms.get(name, 0.0) + float(value)

    @property
    def total(self) -> float:
        return sum(self.terms.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "terms": {k: round(v, 6) for k, v in sorted(self.terms.items())},
            "total": round(self.total, 6),
        }


def score_week(
    params: ObjectiveParams,
    week: int,
    *,
    xi_points: float,
    captain_points: float,
    vice_points: float,
    bench_points_by_pos: dict[str, float],
    ceiling_bonus: float = 0.0,
    gk_overspend: float = 0.0,
    transfers_made: int = 0,
    paid_transfers: int = 0,
    free_transfers_carried: float = 0.0,
) -> TermBreakdown:
    """Score one gameweek under the shared objective.

    Used by the reconciliation report and by the tests that assert the two
    solvers agree; the MILPs express the same algebra in PuLP variables.
    """
    d = params.decay(week)
    b = TermBreakdown()
    b.add("xi", d * xi_points)
    b.add("captain", d * captain_points)
    b.add("vice", d * params.vice_weight * vice_points)
    for pos, pts in bench_points_by_pos.items():
        b.add("bench", d * params.bench(pos) * pts)
    if ceiling_bonus:
        b.add("ceiling", d * params.ceiling_weight * ceiling_bonus)
    if gk_overspend:
        b.add("gk_penalty", -d * params.gk_spend_penalty * gk_overspend)
    if paid_transfers:
        b.add("hits", -params.hit_cost_at(week) * paid_transfers)
    if transfers_made:
        b.add("friction", -params.transfer_friction * d * transfers_made)
    if free_transfers_carried:
        b.add("ft_value", d * params.ft_value * free_transfers_carried)
    return b


def score_terminal(
    params: ObjectiveParams,
    *,
    final_ft: float,
    final_bank: float,
    final_xi_points: float,
) -> TermBreakdown:
    """Value the state left at the end of the horizon.

    Without this the planner treats the last modelled week as the end of the
    world: it dumps every banked transfer there because the squad has no future
    worth protecting. Terminal value restores the missing tail.
    """
    b = TermBreakdown()
    b.add("terminal_ft", params.terminal_ft_value * final_ft)
    b.add("terminal_bank", params.terminal_bank_value * final_bank)
    b.add("terminal_squad", params.terminal_squad_value * final_xi_points)
    return b
