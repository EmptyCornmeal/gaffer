"""Chip optimisation (T-20).

The audited Chips page was ~10 lines of Svelte arithmetic: Free Hit EV was
"average week minus weakest week", Wildcard had no EV at all, and `active_chip`
was ingested but never read, so it would happily recommend a chip you had
already played.

Chip definitions, windows and counts come from the live API (`bootstrap.chips`),
not from a hard-coded GW19 split — the current season ships eight chips in two
sets and Wildcard/Free Hit are unavailable in GW1.

Each chip is valued against the SAME scenario set as everything else, so a chip's
gain is measured in the same simulated football as the squad it modifies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

CHIPS_VERSION = "chips-1.0"

WILDCARD = "wildcard"
FREEHIT = "freehit"
BENCH_BOOST = "bboost"
TRIPLE_CAPTAIN = "3xc"

#: Minimum expected gain, in points, before using a chip is worth recommending.
#: Below this, holding an option with weeks of optionality left is better.
USE_THRESHOLD = 4.0


@dataclass
class ChipWindow:
    name: str
    number: int
    start_event: int
    stop_event: int
    chip_type: str

    def covers(self, gw: int) -> bool:
        return self.start_event <= gw <= self.stop_event

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "number": self.number,
                "start_event": self.start_event, "stop_event": self.stop_event,
                "chip_type": self.chip_type}


def parse_windows(bootstrap: dict[str, Any]) -> list[ChipWindow]:
    """Discover this season's chips from the API rather than assuming them."""
    out = []
    for c in bootstrap.get("chips") or []:
        try:
            out.append(ChipWindow(
                name=str(c["name"]), number=int(c.get("number", 1)),
                start_event=int(c["start_event"]), stop_event=int(c["stop_event"]),
                chip_type=str(c.get("chip_type", "")),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def available_windows(
    windows: list[ChipWindow], used: list[str], gw: int,
) -> list[ChipWindow]:
    """Windows still usable at ``gw``, after removing spent chips.

    A chip name may appear twice (two sets); each use consumes one instance, so
    playing the first-half Wildcard leaves the second-half one available.
    """
    remaining = list(windows)
    for name in used:
        for i, w in enumerate(remaining):
            if w.name == name:
                del remaining[i]
                break
    return [w for w in remaining if w.covers(gw)]


@dataclass
class ChipEvaluation:
    chip: str
    gameweek: int
    expected_gain: float
    ci95: tuple[float, float]
    baseline: float
    with_chip: float
    assumptions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chip": self.chip, "gameweek": self.gameweek,
            "expected_gain": round(self.expected_gain, 2),
            "ci95": [round(self.ci95[0], 2), round(self.ci95[1], 2)],
            "baseline_points": round(self.baseline, 2),
            "with_chip_points": round(self.with_chip, 2),
            "assumptions": self.assumptions,
        }


def _ci(diff: np.ndarray) -> tuple[float, float]:
    if diff.size == 0:
        return (0.0, 0.0)
    se = float(diff.std(ddof=1)) / max(np.sqrt(diff.size), 1.0)
    m = float(diff.mean())
    return (m - 1.96 * se, m + 1.96 * se)


def evaluate_bench_boost(scen: Any, starting: list[int], bench: list[int],
                         captain: int | None, gw: int) -> ChipEvaluation:
    """All 15 score. The gain is exactly the bench, in the same scenarios."""
    base = scen.squad_points(starting, captain=captain)
    boosted = scen.squad_points(starting, captain=captain, bench=bench,
                                bench_boost=True)
    d = boosted - base
    return ChipEvaluation(
        BENCH_BOOST, gw, float(d.mean()), _ci(d), float(base.mean()),
        float(boosted.mean()),
        ["Bench points are simulated under the same fixtures as the XI.",
         "Autosubs are not modelled, so a benched non-starter contributes his "
         "own simulated points rather than a replacement's."])


def evaluate_triple_captain(scen: Any, starting: list[int], captain: int | None,
                            gw: int) -> ChipEvaluation:
    """x3 instead of x2 on the armband: the gain is one extra captain copy."""
    base = scen.squad_points(starting, captain=captain)
    tripled = scen.squad_points(starting, captain=captain, captain_multiplier=3)
    d = tripled - base
    return ChipEvaluation(
        TRIPLE_CAPTAIN, gw, float(d.mean()), _ci(d), float(base.mean()),
        float(tripled.mean()),
        ["Assumes the captain plays; a blank or an early injury forfeits the chip.",
         "Evaluated on the captain the optimiser would pick, not a fixed choice."])


def evaluate_free_hit(scen: Any, starting: list[int], captain: int | None,
                      best_xi: list[int], best_captain: int | None,
                      gw: int) -> ChipEvaluation:
    """A one-week unconstrained squad that REVERTS afterwards.

    The gain is the optimal one-week XI minus your own, both scored in the same
    scenarios. It is a one-week gain only — the squad is handed back, so nothing
    carries into later weeks.
    """
    base = scen.squad_points(starting, captain=captain)
    free = scen.squad_points(best_xi, captain=best_captain)
    d = free - base
    return ChipEvaluation(
        FREEHIT, gw, float(d.mean()), _ci(d), float(base.mean()), float(free.mean()),
        ["The Free Hit squad reverts after this gameweek: the gain is one week "
         "only and no player is retained.",
         "The comparison squad is a real budget-legal solve, not the strongest "
         "XI in the game."])


def evaluate_wildcard(scen: Any, starting: list[int], captain: int | None,
                      best_xi: list[int], best_captain: int | None, gw: int,
                      weeks_retained: int = 1) -> ChipEvaluation:
    """A permanent squad rebuild.

    Unlike a Free Hit the new squad persists, so the one-week gain is scaled by
    how long it is expected to hold up. That multiplier is an assumption, stated
    rather than buried.
    """
    base = scen.squad_points(starting, captain=captain)
    new = scen.squad_points(best_xi, captain=best_captain)
    d = (new - base) * max(1, weeks_retained)
    lo, hi = _ci(d)
    return ChipEvaluation(
        WILDCARD, gw, float(d.mean()), (lo, hi), float(base.mean()),
        float(new.mean()),
        [f"The rebuilt squad is assumed to hold its edge for {weeks_retained} "
         "gameweek(s); the per-week gain is simulated, the persistence is not.",
         "Gaffer's multi-week projections are materially weaker than its "
         "one-week ones, so a long retention assumption is the dominant "
         "uncertainty here."])


@dataclass
class ChipPlan:
    recommendation: str
    gameweek: int | None
    expected_gain: float
    alternatives: list[dict[str, Any]]
    available: list[dict[str, Any]]
    used: list[str]
    reason: str
    threshold: float = USE_THRESHOLD

    def as_dict(self) -> dict[str, Any]:
        return {
            "chips_version": CHIPS_VERSION,
            "recommendation": self.recommendation,
            "gameweek": self.gameweek,
            "expected_gain": round(self.expected_gain, 2),
            "use_threshold": self.threshold,
            "alternatives": self.alternatives,
            "available": self.available,
            "used": self.used,
            "reason": self.reason,
        }


def plan_chips(
    evaluations: list[ChipEvaluation], windows: list[ChipWindow],
    used: list[str], gw: int, threshold: float = USE_THRESHOLD,
    squad_known: bool = True,
) -> ChipPlan:
    """Compare 'use now' against holding, and say which — with a reason.

    ``squad_known=False`` means the evaluations were computed against a stand-in
    squad (pre-season, before any deadline has passed, FPL exposes no picks). The
    gains are still worth showing, but a chip is spent on a squad you actually
    own, so nothing is recommended.
    """
    avail = available_windows(windows, used, gw)
    usable = {w.name for w in avail}
    live = [e for e in evaluations if e.chip in usable]
    live.sort(key=lambda e: -e.expected_gain)
    alts = [e.as_dict() for e in live]

    if not squad_known:
        return ChipPlan(
            "hold", None, live[0].expected_gain if live else 0.0, alts,
            [w.as_dict() for w in avail], list(used),
            "your own squad is not readable yet, so these gains are measured "
            "against a stand-in squad — a chip is spent on the team you actually "
            "own, so nothing is recommended until your picks are public",
            threshold)

    if not live:
        return ChipPlan("hold", None, 0.0, alts,
                        [w.as_dict() for w in avail], list(used),
                        "no chip is available in this gameweek's windows",
                        threshold)
    best = live[0]
    if best.expected_gain < threshold:
        return ChipPlan(
            "hold", None, best.expected_gain, alts,
            [w.as_dict() for w in avail], list(used),
            f"the best available chip ({best.chip}) is worth only "
            f"{best.expected_gain:.1f} points here, below the {threshold:.0f}-point "
            "bar for spending a one-per-half option",
            threshold)
    return ChipPlan(
        best.chip, best.gameweek, best.expected_gain, alts,
        [w.as_dict() for w in avail], list(used),
        f"{best.chip} projects +{best.expected_gain:.1f} points "
        f"(95% CI {best.ci95[0]:.1f} to {best.ci95[1]:.1f}) in GW{best.gameweek}",
        threshold)


def chips_used_from_history(history: dict[str, Any] | None) -> list[str]:
    """Names of chips already played, from ``entry/{id}/history``."""
    out = []
    for c in (history or {}).get("chips") or []:
        if isinstance(c, dict) and c.get("name"):
            out.append(str(c["name"]))
    return out
