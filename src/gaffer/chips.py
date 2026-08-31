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

**A chip is a WHEN decision, not a WHETHER decision.** You get one per half; the
question is never "is this worth more than nothing", it is "is this the best
gameweek left in the window". This module used to answer only the first: it took
the highest-gain available chip and fired it in the current gameweek the moment
it cleared a flat bar, which is how a Triple Captain came to be recommended in
GW3 with 36 gameweeks still to play. Timing is now assessed explicitly, over the
gameweeks Gaffer actually projects, and a chip whose timing could NOT be assessed
is published as a CANDIDATE with that stated — never as a recommendation.

**The wildcard is netted against the free-transfer path.** The distance from your
squad to the optimum is a property every squad has in every gameweek; it is not
what a wildcard produces. What a wildcard produces is ACCELERATION — arriving at
that squad now rather than one transfer a week. Measuring the raw distance and
then multiplying it by the weeks the squad is held made the wildcard
arithmetically incapable of ranking below the free hit (same inputs, one scaled
by four) and dropped the effective bar for burning it to a quarter of the stated
one.

**Every published chip figure reconciles.** ``expected_gain`` is exactly
``with_chip_points - baseline_points``, at full precision and at the two decimal
places that ship. ``ChipEvaluation`` refuses to be constructed otherwise, because
the previous defect was invisible: the wildcard multiplied the gain and published
the un-multiplied means beside it, so the artifact disagreed with itself by
exactly the multiplier and nothing complained.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

CHIPS_VERSION = "chips-1.1"

WILDCARD = "wildcard"
FREEHIT = "freehit"
BENCH_BOOST = "bboost"
TRIPLE_CAPTAIN = "3xc"

#: Minimum expected gain, in points, before using a chip is worth recommending.
#: Below this, holding an option with weeks of optionality left is better.
USE_THRESHOLD = 4.0

#: Free transfers a manager banks per gameweek. The wildcard is netted against
#: this: over any window your free transfers close some of the same gap on their
#: own, for nothing, and only the part they would NOT have closed is the chip's.
FREE_TRANSFERS_PER_WEEK = 1.0

#: How much better a LATER gameweek must look before Gaffer advises holding for
#: it. Gaffer's multi-week projections are materially weaker than its one-week
#: ones, so a thin future edge is noise, not a plan.
TIMING_MARGIN = 1.0

#: Timing coverage. These describe what was CHECKED, not what was found, and
#: they travel with the artifact so no screen can present an unchecked "now" as
#: a considered one.
TIMING_FULL = "full"            # every remaining gameweek in the window
TIMING_PARTIAL = "partial"      # only the gameweeks Gaffer projects
TIMING_NONE = "none"            # nothing beyond this gameweek was valued
TIMING_MOOT = "last_gameweek"   # the window ends here; there is no later


@dataclass(frozen=True)
class ChipUse:
    """A chip the manager has already played, and when.

    The gameweek matters. With two sets of every chip a bare name cannot say
    *which* instance was spent — see ``available_windows``.
    """
    name: str
    event: int | None = None


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
    windows: list[ChipWindow], used: list[ChipUse | str], gw: int,
) -> list[ChipWindow]:
    """Windows still usable at ``gw``, after removing spent chips.

    A chip name may appear twice (two sets), so a use has to be matched to the
    *right* instance. When the use carries the gameweek it was played in — which
    ``entry/{id}/history`` does — consume the window whose range covers it.
    Matching on the name alone always deleted the earliest window bearing that
    name. Play the second-half Wildcard while the first-half one expired unused
    and it removed the *expired* window, leaving the one you had just spent
    looking available — so it could be recommended a second time. A bare name is
    still accepted, and still falls back to first-match, because that is all a
    caller without event data can offer.
    """
    remaining = list(windows)
    for use in used:
        name = use.name if isinstance(use, ChipUse) else str(use)
        event = use.event if isinstance(use, ChipUse) else None
        idx = None
        if event is not None:
            idx = next((i for i, w in enumerate(remaining)
                        if w.name == name and w.covers(event)), None)
        if idx is None:
            idx = next((i for i, w in enumerate(remaining) if w.name == name), None)
        if idx is not None:
            del remaining[idx]
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
    #: Gameweeks the three point figures cover. One-week chips are 1; the
    #: wildcard's gain is a multi-week total, so its baseline and with-chip
    #: figures are multi-week totals too. Publishing a multi-week gain beside
    #: one-week means is what let the wildcard contradict its own arithmetic.
    horizon: int = 1

    def __post_init__(self) -> None:
        implied = self.with_chip - self.baseline
        if abs(implied - self.expected_gain) > 1e-6:
            raise ValueError(
                f"{self.chip}: expected_gain {self.expected_gain!r} is not "
                f"with_chip - baseline ({self.with_chip!r} - {self.baseline!r} "
                f"= {implied!r}). A chip evaluation whose own arithmetic does "
                "not close cannot be published — anyone checking it on screen "
                "gets a different number from the one we recommend on.")
        if self.horizon < 1:
            raise ValueError(
                f"{self.chip}: horizon must be at least one gameweek, "
                f"got {self.horizon!r}")

    def as_dict(self) -> dict[str, Any]:
        # Round ONCE and derive the third figure, so the arithmetic closes at
        # the precision that ships as well as in full precision. Rounding all
        # three independently let a 0.115 gain print as +0.12 beside a
        # difference of 0.11.
        base = round(self.baseline, 2)
        gain = round(self.expected_gain, 2)
        return {
            "chip": self.chip, "gameweek": self.gameweek,
            "expected_gain": gain,
            "ci95": [round(self.ci95[0], 2), round(self.ci95[1], 2)],
            "baseline_points": base,
            "with_chip_points": round(base + gain, 2),
            "horizon_gameweeks": self.horizon,
            "expected_gain_per_gameweek": round(gain / self.horizon, 2),
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
    base_mean = float(base.mean())
    gain = float((boosted - base).mean())
    return ChipEvaluation(
        BENCH_BOOST, gw, gain, _ci(boosted - base), base_mean, base_mean + gain,
        ["Bench points are simulated under the same fixtures as the XI.",
         "Autosubs are not modelled, so a benched non-starter contributes his "
         "own simulated points rather than a replacement's.",
         "A one-gameweek figure: this is what the chip is worth in GW"
         f"{gw} specifically, not in the best gameweek of its window."])


def evaluate_triple_captain(scen: Any, starting: list[int], captain: int | None,
                            gw: int) -> ChipEvaluation:
    """x3 instead of x2 on the armband: the gain is one extra captain copy."""
    base = scen.squad_points(starting, captain=captain)
    tripled = scen.squad_points(starting, captain=captain, captain_multiplier=3)
    base_mean = float(base.mean())
    gain = float((tripled - base).mean())
    return ChipEvaluation(
        TRIPLE_CAPTAIN, gw, gain, _ci(tripled - base), base_mean, base_mean + gain,
        ["Assumes the captain plays; a blank or an early injury forfeits the chip.",
         "Evaluated on the captain the optimiser would pick, not a fixed choice.",
         "A one-gameweek figure: this is what the chip is worth in GW"
         f"{gw} specifically, not in the best gameweek of its window."])


def evaluate_free_hit(scen: Any, starting: list[int], captain: int | None,
                      best_xi: list[int], best_captain: int | None,
                      gw: int) -> ChipEvaluation:
    """A one-week unconstrained squad that REVERTS afterwards.

    The gain is the optimal one-week XI minus your own, both scored in the same
    scenarios. It is a one-week gain only — the squad is handed back, so nothing
    carries into later weeks, and no transfer is spent reaching it.
    """
    base = scen.squad_points(starting, captain=captain)
    free = scen.squad_points(best_xi, captain=best_captain)
    base_mean = float(base.mean())
    gain = float((free - base).mean())
    return ChipEvaluation(
        FREEHIT, gw, gain, _ci(free - base), base_mean, base_mean + gain,
        ["The Free Hit squad reverts after this gameweek: the gain is one week "
         "only and no player is retained.",
         "The comparison squad is a real budget-legal solve, not the strongest "
         "XI in the game.",
         "Nothing is netted off: a Free Hit costs no transfers, so the whole "
         "one-week distance to the optimal squad is the chip's."])


def free_transfer_catchup(
    weeks: int, changes: int,
    free_transfers_per_week: float = FREE_TRANSFERS_PER_WEEK,
) -> list[float]:
    """Per gameweek, the share of the wildcard's edge free transfers have NOT
    yet closed.

    A wildcard does not create the gap between your squad and the optimum. That
    gap exists in every gameweek, for every manager, and it is closed for free
    at one transfer a week. What the chip buys is arriving NOW, so week ``k``'s
    honest credit is the part of the gap the free-transfer path would still be
    short of by then.

    The gap is assumed to close at an even rate across the ``changes`` players
    that differ. Real transfers take the biggest gaps first, so this OVERSTATES
    the wildcard's advantage — deliberately: a chip that fails the bar under a
    generous assumption has genuinely failed.
    """
    weeks = max(1, int(weeks))
    if changes <= 0:
        # Nothing to change: the optimum IS your squad, and a wildcard buys
        # nothing at all.
        return [0.0] * weeks
    rate = max(0.0, float(free_transfers_per_week)) / changes
    return [max(0.0, 1.0 - rate * k) for k in range(1, weeks + 1)]


def evaluate_wildcard(scen: Any, starting: list[int], captain: int | None,
                      best_xi: list[int], best_captain: int | None, gw: int,
                      weeks_retained: int = 1,
                      free_transfers_per_week: float = FREE_TRANSFERS_PER_WEEK,
                      ) -> ChipEvaluation:
    """A permanent squad rebuild, measured against the transfers you had anyway.

    Two paths over the same window, scored in the same scenarios:

    * **wildcard** — the rebuilt squad from this deadline onward;
    * **free transfers** — the squad you hold now, improving by one transfer a
      week toward the same optimum.

    The gain is the difference between them, which is the only thing the chip is
    responsible for. The previous version measured the raw distance to the
    optimum and multiplied it by the retention window, so the wildcard was
    ``free_hit_gain x weeks_retained`` by construction — it could never rank
    below the Free Hit, and a four-week window quartered the real bar for
    burning it. Baseline and with-chip figures below are window TOTALS, matching
    the gain.
    """
    weeks = max(1, int(weeks_retained))
    base = scen.squad_points(starting, captain=captain)
    new = scen.squad_points(best_xi, captain=best_captain)
    edge = new - base
    changes = len(set(best_xi) - set(starting))
    weights = free_transfer_catchup(weeks, changes, free_transfers_per_week)
    credited = float(sum(weights))

    base_mean = float(base.mean())
    edge_mean = float(edge.mean())
    # What the free-transfer path scores: the held squad, closing the gap.
    baseline = base_mean * weeks + edge_mean * (weeks - credited)
    # What the wildcard path scores: the rebuilt squad, every week.
    with_chip = base_mean * weeks + edge_mean * weeks
    gain = with_chip - baseline

    ft = f"{free_transfers_per_week:g}"
    return ChipEvaluation(
        WILDCARD, gw, gain, _ci(edge * credited), baseline, with_chip,
        [f"Measured over {weeks} gameweek(s): both point figures are "
         f"{weeks}-gameweek totals, not one week.",
         f"Netted against the free-transfer path — {ft} free transfer(s) a week "
         "would close the same gap unaided, so only the ACCELERATION is counted "
         "as the chip's gain.",
         f"{changes} of your XI differ from the budget-legal optimum; the "
         "free-transfer path is assumed to close that gap at an even rate. Real "
         "transfers take the biggest gaps first, so this OVERSTATES the "
         "wildcard's advantage.",
         f"Over the window the free-transfer path is credited with closing "
         f"{weeks - credited:.2f} gameweek(s) worth of the gap by itself.",
         "The rebuilt squad is assumed to hold its edge for the whole window; "
         "the per-week edge is simulated, the persistence is not.",
         "Gaffer's multi-week projections are materially weaker than its "
         "one-week ones, so the persistence assumption is the dominant "
         "uncertainty here."],
        horizon=weeks)


# ---------------------------------------------------------------------------
# Timing: WHEN, not just whether
# ---------------------------------------------------------------------------

def timing_report(
    chip: str, gw: int, window_end: int | None,
    profile: dict[int, float] | None,
) -> dict[str, Any]:
    """What was checked about ``chip``'s timing, and what it showed.

    ``profile`` maps gameweek -> the chip's value in that gameweek, on whatever
    basis the caller states. Only gameweeks inside the chip's remaining window
    count. Coverage is about what was CHECKED: a profile that stops at GW7 while
    the window runs to GW19 is ``partial``, and partial coverage cannot support
    "now is the best week" — only "a week we can see is better than now".
    """
    prof = {int(g): float(v) for g, v in (profile or {}).items()}
    if window_end is not None and window_end <= gw:
        return {
            "chip": chip, "coverage": TIMING_MOOT, "window_end": window_end,
            "assessed_through": gw, "gameweeks": {},
            "now_gain": prof.get(gw), "best_gameweek": gw,
            "best_gain": prof.get(gw),
            "note": f"GW{gw} is the last gameweek of this chip's window, so "
                    "there is no later gameweek to hold it for.",
        }
    inside = {g: v for g, v in prof.items()
              if g >= gw and (window_end is None or g <= window_end)}
    if not inside:
        return {
            "chip": chip, "coverage": TIMING_NONE, "window_end": window_end,
            "assessed_through": None, "gameweeks": {},
            "now_gain": None, "best_gameweek": None, "best_gain": None,
            "note": "No gameweek in this chip's window was valued, so Gaffer "
                    "has not checked whether a later gameweek is worth more.",
        }
    through = max(inside)
    coverage = (TIMING_FULL if window_end is not None and through >= window_end
                else TIMING_PARTIAL)
    best_gw = max(sorted(inside), key=lambda g: inside[g])
    tail = (f"GW{gw}-GW{through}" if through > gw else f"GW{gw} only")
    note = (f"Timing checked over {tail}"
            + (f"; the window runs to GW{window_end}, so GW{through + 1}-GW"
               f"{window_end} were NOT assessed."
               if coverage == TIMING_PARTIAL and window_end is not None
               else "; that is the whole remaining window."))
    return {
        "chip": chip, "coverage": coverage, "window_end": window_end,
        "assessed_through": through,
        "gameweeks": {str(g): round(inside[g], 2) for g in sorted(inside)},
        "now_gain": round(inside[gw], 2) if gw in inside else None,
        "best_gameweek": best_gw, "best_gain": round(inside[best_gw], 2),
        "note": note,
    }


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
    #: False when the played-chip ledger could not be read. Travels with the
    #: artifact so no screen can present "no chips used" as a fact we know.
    state_known: bool = True
    #: The best chip Gaffer can see when it is NOT recommending it because the
    #: WHEN question is unanswered. A candidate is not advice, and the artifact
    #: says which it is holding.
    candidate: dict[str, Any] | None = None
    #: What was and was not checked about timing.
    timing: dict[str, Any] = field(default_factory=dict)

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
            "state_known": self.state_known,
            "candidate": self.candidate,
            "timing": self.timing,
            "reason": self.reason,
        }


def _candidate(e: ChipEvaluation, why: str) -> dict[str, Any]:
    d = e.as_dict()
    d["why_not_recommended"] = why
    return d


def plan_chips(
    evaluations: list[ChipEvaluation], windows: list[ChipWindow],
    used: list[ChipUse | str], gw: int, threshold: float = USE_THRESHOLD,
    squad_known: bool = True, chip_state_known: bool = True,
    timing: dict[str, dict[int, float]] | None = None,
    timing_basis: str = "",
    projected_through: int | None = None,
) -> ChipPlan:
    """Answer WHEN, not just whether — and say which question was answered.

    ``squad_known=False`` means the evaluations were computed against a stand-in
    squad (pre-season, before any deadline has passed, FPL exposes no picks). The
    gains are still worth showing, but a chip is spent on a squad you actually
    own, so nothing is recommended.

    ``chip_state_known=False`` means the played-chip ledger could not be read.
    That is not the same as "no chips have been played", and treating it as such
    is how an already-spent chip gets recommended a second time. Recommend
    nothing and say why.

    ``timing`` maps chip -> {gameweek: value}, on the basis named by
    ``timing_basis``. A chip is recommended only when its timing was actually
    assessed across its whole remaining window and this gameweek won. When the
    window runs past what Gaffer projects, or the chip has no profile at all,
    the best chip is published as a ``candidate`` and the recommendation is
    ``hold`` — with the un-assessed span named. A chip fired on the strength of
    "it beats nothing" is the defect this replaces.
    """
    avail = available_windows(windows, used, gw)
    usable = {w.name for w in avail}
    live = [e for e in evaluations if e.chip in usable]
    live.sort(key=lambda e: -e.expected_gain)
    alts = [e.as_dict() for e in live]
    used_names = [u.name if isinstance(u, ChipUse) else str(u) for u in used]

    window_end = {}
    for w in avail:
        window_end[w.name] = max(window_end.get(w.name, w.stop_event), w.stop_event)
    reports = {
        e.chip: timing_report(e.chip, gw, window_end.get(e.chip),
                              (timing or {}).get(e.chip))
        for e in live
    }
    assessed = sorted(c for c, r in reports.items()
                      if r["coverage"] in (TIMING_FULL, TIMING_MOOT))
    partial = sorted(c for c, r in reports.items()
                     if r["coverage"] == TIMING_PARTIAL)
    unassessed = sorted(c for c, r in reports.items()
                        if r["coverage"] == TIMING_NONE)
    timing_block = {
        "basis": timing_basis,
        "projected_through": projected_through,
        "margin": TIMING_MARGIN,
        "assessed": assessed,
        "partly_assessed": partial,
        "not_assessed": unassessed,
        "by_chip": reports,
    }

    def plan(rec, gameweek, gain, reason, *, candidate=None, state=True):
        return ChipPlan(rec, gameweek, gain, alts, [w.as_dict() for w in avail],
                        used_names, reason, threshold, state_known=state,
                        candidate=candidate, timing=timing_block)

    if not chip_state_known:
        return plan(
            "hold", None, live[0].expected_gain if live else 0.0,
            "your chip history could not be read, so Gaffer cannot tell which "
            "chips you have already played — recommending one now risks "
            "spending it twice", state=False)

    if not squad_known:
        return plan(
            "hold", None, live[0].expected_gain if live else 0.0,
            "your own squad is not readable yet, so these gains are measured "
            "against a stand-in squad — a chip is spent on the team you actually "
            "own, so nothing is recommended until your picks are public")

    if not live:
        return plan("hold", None, 0.0,
                    "no chip is available in this gameweek's windows")

    best = live[0]
    if best.expected_gain < threshold:
        span = ("" if best.horizon <= 1
                else f" over {best.horizon} gameweeks")
        return plan(
            "hold", None, best.expected_gain,
            f"the best available chip ({best.chip}) is worth only "
            f"{best.expected_gain:.1f} points here{span}, below the "
            f"{threshold:.0f}-point bar for spending a one-per-half option")

    rep = reports[best.chip]
    coverage = rep["coverage"]

    # A later gameweek Gaffer CAN see is materially better: that is a finding
    # even from partial coverage, and it is the whole point of the exercise.
    if (coverage in (TIMING_FULL, TIMING_PARTIAL)
            and rep["best_gameweek"] != gw
            and rep["now_gain"] is not None
            and rep["best_gain"] > rep["now_gain"] + TIMING_MARGIN):
        return plan(
            "hold", None, best.expected_gain,
            f"{best.chip} is worth +{best.expected_gain:.1f} here, but GW"
            f"{rep['best_gameweek']} projects {rep['best_gain']:.1f} against GW"
            f"{gw}'s {rep['now_gain']:.1f} on the same basis — a chip is a WHEN "
            f"decision and this is not yet the best gameweek in its window "
            f"({rep['note']})",
            candidate=_candidate(
                best, f"a later gameweek in the window (GW{rep['best_gameweek']}) "
                      "projects more"))

    if coverage == TIMING_NONE:
        end = rep["window_end"]
        span = f"GW{gw + 1}-GW{end}" if end else "the rest of its window"
        return plan(
            "hold", None, best.expected_gain,
            f"{best.chip} projects +{best.expected_gain:.1f} points in GW{gw}, "
            f"but Gaffer has not valued it in {span}, so it cannot say this is "
            "the gameweek to spend it. Published as a candidate, not a "
            "recommendation — a chip you can only play once is a WHEN decision, "
            "and the WHEN has not been assessed",
            candidate=_candidate(
                best, "chip timing was not assessed: no later gameweek in the "
                      "window was valued"))

    if coverage == TIMING_PARTIAL:
        end, through = rep["window_end"], rep["assessed_through"]
        return plan(
            "hold", None, best.expected_gain,
            f"{best.chip} projects +{best.expected_gain:.1f} points and GW{gw} "
            f"is the best of GW{gw}-GW{through} — but its window runs to GW"
            f"{end} and Gaffer projects no further, so GW{through + 1}-GW{end} "
            "were not assessed. Published as a candidate, not a recommendation",
            candidate=_candidate(
                best, f"timing assessed only to GW{through}; the window runs to "
                      f"GW{end}"))

    where = ("its window ends here" if coverage == TIMING_MOOT
             else f"the best of GW{gw}-GW{rep['assessed_through']}")
    return plan(
        best.chip, best.gameweek, best.expected_gain,
        f"{best.chip} projects +{best.expected_gain:.1f} points "
        f"(95% CI {best.ci95[0]:.1f} to {best.ci95[1]:.1f}) in GW{best.gameweek}, "
        f"and GW{gw} is {where}")


def chip_uses_from_history(history: dict[str, Any] | None) -> list[ChipUse]:
    """Chips already played **and the gameweek each was played in**.

    The event is what lets ``available_windows`` consume the right instance when
    a chip exists in two sets.
    """
    out: list[ChipUse] = []
    for c in (history or {}).get("chips") or []:
        if not (isinstance(c, dict) and c.get("name")):
            continue
        try:
            event = int(c["event"]) if c.get("event") is not None else None
        except (TypeError, ValueError):
            event = None
        out.append(ChipUse(str(c["name"]), event))
    return out


def chips_used_from_history(history: dict[str, Any] | None) -> list[str]:
    """Names of chips already played, from ``entry/{id}/history``.

    Kept for callers that only need the names; anything matching a use back to a
    window should use :func:`chip_uses_from_history` instead.
    """
    return [u.name for u in chip_uses_from_history(history)]
