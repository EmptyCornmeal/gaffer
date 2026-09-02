"""Heuristic, component-based expected-points model (Phase 1).

Every projection decomposes into the same visible parts —
appearance + goals + assists + clean sheet + DEFCON + bonus — each gated by an
explicit minutes estimate, and carries a confidence read. Phase 2 swaps the
internals for a trained model behind the same interface.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from gaffer import config, gameweek
from gaffer import season as season_mod
from gaffer.model import features as F
from gaffer.model.features import TeamContext, clamp

# 0.2 = T-13: goals conceded, saves, cards, OG, pens, bonus rate.
# 0.3 = M3: a zero in the prior-season baseline is read as a measurement only
#       when the season could have measured it. Numbers move for every player
#       whose baseline records a credible zero, so the version moves with them.
# 0.4 = M3b: the start rate divides fixtures by FIXTURES. It used to divide a
#       fixture-level `starts` tally by an event count, which agree only while
#       every team plays exactly once per gameweek. In-season only, so no effect
#       before GW1 — but it moves real numbers, so it moves the version.
# 0.5 = G-L/G-M/G-P: `defcon_per_90` is empirical-Bayes shrunk like every other
#       rate instead of being read raw, the NegBin dispersion behind it is
#       fitted rather than guessed, and xA is calibrated to FPL's assist
#       definition per position. Every projected DEFCON and assist number moves,
#       so the version moves with them.
# 0.6 = A18: the current-season minutes gate stopped requiring `cur_min`, so a
#       season-to-date zero is believed on the same terms the prior-season arm
#       already believed a `base_starts` zero. It moves 36.5% of backtested
#       player-gameweeks, all of them downward, by a mean of 1.19 points — the
#       largest single change to the projection since it was written.
MODEL_VERSION = "heuristic-0.6"

# Availability status -> baseline multiplier on the chance of featuring.
_STATUS_MULT = {"a": 1.0, "d": 0.5, "i": 0.0, "s": 0.0, "u": 0.0, "n": 0.0}
# Approx minutes for a nailed starter and for a cameo appearance.
_START_MINUTES = 82.0
_CAMEO_MINUTES = 20.0
#: League-average saves per goal conceded, used only when a keeper has no
#: history of his own. PL keepers face roughly this many shots on target per
#: goal shipped.
_SAVES_PER_GOAL = 2.2
#: Weight on a player's own historical bonus rate vs the returns-driven proxy.
#: Bonus is BPS-driven and BPS is post-match, so the rate (a prior-gameweeks
#: aggregate) is the only pre-deadline signal available.
_BONUS_HISTORY_WEIGHT = 0.5

# --- h=1 blend regime -------------------------------------------------------
# The shipped one-week number blends FPL's own `ep_next` at
# `config.EP_NEXT_BLEND_WEIGHT`. The stated justification is that FPL sees team
# news Gaffer does not. That justification is a claim about the source, so it is
# measured every run rather than assumed.
#
# Measured on the live 2026/27 pre-season payload, one week before the GW1
# deadline: `ep_next` topped out at exactly 4.0 across all 587 players, and
# Haaland (15.5m, 6.8 ppg), B.Fernandes (6.7), Gabriel (6.5) and a 6.0m
# goalkeeper (4.4) all held that same 4.0. Blending 70% of that collapsed the
# recommended XI from the model's own 66.2 expected points to a published 43.6,
# and — because the deflation is uneven — reordered the players the decision
# turns on. A goalkeeper outranked a premium forward.
#
# Measured again on 2026-08-31, after GW1 had completed: `ep_next` was exactly
# equal to FPL's own backward-looking `form` for 596 of 626 players (95.2%), was
# equal to `ep_this` for 614 of them, and took only 30 distinct values across
# the entire game. It is a form average. It carries no fixture adjustment and no
# team news — the one thing the whole deference argument rests on. A backup
# goalkeeper who happened to score 10 in GW1 (p_start 0.30) carried `ep_next`
# 10.0 and was published at 7.27 expected points against his own simulated
# 90th-percentile ceiling of 2.0. Forty players were published above their own
# ceiling.
#
# Both of those are the SAME failure at different times of year, so the guard is
# the same guard and it runs every gameweek. It used to be skipped outright once
# a gameweek had completed, on the assumption that a result to compute from
# makes `ep_next` a forecast. The second measurement is what that assumption
# looks like when it is false: the guards were disabled exactly when the season
# made them checkable.
#
# The gate is measured, recorded in meta.json, and lifts by itself.

#: Below this the whole population tops out too low to be a one-week points
#: forecast. A real premium's one-week expectation is comfortably above it.
EP_NEXT_MIN_POPULATION_MAX = 4.5
#: Below this the external forecast is compressed relative to the model it is
#: being blended into, so it cannot separate the players a decision turns on.
#: Self-calibrating — it asks "does this source spread the way ours does?",
#: not "is this number large?". Measured at 0.28 on the pre-season payload.
EP_NEXT_MIN_SPREAD_RATIO = 0.5
#: Fewer paired players than this and neither statistic means anything.
EP_NEXT_MIN_SAMPLE = 10

#: Above this share of the paired population, `ep_next` is FPL's own
#: backward-looking `form` rather than a forecast of anything. `form` is the
#: player's recent points average: it looks only at matches he has already
#: played, so a number that IS it cannot contain the fixture or the team news
#: that are the entire reason for deferring to an external source.
#:
#: Measured 2026-08-31 over the 355 players actually eligible for the blend:
#: 93.0% exact agreement. The rate expected by chance, drawing independently
#: from the two observed marginals, is 11.2% — the coarse one-decimal grid does
#: collide, but nowhere near this often. 0.60 is roughly five times the chance
#: rate and comfortably below the observed collapse, so it separates the two
#: without needing to be precise.
EP_NEXT_MAX_FORM_MATCH = 0.60

#: `ep_next` and `form` are published to one decimal place, so equality is exact
#: up to float representation.
_FORM_MATCH_TOL = 1e-3

#: The published h=1 number is the blend of the component model with `ep_next`.
REGIME_BLENDED = "blended"
#: The published h=1 number is Gaffer's component model alone.
REGIME_COMPONENT_ONLY = "component_only"


def _quantile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank quantile. Deliberately not interpolated: these are decision
    thresholds, and an exact tie should read as the value that is actually there."""
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(q * len(sorted_values))))
    return sorted_values[idx]


def ep_next_regime(
    pairs: list[tuple[float, float]], *, season_started: bool,
    forms: list[float | None] | None = None,
) -> dict[str, Any]:
    """Decide whether FPL's ``ep_next`` is worth blending into h=1 this run.

    ``pairs`` is ``(ep_next, model_points)`` for every player carrying both.
    ``forms`` is FPL's own ``form`` for those same players in the same order,
    when the caller has it; ``None`` means the collapse test cannot run, and the
    reason says so rather than passing the test by default.

    Three independent degeneracy tests, any one of which disables the blend:

    1. **Collapse onto form** — ``ep_next`` equals FPL's own ``form`` for more
       than ``EP_NEXT_MAX_FORM_MATCH`` of the population. A number that IS the
       backward-looking average carries no fixture and no team news, which is
       the entire justification for deferring to it.
    2. **Absolute** — the population maximum is at or below
       ``EP_NEXT_MIN_POPULATION_MAX``. Nothing that tops out at 4.0 across every
       player in the game is a one-week points forecast.
    3. **Relative** — the source's upper spread is less than
       ``EP_NEXT_MIN_SPREAD_RATIO`` of the model's own, so it cannot discriminate
       where the model can.

    All three run every gameweek. ``season_started`` is recorded because it is
    worth knowing, and is deliberately NOT acted on: it used to short-circuit
    every test above on the assumption that a completed gameweek makes
    ``ep_next`` "real form and fixtures", and the 2026-08-31 measurement is what
    that assumption looks like when it is wrong.
    """
    eps = sorted(e for e, _ in pairs)
    mods = sorted(m for _, m in pairs)
    stats: dict[str, Any] = {
        "sample": len(pairs),
        "ep_max": round(eps[-1], 3) if eps else None,
        "ep_spread": None,
        "model_spread": None,
        "spread_ratio": None,
        "form_sample": 0,
        "form_match": None,
        "season_started": season_started,
    }
    full = config.EP_NEXT_BLEND_WEIGHT

    def out(regime: str, weight: float, reason: str) -> dict[str, Any]:
        return {**stats, "regime": regime, "blend_weight": round(weight, 4),
                "reason": reason}

    if len(pairs) < EP_NEXT_MIN_SAMPLE:
        return out(REGIME_COMPONENT_ONLY, 0.0,
                   f"only {len(pairs)} player(s) carry both an ep_next and a "
                   "model projection, which is too few to judge whether the "
                   "external forecast carries any information")

    if forms is not None:
        if len(forms) != len(pairs):
            raise ValueError(
                f"forms has {len(forms)} entries for {len(pairs)} pairs; they "
                "must be parallel or the match rate is measured against the "
                "wrong players")
        matched = [(e, f) for (e, _), f in zip(pairs, forms, strict=True)
                   if f is not None]
        stats["form_sample"] = len(matched)
        if len(matched) >= EP_NEXT_MIN_SAMPLE:
            same = sum(1 for e, f in matched if abs(e - float(f)) <= _FORM_MATCH_TOL)
            stats["form_match"] = round(same / len(matched), 3)

    ep_spread = _quantile(eps, 0.95) - _quantile(eps, 0.50)
    model_spread = _quantile(mods, 0.95) - _quantile(mods, 0.50)
    ratio = (ep_spread / model_spread) if model_spread > 0 else 0.0
    stats["ep_spread"] = round(ep_spread, 3)
    stats["model_spread"] = round(model_spread, 3)
    stats["spread_ratio"] = round(ratio, 3)

    if stats["form_match"] is not None and stats["form_match"] > EP_NEXT_MAX_FORM_MATCH:
        return out(REGIME_COMPONENT_ONLY, 0.0,
                   f"measured this run: ep_next is identical to FPL's own "
                   f"backward-looking `form` for {stats['form_match']:.0%} of the "
                   f"{stats['form_sample']} players eligible for the blend, so it "
                   "is a recent-points average carrying no fixture adjustment and "
                   "no team news rather than a one-week forecast")

    if stats["ep_max"] is not None and stats["ep_max"] <= EP_NEXT_MIN_POPULATION_MAX:
        return out(REGIME_COMPONENT_ONLY, 0.0,
                   f"measured this run: ep_next tops out at {stats['ep_max']:g} "
                   f"across all {len(pairs)} projected players, which is a clipped "
                   "placeholder rather than a one-week forecast")

    if ratio < EP_NEXT_MIN_SPREAD_RATIO:
        return out(REGIME_COMPONENT_ONLY, 0.0,
                   f"measured this run: ep_next spreads only {ratio:.2f}x as "
                   "widely as Gaffer's own projection over the same players, so "
                   "blending it would compress the ranking rather than inform it")

    if stats["form_match"] is None:
        seen = ("FPL's own `form` was not supplied to this check, so the "
                "collapse-onto-form test did not run")
    else:
        seen = (f"it repeats FPL's own `form` for {stats['form_match']:.0%} of "
                f"the {stats['form_sample']} eligible players, under the "
                f"{EP_NEXT_MAX_FORM_MATCH:.0%} collapse threshold")
    return out(REGIME_BLENDED, full,
               f"measured this run: ep_next tops out at {stats['ep_max']:g}, "
               f"spreads {ratio:.2f}x as widely as Gaffer's own projection over "
               f"{len(pairs)} players, and {seen}")


def rotation_scale(p_start: float | None) -> float:
    """The share of the external weight a player's start probability supports.

    ``ep_next`` contains no start information at all. For most of the population
    it is FPL's ``form`` — an average over matches the player actually played —
    so applied to somebody Gaffer's own model says is a bench option it is not
    merely noisy, it is biased high by roughly ``1 / p_start``. The availability
    scaler does not catch this: a fit backup is ``1.0``.

    So the deference decays with the model's own read of whether the player will
    start. Linear ramp: all of the weight at or above
    ``config.EP_NEXT_ROTATION_FULL_P_START``, none of it at or below
    ``config.EP_NEXT_ROTATION_ZERO_P_START``.

    A missing ``p_start`` means "no rotation information", which must read as no
    attenuation rather than as a silent kill of the blend.
    """
    if p_start is None:
        return 1.0
    lo = config.EP_NEXT_ROTATION_ZERO_P_START
    hi = config.EP_NEXT_ROTATION_FULL_P_START
    if hi <= lo:  # misconfigured; refuse to invent a ramp
        return 1.0
    return clamp((float(p_start) - lo) / (hi - lo), 0.0, 1.0)


def apply_ep_next_blend(
    rows: list[dict], *, from_gw: int, availability: dict[int, float],
    season_started: bool, form: dict[int, float] | None = None,
) -> dict[str, Any]:
    """Blend ``ep_next`` into the h=1 rows, unless the source is degenerate.

    Mutates ``rows`` in place and returns the regime record. Rows keep
    ``exp_points_model`` and ``exp_points_ep_next`` untouched either way, so the
    component breakdown always adds up and the two inputs stay auditable.

    ``form`` maps player id to FPL's own ``form``, and is what lets the regime
    check see whether ``ep_next`` has collapsed onto it.
    """
    eligible = [
        r for r in rows
        if r["gw"] == from_gw
        and r.get("exp_points_ep_next") is not None
        and float(r["exp_points_ep_next"]) > 0
        and float(r["exp_points_model"]) > 0
    ]
    pairs = [(float(r["exp_points_ep_next"]), float(r["exp_points_model"]))
             for r in eligible]
    forms = None
    if form is not None:
        forms = [form.get(r["player_id"]) for r in eligible]
    regime = ep_next_regime(pairs, season_started=season_started, forms=forms)
    base = regime["blend_weight"]
    if base <= 0:
        return regime
    applied: list[float] = []
    zeroed = 0
    for r in rows:
        if r["gw"] != from_gw:
            continue
        ep = r.get("exp_points_ep_next")
        if ep is None or float(ep) <= 0:
            continue
        # Two independent suppressions of the external weight, and they are not
        # the same thing. Availability is our own injury/suspension read, kept
        # because FPL's ep_next does not always reflect fresh news and without it
        # an unavailable player would be resurrected by the blend. Rotation is
        # the failure availability cannot see: a fit backup scores 1.0 there.
        #
        # p_start already has availability multiplied into it, so for the ~97% of
        # blended players on status "a" the product IS the rotation scale alone.
        # It compounds only for doubtful players, where being extra reluctant to
        # defer to a number that may not have seen the news is the right
        # direction to be wrong in.
        w = (base
             * availability.get(r["player_id"], 1.0)
             * rotation_scale(r.get("p_start")))
        applied.append(w)
        if w <= 0:
            zeroed += 1
        r["exp_points"] = round(
            (1.0 - w) * float(r["exp_points_model"]) + w * float(ep), 3)
    if applied:
        mean_w = sum(applied) / len(applied)
        regime["blend_weight_applied_mean"] = round(mean_w, 4)
        regime["blend_weight_zeroed"] = zeroed
        regime["reason"] += (
            f"; the nominal weight of {base:g} was then scaled per player by "
            f"availability and start probability, averaging {mean_w:.2f} across "
            f"the {len(applied)} blended players and falling to zero for {zeroed}")
    return regime


def record_regime(conn: sqlite3.Connection, regime: dict[str, Any]) -> None:
    """Stamp the active projection regime into ``meta`` so it reaches meta.json.

    The regime is the difference between "these are Gaffer's numbers" and "these
    are 70% somebody else's". It travels with the artifact for the same reason
    the model version does.
    """
    from gaffer.store import db

    db.set_meta(conn, "projection_regime", regime.get("regime"))
    db.set_meta(conn, "projection_regime_reason", regime.get("reason") or "")
    db.set_meta(conn, "ep_next_blend_weight", regime.get("blend_weight"))
    for key in ("sample", "ep_max", "spread_ratio", "form_match",
                "form_sample", "blend_weight_applied_mean"):
        val = regime.get(key)
        db.set_meta(conn, f"ep_next_{key}", "" if val is None else val)


def _rate(player: Any, key: str) -> float:
    """A per-90 rate from the player row, tolerating absent columns.

    Historical frames and test fixtures do not always carry every rate; a
    missing rate means "no evidence", which must read as zero contribution
    rather than raising.
    """
    try:
        v = player[key]
    except (KeyError, IndexError, TypeError):
        return 0.0
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


@dataclass
class GwProjection:
    player_id: int
    gw: int
    p_start: float
    exp_minutes: float
    exp_goal_pts: float
    exp_assist_pts: float
    exp_cs_pts: float
    exp_defcon_pts: float
    exp_bonus_pts: float
    exp_appearance: float
    exp_conceded_pts: float
    exp_saves_pts: float
    exp_cards_pts: float
    exp_misc_pts: float
    exp_points: float
    confidence: float
    exp_points_model: float = 0.0            # Gaffer's own component sum
    exp_points_ep_next: float | None = None  # FPL's ep_next, where it exists
    model_version: str = MODEL_VERSION
    generated_at: str = ""


def _availability(status: str | None, chance: int | None) -> float:
    base = _STATUS_MULT.get(status or "a", 1.0)
    if chance is not None:  # explicit % overrides the coarse status bucket
        base = chance / 100.0
    return clamp(base, 0.0, 1.0)


# ---------------------------------------------------------------------------
# 4.2/4.3/4.4 -- EVIDENCE QUALITY
# ---------------------------------------------------------------------------
#
# Gaffer measures which of its own components are unreliable and publishes that
# on the Model page, then spends them at full confidence everywhere else. The
# two halves of the product are epistemically disconnected: one page says the
# clean-sheet term has no measured skill, and the page that prints a number
# built more than half from it says nothing.
#
# This is the wiring between them. Every projection can now say what share of
# itself comes from components whose skill has been measured and found wanting.
#
# NOT "confidence". The share is a statement about EVIDENCE, not about the
# probability that a recommendation is right: "56% of this projection depends
# on poorly validated components" and "this recommendation is 44% likely to be
# correct" are different claims, and conflating them would be a fresh semantic
# overreach inside the very contract meant to stop them. `confidence` is
# reserved for the day there is a calibrated probability that a recommendation
# beats its alternative.
#
# THREE-WAY, not binary. "We measured this and it failed" and "we have too
# little data to know" are different claims, and collapsing them would be a
# confidence violation in the other direction -- treating absence of proof as
# proof of failure. This mirrors the calibration ledger's own `reportable`
# discipline, which already refuses to grade what it cannot.
SUPPORTED = "supported"
WEAK_OR_FAILED = "weak_or_failed"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"

#: What is known about each component of a projection, and where it was
#: measured. A POLICY table, not a fitted one -- the statuses come from
#: measurements recorded in `backtest.py`, and this names them so a projection
#: can carry its own provenance.
COMPONENT_EVIDENCE: dict[str, dict[str, str]] = {
    "appearance": {
        "status": SUPPORTED,
        "evidence": ("h=1 Brier 0.086 against 0.099 for the best naive "
                     "baseline after Phase 2A; beats every baseline at every "
                     "horizon on Brier and AUC"),
        "where": "backtest.minutes_model",
    },
    "goals": {
        "status": SUPPORTED,
        "evidence": ("carried by the points model, which beats the naive "
                     "baseline on h=1 MAE (1.048 against 1.075)"),
        "where": "backtest.per_horizon",
    },
    "assists": {
        "status": SUPPORTED,
        "evidence": "as goals; the same rate machinery and the same test",
        "where": "backtest.per_horizon",
    },
    "clean_sheet": {
        "status": WEAK_OR_FAILED,
        "evidence": ("Brier 0.1899 against a league base rate of 0.1901 -- "
                     "barely distinguishable from quoting the base rate to "
                     "everybody -- and over-confident above 0.35, where a "
                     "claimed 0.49 realises 0.29. A reconciled two-pass "
                     "alternative was built and measured and lost on the "
                     "decision metric"),
        "where": "backtest.CLEAN_SHEET_CONTRADICTION",
    },
    "defcon": {
        "status": INSUFFICIENT_EVIDENCE,
        "evidence": ("2025-26 is the only season in the archive with a "
                     "defensive-contribution column, so this component has "
                     "been measured exactly once. One season is one season"),
        "where": "backtest.limitations",
    },
    "bonus": {
        "status": INSUFFICIENT_EVIDENCE,
        "evidence": ("a proxy for BPS, never scored against realised bonus on "
                     "its own; it is blended with history and the blend has no "
                     "separate measurement"),
        "where": "projection._bonus",
    },
    "saves": {
        "status": INSUFFICIENT_EVIDENCE,
        "evidence": "no separate measurement exists for the saves term",
        "where": "-",
    },
    "other": {
        "status": INSUFFICIENT_EVIDENCE,
        "evidence": "cards, own goals and penalties; small, and never measured apart",
        "where": "-",
    },
}

#: Components whose share is reported as weak evidence. A DECLARED POLICY
#: CHOICE, exactly like `EP_NEXT_BLEND_WEIGHT` and the minimum-actionable
#: thresholds, and labelled as one wherever it is published: nothing fitted it.
WEAK_EVIDENCE_STATUSES = (WEAK_OR_FAILED, INSUFFICIENT_EVIDENCE)


def evidence_quality(breakdown: dict[str, float] | None) -> dict[str, Any]:
    """What share of this projection rests on components measured and wanting.

    Reads the published breakdown, so it describes the number actually shown
    rather than a re-derivation of it. Negative components (the `other` term is
    routinely negative) are taken by magnitude: a term that subtracts a point
    is as much of the answer as one that adds it.
    """
    if not isinstance(breakdown, dict) or not breakdown:
        return {"available": False,
                "reason": "no component breakdown was published"}
    total = sum(abs(float(v or 0.0)) for v in breakdown.values())
    if total <= 0:
        return {"available": False, "reason": "the projection is entirely zero"}
    by_status: dict[str, float] = {}
    unknown: list[str] = []
    for name, value in breakdown.items():
        meta = COMPONENT_EVIDENCE.get(name)
        if meta is None:
            unknown.append(name)
            status = INSUFFICIENT_EVIDENCE
        else:
            status = meta["status"]
        by_status[status] = by_status.get(status, 0.0) + abs(float(value or 0.0))
    weak = sum(by_status.get(k, 0.0) for k in WEAK_EVIDENCE_STATUSES)
    out: dict[str, Any] = {
        "available": True,
        "weak_evidence_share": round(weak / total, 4),
        "share_by_status": {k: round(v / total, 4) for k, v in by_status.items()},
        "largest_weak_component": None,
        "policy": ("which statuses count as weak evidence is a DECLARED POLICY "
                   "CHOICE, not a fitted threshold; the statuses themselves "
                   "come from measurements recorded in backtest.py"),
    }
    weak_parts = {
        n: abs(float(v or 0.0)) for n, v in breakdown.items()
        if COMPONENT_EVIDENCE.get(n, {}).get("status", INSUFFICIENT_EVIDENCE)
        in WEAK_EVIDENCE_STATUSES
    }
    if weak_parts:
        biggest = max(weak_parts, key=lambda k: weak_parts[k])
        out["largest_weak_component"] = {
            "component": biggest,
            "share": round(weak_parts[biggest] / total, 4),
            **COMPONENT_EVIDENCE.get(biggest, {}),
        }
    if unknown:
        out["unrecognised_components"] = sorted(unknown)
    return out


def _start_prior(position: str, price: int) -> float:
    """Fallback start probability for players with no usable PL history.

    Leans on price as a proxy for expected role (pricier => more nailed).
    """
    frac = clamp((price - 40) / 60.0, 0.0, 1.0)  # £4.0m..£10.0m -> 0..1
    ceiling = {"GKP": 0.9, "DEF": 0.85, "MID": 0.8, "FWD": 0.8}[position]
    return 0.25 + frac * (ceiling - 0.25)


#: 2A -- shrinkage half-life, in team fixtures. At three completed fixtures the
#: current season and the prior one weigh equally, which is exactly where the
#: gate this replaces flipped from one to the other in a single step. Chosen to
#: make the change about the SHAPE rather than about a tuned constant, and
#: deliberately not swept: sweeping it on the same data that chose it is how an
#: in-sample gain gets shipped as a finding.
START_SHRINK_K = 3.0

#: Weight on the last-three start share, and on whether he started his last
#: fixture. Also not swept.
START_W_LAST3 = 0.35
START_W_LAST_MATCH = 0.25


def base_start_rate(
    starts_td: float | None, fixtures_played: int, base_starts: float | None,
    *, price: int | None = None, position: str | None = None,
    started_lag: float | None = None, start_rate_r3: float | None = None,
    have_base: bool = True,
) -> tuple[float, str]:
    """How often this player starts, and which evidence said so.

    2A.1-2A.2. This replaces a HARD GATE with shrinkage plus recency.

    The gate was ``fixtures_played >= 3``: below it the current season was
    invisible and every player in the game was graded on ``base_starts / 38``.
    Teams have played two fixtures at GW3, so on 2026-09-01 the model published
    ``p_start 0.90`` and a NAILED badge for a player with 0 starts and 11
    minutes, while six ever-presents were flagged as rotation risks. The
    ordering was anti-correlated with the only evidence the season had
    produced. At GW4 the gate opened and the same ranking inverted on no new
    information beyond a counter reaching three.

    Two variants had been measured for that gate and both were RATES -- the
    shipped ``>= 3`` and a refused ``>= 1`` -- and neither was scored inside the
    window the gate binds in, which is about 8% of a season. The estimator that
    wins there is RECENCY, and it was never a candidate.

    Measured on three seasons before this was written
    (``scripts/run_minutes_ablation.py``, rungs R0-R3, Brier on ``started`` at
    h=1). Every rung improved on the one above it, on both the GW1-3 window and
    the full season, in all three seasons:

        season          GW1-3: shipped -> R3      full: shipped -> R3
        2023-24 train   0.18630 -> 0.12057        0.11846 -> 0.08857
        2024-25 select  0.18173 -> 0.11550        0.12038 -> 0.09485
        2025-26 TEST    0.18184 -> 0.12310        0.11544 -> 0.08902

    R3 also beats every naive baseline on both metrics in all three seasons,
    which the shipped model did not: it lost to "started last time" in GW1-3 by
    0.056 Brier.

    Returns ``(rate, branch)``. The branch names the evidence, because a
    probability whose provenance is unknown is what produced the 0.90.
    """
    prior_rate = (base_starts / 38.0) if (have_base and base_starts is not None) else None
    td_rate = ((starts_td / fixtures_played)
               if (fixtures_played > 0 and starts_td is not None) else None)

    if td_rate is None and prior_rate is None:
        # No season, no prior season. The price prior is the last resort and is
        # measured to be the worst arm in the model; A18 shrank it from a third
        # of all rows to a twenty-fifth and it stays a fallback, not a branch
        # anything is expected to land in.
        return _start_prior(position or "MID", int(price or 40)), "price_prior"

    if td_rate is None:
        rate, branch = prior_rate, "prior_season"
    elif prior_rate is None:
        rate, branch = td_rate, "current_season"
    else:
        # Shrinkage, not a switch. The current season enters from the FIRST
        # fixture and its weight grows with how much of it there is, so there is
        # no gameweek at which the answer jumps.
        w = fixtures_played / (fixtures_played + START_SHRINK_K)
        rate = w * td_rate + (1.0 - w) * prior_rate
        branch = "shrunk_current_and_prior"

    # Recency. Both terms are per-FIXTURE facts the season-to-date rate cannot
    # see: a rate over two games cannot tell which game it came from, and a
    # half-time withdrawal on a yellow card looks identical to a demotion.
    if start_rate_r3 is not None:
        rate = (1.0 - START_W_LAST3) * rate + START_W_LAST3 * start_rate_r3
        branch += "+last3"
    if started_lag is not None:
        rate = (1.0 - START_W_LAST_MATCH) * rate + START_W_LAST_MATCH * started_lag
        branch += "+last_match"
    return clamp(rate, 0.0, 0.98), branch


#: The most minutes a player could accumulate in a season without ever starting:
#: 38 appearances at the model's own cameo length. Above this, a `base_starts` of
#: 0 is a column the source did not have, not a career on the bench — and unlike
#: the season check this holds even when the provenance was never recorded.
_MAX_UNSTARTED_MINUTES = 38 * _CAMEO_MINUTES


def _field(player: Any, key: str, default: Any = None) -> Any:
    """One optional input, whatever the row type. ``sqlite3.Row`` raises
    IndexError for an unknown column and a dict raises KeyError; a column added
    after a database was created must not take the projection down."""
    try:
        return player[key]
    except (KeyError, IndexError):
        return default


def shrunk_defcon90(player: Any) -> float:
    """The DEFCON rate the projection actually believes, per 90.

    Lifted out of ``fixture_rates`` so a player card cannot quote a different
    number from the one the model scores. ``export.artifacts`` published
    ``players.defcon_per_90`` straight off the row, so once the shrinkage landed
    a card could read *"reliable DEFCON points (90.0/90 → +2 most weeks)"*
    directly above a P(hit) of 0.000 — two numbers on one card, describing the
    same player, disagreeing by two orders of magnitude. The badge is the half
    of this defect a reader can actually see, so it must come from here rather
    than from a second copy of the arithmetic that can drift.

    Returns 0.0 where DEFCON does not score, so callers may keep reading a zero
    as "not applicable" exactly as they did before.
    """
    pos = player["position"]
    if config.DEFCON_THRESHOLD.get(pos, 99) >= 99:
        return 0.0
    cur_min = player["minutes"] or 0
    base_min = player["base_minutes"] or 0
    have_base = base_min >= config.BASE_SAMPLE_MINUTES
    base_dc = _rate(player, "base_defcon90")
    dc_recorded = config.season_reports_defcon(
        _field(player, "base_season")) is not False
    tgt_dc = (base_dc if (have_base and dc_recorded and base_dc > 0)
              else F.DEFCON_PRIOR[pos])
    # Whichever season produced the rate is the season that sized it.
    dc_minutes = cur_min if cur_min > 0 else base_min
    return F.shrink(_rate(player, "defcon_per_90"), dc_minutes, tgt_dc,
                    F.DEFCON_SHRINK_K)


def _shrunk_rate(player: Any, key: str) -> float:
    """A per-90 rate shrunk toward its positional prior by minutes played.

    M11. These six rates went through `_rate` raw, so a single event in a cameo
    produced a rate one to three orders of magnitude above the league's --
    D.Essugo shipped `other = -2.25` off one red card in about thirteen minutes.
    The DEFCON fix (G-L) is the same shape and is already proven in production.

    Whichever season produced the rate is the season that sized it, so minutes
    fall back to `base_minutes` when the current season has none -- identical to
    `defcon90` above, and the reason a pre-season projection is not handed a
    full-season rate against zero minutes.
    """
    observed = _rate(player, key)
    priors = F.RATE_PRIORS.get(key)
    if not priors:
        return observed
    prior = priors.get(player["position"], 0.0)
    if prior <= 0:
        # A structural zero for this position -- an outfielder cannot save a
        # penalty. There is no prior to pull toward, and the scoring layer
        # already refuses to pay for it. Leave the value exactly as found.
        return observed
    if observed <= prior:
        # **Deliberately one-sided.** The defect is a rate that is impossibly
        # HIGH because one event landed in a cameo. Pulling low and zero rates
        # *up* to the prior is the statistically purer estimator, but it is a
        # different change: it moves every one of 599 players, adds a card cost
        # to players who have never been booked, and would need the whole model
        # re-measured. G-L scoped this explicitly -- "shrink the other six for
        # robustness, not urgency... do not let this grow into a rewrite of
        # `fixture_rates`" -- and two "obviously right" model changes this week
        # measured worse. Two-sided shrinkage is worth doing with a backtest
        # behind it; it is not worth smuggling in behind a defect fix.
        return observed
    cur_min = player["minutes"] or 0
    base_min = player["base_minutes"] or 0
    minutes = cur_min if cur_min > 0 else base_min
    return F.shrink(observed, minutes, prior, F.rate_shrink_k(prior))


def fixture_rates(
    player: sqlite3.Row, fx: F.Fixture, ctx: TeamContext, avail: float,
    fixtures_played: int = 0,
    recency: dict[str, float] | None = None,
) -> dict[str, float]:
    """The underlying per-fixture rate bundle the projection is built from.

    Exposed so the Monte-Carlo layer (``model.simulate``) samples from the *same*
    rates the deterministic projection sums — the point estimate and the
    distribution can never drift apart.

    A13. Sharing the bundle turned out not to be enough on its own, and this
    docstring was the reason nobody looked: the sampler read six of the eleven
    components in it and the two readings differed by up to 1.25 points on a live
    artifact. So the bundle now also carries `conceded_lam` and `saves_lam` — the
    lambdas behind the two `expected_floor_div` terms, which cannot be recovered
    from the expectations — and `bonus_points` is a shared function rather than a
    formula written out twice. `tests/test_simulate.py` is what actually holds
    the promise this docstring makes.
    """
    pos = player["position"]
    cur_min = player["minutes"] or 0
    base_min = player["base_minutes"] or 0
    base_starts = _field(player, "base_starts") or 0
    # Whether a prior season was RECORDED at all. Everything below turns on this
    # rather than on truthiness, because `base_*` is 0 in two situations that
    # mean opposite things: no sample exists, or a real sample measured zero.
    # Both writers gate on the same figure, so the test is exact.
    have_base = base_min >= config.BASE_SAMPLE_MINUTES
    # ...and whether a zero in that sample can be believed. FPL back-fills old
    # seasons with 0 instead of omitting the field, so a zero is only evidence
    # when the season was capable of reporting it. None means unrecorded, which
    # is treated as "believe it" — the physical check below is what protects the
    # unrecorded case.
    zero_is_evidence = config.season_reports_advanced_stats(
        _field(player, "base_season")) is not False

    # --- minutes gate ---------------------------------------------------
    # start prob: current-season starts/games once enough games; else last-season
    # starts/38; else a price-based prior. (starts/38 mid-season is wrong.)
    #
    # Zero starts off a full sample is the strongest bench evidence there is, and
    # the old truthiness test threw it away — sending exactly those players to a
    # price prior that reads an expensive squad player as a probable starter. One
    # start scores 1/38 and is believed; nought is believed on the same terms,
    # but only when it is credible:
    #   * the season could report `starts` at all, and
    #   * the minutes are physically reachable without ever starting. A season of
    #     substitute appearances cannot exceed 38 cameos; more than that with no
    #     starts is a missing column, whatever the provenance says.
    zero_starts_possible = base_min <= _MAX_UNSTARTED_MINUTES
    # `starts` counts FIXTURES, so the denominator counts fixtures too — the
    # team's own completed fixtures, not the number of gameweeks that have
    # elapsed. See `features.played_fixtures_by_team`.
    #
    # A18. The gate used to read `fixtures_played >= 3 and cur_min and ...`, and
    # that `cur_min` was the same truthiness mistake the prior-season arm above
    # had already had corrected out of it. A player whose team had completed
    # eight fixtures and who had played none of them failed on `cur_min` and
    # fell through to a PRICE prior — which reads an expensive squad player as a
    # probable starter. The lesson had been learned once and applied to one of
    # the two arms.
    #
    # It is dropped. A season-to-date zero is now believed exactly the way a
    # `base_starts` zero is, and the sample requirement carries the whole guard:
    # three completed team fixtures, `starts` readable, and then the arithmetic
    # says what it says. Every row this moves has `starts == 0` and therefore
    # goes to precisely 0.00, which is the correct reading of "his team has
    # played and he has not".
    #
    # Measured on the train and select seasons before the test season was
    # looked at, then confirmed once on test. h=1 Brier on `starts`:
    #
    #     2023-24  (train)   0.1452 -> 0.1185
    #     2024-25  (select)  0.1495 -> 0.1204
    #     2025-26  (test)    0.1509 -> 0.1154
    #
    # and it pays through to the points model rather than only to its own
    # metric: h=1 MAE on the test season 1.539 -> 1.114, rank correlation 0.455
    # -> 0.626, legal-XI points 49.7 -> 50.1 per gameweek. The `price_prior`
    # arm — a third of every backtested row, told it had a ~29% chance of
    # starting while starting 2.3% of the time — falls to 4.1% of rows.
    # `backtest.MINUTES_CANDIDATE_FIX` carries the full record, including the
    # larger variant that was measured alongside this one and REFUSED.
    #
    # The residual is real and is not hidden: 0.5%-1.2% of the moved rows did
    # start, and the model now calls them at 0.00 rather than at 0.29. It was
    # wrong about 99.5% of that population before and is wrong about 0.6% of it
    # now, which is the trade being made.
    # 2A.1/2A.2 -- shrinkage plus recency, replacing the `fixtures_played >= 3`
    # gate. See `base_start_rate` for the mechanism, the three-season
    # measurement and why the two variants measured before it both missed the
    # regime the gate actually binds in.
    # A prior-season zero is only evidence when the season could report it AND
    # the minutes are physically reachable without ever starting: a season of
    # substitute appearances cannot exceed 38 cameos, and more than that with no
    # starts is a missing column whatever the provenance says. Preserved from
    # the gate this replaces -- dropping it would send exactly those players to
    # the price prior, which reads an expensive squad player as a probable
    # starter.
    prior_usable = have_base and bool(
        base_starts or (zero_is_evidence and zero_starts_possible))
    base_start, start_branch = base_start_rate(
        starts_td=_field(player, "starts", None),
        fixtures_played=fixtures_played,
        base_starts=base_starts if prior_usable else None,
        price=_field(player, "price", None),
        position=pos,
        started_lag=(recency or {}).get("started_lag"),
        start_rate_r3=(recency or {}).get("start_rate_r3"),
        have_base=prior_usable,
    )
    p_start = clamp(base_start * avail, 0.0, 0.98)
    # M9 — the cameo term was a flat 0.35 for every player in the game, which
    # handed a backup keeper the same chance of appearing as a rotating forward
    # and put a floor of 0.5125 under everybody. It is now measured, and it
    # depends on both how often the player starts and what he plays.
    cameo = F.cameo_probability(p_start, pos)
    p_play = clamp(p_start + (1 - p_start) * cameo * avail, 0.0, 0.99)
    exp_minutes = p_start * _START_MINUTES + (p_play - p_start) * _CAMEO_MINUTES
    # M10 — starting and lasting an hour are different events, and the gap is
    # positional. `p60` gates clean sheets and the long appearance point, so
    # asserting every starter reaches 60' concentrates the error on keepers and
    # defenders. A substitute reaching 60' is rare rather than impossible, so
    # that arm is carried too.
    p60 = (p_start * F.P60_GIVEN_START.get(pos, 1.0)
           + max(p_play - p_start, 0.0) * F.P60_GIVEN_SUB.get(pos, 0.0))
    mins_frac = exp_minutes / 90.0

    # --- attacking ------------------------------------------------------
    # Shrink current-season rate toward the LAST-SEASON rate (survives the FPL
    # stats reset), falling back to a flat position prior for players with none.
    prior = F.XGI_PRIOR[pos]
    # A measured zero outranks a prior. A holding midfielder with 2,000 minutes
    # and no goals has told us what his xG rate is; substituting a positional
    # average there is not caution, it is discarding the only evidence available.
    # But a season that predated expected-goals reports 0.00 for everyone, and
    # believing THAT would project Bruno Fernandes as a man who never threatens.
    base_xg = player["base_xg90"] or 0.0
    base_xa = player["base_xa90"] or 0.0
    use_base_xgi = have_base and (zero_is_evidence or base_xg or base_xa)
    tgt_xg = base_xg if use_base_xgi else prior * 0.55
    tgt_xa = base_xa if use_base_xgi else prior * 0.45
    xg90 = F.shrink(player["xg_per_90"] or 0, cur_min, tgt_xg)
    xa90 = F.shrink(player["xa_per_90"] or 0, cur_min, tgt_xa)
    att_mult = ctx.attack_multiplier(fx.opponent_id, fx.at_home)
    exp_goals = xg90 * mins_frac * att_mult
    exp_assists = xa90 * mins_frac * att_mult

    # --- clean sheet ----------------------------------------------------
    # A19 — MEASURED, NOT FIXED. This module carries TWO estimates of one
    # quantity, "how many goals does the opposition score in this fixture":
    #
    #   top-down    `ctx.expected_conceded(...)`, from team strength and xGC.
    #               It is the only one `p_cs` reads.
    #   bottom-up   the sum of the opposing side's players' `exp_goals`, each
    #               of which is `xg90 * mins_frac * ctx.attack_multiplier(...)`.
    #               It is what every attacking projection is built from.
    #
    # They disagree, by a mean 0.36 goals per fixture-side and up to 4.6 over
    # three seasons of archive, and the disagreement is WORST in exactly the
    # regime the live product occupies in early September: at GW1-3 the mean gap
    # is 0.670 and the two lambdas correlate at 0.26, against 0.314 and 0.76
    # from GW9 on. No sampler can be exact against both, which is how
    # `model.scenarios` found this.
    #
    # Measured against outcomes, the bottom-up lambda is the better clean-sheet
    # forecaster in all three split seasons — Brier 0.1603/0.1714/0.1853 against
    # this one's 0.1643/0.1794/0.1899, AUC 0.662/0.659/0.614 against
    # 0.650/0.635/0.612 — and it is better calibrated at every level above 0.35.
    # What ships here is barely distinguishable from quoting the league clean-
    # sheet rate to everybody: 0.1899 against a base rate of 0.1901 on the test
    # season, and WORSE than the base rate on the train season.
    #
    # And it is over-confident where it matters. Claimed 0.49 realises 0.29;
    # claimed 0.61 realises 0.44; the ten fixture-sides that ever cleared 0.70
    # realised 0.60. The live artifact currently prints 0.760 for one club at
    # home after one finished gameweek, which is above anything three seasons of
    # archive support.
    #
    # It is NOT changed here, and the reason is structural rather than nerve:
    # `fixture_rates` is per-player and cannot see the opposing team's squad, so
    # deriving `p_cs` from the bottom-up lambda needs a two-pass projection —
    # accumulate every team's attacking lambda, then project. That moves every
    # defender and every goalkeeper in the product and needs the points backtest
    # behind it, the way A18 did. `backtest.CLEAN_SHEET_CONTRADICTION` carries
    # the measurement so the next person starts from it.
    p_cs = 0.0
    if config.CS_POINTS[pos] > 0:
        lam = ctx.expected_conceded(player["team_id"], fx.opponent_id, fx.at_home)
        p_cs = F.poisson_p0(lam)

    # --- DEFCON ---------------------------------------------------------
    # G-L. This read the rate raw while both attacking rates above it were
    # shrunk, and a per-90 rate is a division: two players in the shipped 2026/27
    # pre-season artifact carried exactly 90.0 defensive contributions per 90 —
    # one contribution in one minute of football — and the model answered
    # P(hit) = 0.945 and 0.952 and printed "elite defensive volume" on a card
    # that said CAMEO? ~29' three lines further up.
    #
    # The obvious fix ships a worse bug. `defcon_per_90` does not mean what
    # `xg_per_90` means. FPL resets `minutes` at the season rollover but KEEPS
    # its per-90 fields, and `ingest.ingest_players` additionally falls back to
    # the enriched last-season figure when the bootstrap ships a zero — so out of
    # season this column holds a rate derived from ~3,000 prior-season minutes
    # while `cur_min` is 0. Shrinking that against `cur_min` would throw away the
    # best DEFCON evidence in the system and answer with a positional average.
    #
    # So two things are made explicit rather than one:
    #
    #   the TARGET is `base_defcon90`, the prior-season rate, mirroring
    #   `base_xg90` exactly (schema + `ingest.enrich_history`; `histdata` has
    #   computed the column all along for the backtest path);
    #
    #   the SAMPLE SIZE is the minutes that actually generated the rate, which
    #   is last season's whenever the current season has none yet. Without this
    #   second half, an existing database — where `base_defcon90` has not been
    #   backfilled yet but `defcon_per_90` is already correct — would send every
    #   elite ball-winner to a positional average on the first run after the
    #   migration. With it they lose about 3% instead: Anderson 13.91 -> 13.47,
    #   Gabriel 9.06 -> 8.93. Once the backfill runs they are exactly unmoved.
    #
    # A zero in `base_defcon90` is never read as a measurement. 392 outfielders
    # cleared `BASE_SAMPLE_MINUTES` in 2025-26 and not one recorded zero
    # defensive contributions; the floor is 2.25 per 90. Defensive contributions
    # are a high-frequency count, so a zero over a real sample is a column that
    # was not read — the same distinction `base_xg90` draws for seasons that
    # predated expected goals, and `season_reports_defcon` draws it against
    # DEFCON's own later cutoff.
    #
    # Verified on the live artifact: Mheuka 90.0 -> 4.7 (P(hit) 0.945 -> 0.000)
    # and Fredricson 90.0 -> 7.7 (0.952 -> 0.000), while Anderson (13.91),
    # Senesi (11.47), Tarkowski (10.16), Rice (10.94) and Gabriel (9.06) keep
    # their rate to the decimal and move only by the dispersion refit.
    thr = config.DEFCON_THRESHOLD[pos]
    defcon_mu = 0.0
    p_hit = 0.0
    if thr < 99:
        defcon_mu = shrunk_defcon90(player) * mins_frac
        p_hit = F.nbinom_sf(thr, defcon_mu, F.DEFCON_NB_DISPERSION)

    # --- goals conceded / saves (T-13) ----------------------------------
    # Both derive from the SAME expected-goals-conceded figure that drives the
    # clean sheet, so the two cannot disagree about how leaky the fixture is.
    lam_conceded = ctx.expected_conceded(player["team_id"], fx.opponent_id, fx.at_home)
    conceded_lam = 0.0
    conceded_units = 0.0
    if pos in config.CONCEDED_POSITIONS:
        # Only goals shipped while on the pitch count; scale the rate by the
        # share of the match played, not the whole 90.
        conceded_lam = lam_conceded * mins_frac
        conceded_units = F.expected_floor_div(
            conceded_lam, config.CONCEDED_PER_PENALTY)

    saves_lam = 0.0
    save_units = 0.0
    if pos == "GKP":
        rate = _rate(player, "saves_per_90")
        if rate <= 0:
            # No history: fall back to the league relationship between goals
            # conceded and shots faced rather than assuming a keeper never saves.
            rate = lam_conceded * _SAVES_PER_GOAL
        saves_lam = rate * mins_frac
        save_units = F.expected_floor_div(saves_lam, config.SAVES_PER_POINT)

    return {
        "pos": pos,
        "p_start": p_start,
        # 2A -- the estimator names its own branch, so nothing downstream has to
        # transcribe the conditions to work out which evidence answered. The
        # backtest used to keep a second copy of the gate for exactly that, and
        # its own docstring called the duplication a liability.
        "start_branch": start_branch,
        "p_play": p_play,
        "p60": p60,
        "exp_minutes": exp_minutes,
        "mins_frac": mins_frac,
        "exp_goals": exp_goals,
        "exp_assists": exp_assists,
        "goal_pts_per": float(config.GOAL_POINTS[pos]),
        "assist_pts_per": float(config.ASSIST_POINTS),
        "p_cs": p_cs,
        "cs_pts_per": float(config.CS_POINTS[pos]),
        "defcon_mu": defcon_mu,
        "defcon_thr": float(thr),
        "defcon_p_hit": p_hit,
        "defcon_pts": float(config.DEFCON_POINTS),
        "lam_conceded": lam_conceded,
        "conceded_units": conceded_units,
        "save_units": save_units,
        # A13. The Poisson means the two `expected_floor_div` calls integrate.
        # `conceded_units` and `save_units` are E[floor(X/d)], and a floor is not
        # linear, so a sampler cannot recover the lambda from the expectation.
        # Publishing it is what lets `model.simulate` draw the SAME X the point
        # estimate integrates over instead of guessing at one.
        "conceded_lam": conceded_lam,
        "saves_lam": saves_lam,
        # M11 — shrunk, not raw. See `_shrunk_rate`.
        "yellow_rate": _shrunk_rate(player, "yellow_per_90"),
        "red_rate": _shrunk_rate(player, "red_per_90"),
        "og_rate": _shrunk_rate(player, "og_per_90"),
        "pen_save_rate": _shrunk_rate(player, "pen_save_per_90"),
        "pen_miss_rate": _shrunk_rate(player, "pen_miss_per_90"),
        "bonus_rate": _shrunk_rate(player, "bonus_per_90"),
    }


def bonus_points(
    pos: str, goals: Any, assists: Any, defcon_pts: Any, cs_pts: Any,
    cs_pts_per: float, hist: Any, use_history: bool,
) -> Any:
    """The bonus proxy — ONE formula, read by the point estimate AND the sampler.

    BPS is post-match, so it cannot be a feature. What is available before a
    deadline is the player's own historical bonus rate (a prior-gameweeks
    aggregate) and a proxy driven by the returns being projected.

    A13. This used to live inline in `_project_one_fixture` while `model.simulate`
    carried a SECOND, different proxy — `round(0.9*goals + 0.6*assists + 0.4*cs +
    0.3*defcon)`, capped at 3 — so the distribution was centred on a different
    bonus number from the one published beside it, by up to 0.41 points a player.
    Two guesses at the same unmeasurable quantity is one guess too many.

    `goals`, `assists`, `defcon_pts` and `cs_pts` are EXPECTATIONS when the point
    estimate calls this and REALISED DRAWS when the sampler does. The arithmetic
    is linear in all four, which is precisely why one function serves both: the
    mean of the sampled bonus is the published bonus by construction, not by
    calibration.
    """
    proxy = 0.55 * (goals + assists) + 0.25 * defcon_pts
    if pos in ("GKP", "DEF"):
        # `cs_pts / cs_pts_per` recovers the clean-sheet EVENT from its points.
        proxy = proxy + 0.35 * cs_pts / max(cs_pts_per, 1.0)
    if not use_history:
        return proxy
    return (1 - _BONUS_HISTORY_WEIGHT) * proxy + _BONUS_HISTORY_WEIGHT * hist


def _project_one_fixture(
    player: sqlite3.Row, fx: F.Fixture, ctx: TeamContext, avail: float,
    fixtures_played: int = 0,
    recency: dict[str, float] | None = None,
) -> dict[str, float]:
    r = fixture_rates(player, fx, ctx, avail, fixtures_played, recency)
    pos = r["pos"]

    exp_goal_pts = r["exp_goals"] * r["goal_pts_per"]
    exp_assist_pts = r["exp_assists"] * r["assist_pts_per"]
    exp_cs_pts = r["p_cs"] * r["cs_pts_per"] * r["p60"]
    exp_defcon_pts = r["defcon_p_hit"] * r["defcon_pts"]

    # --- appearance -----------------------------------------------------
    exp_appearance = (
        r["p60"] * config.APPEARANCE_LONG
        + (r["p_play"] - r["p60"]) * config.APPEARANCE_SHORT
    )

    # --- goals conceded (T-13) ------------------------------------------
    # Negative counterpart to the clean sheet, from the same lambda: a defender
    # at a leaky club is no longer rewarded for the fixture and spared its cost.
    exp_conceded_pts = r["conceded_units"] * config.CONCEDED_PENALTY

    # --- goalkeeper saves (T-13) ----------------------------------------
    exp_saves_pts = r["save_units"] * config.SAVE_POINTS

    # --- discipline and rare events (T-13) ------------------------------
    # Scaled by time on the pitch. Rates are per-90 season aggregates, so these
    # are expectations, not predictions of a specific booking.
    mf = r["mins_frac"]
    exp_cards_pts = (
        r["yellow_rate"] * mf * config.YELLOW_POINTS
        + r["red_rate"] * mf * config.RED_POINTS
    )
    exp_misc_pts = (
        r["og_rate"] * mf * config.OWN_GOAL_POINTS
        + r["pen_miss_rate"] * mf * config.PENALTY_MISS_POINTS
        + (r["pen_save_rate"] * mf * config.PENALTY_SAVE_POINTS if pos == "GKP" else 0.0)
    )

    # --- bonus ------------------------------------------------------------
    # Shared with `model.simulate` so the distribution centres on this number.
    hist = r["bonus_rate"] * mf
    exp_bonus_pts = bonus_points(
        pos, r["exp_goals"], r["exp_assists"], exp_defcon_pts,
        exp_cs_pts, r["cs_pts_per"], hist, hist > 0,
    )

    exp_points = (
        exp_appearance
        + exp_goal_pts
        + exp_assist_pts
        + exp_cs_pts
        + exp_defcon_pts
        + exp_bonus_pts
        + exp_conceded_pts
        + exp_saves_pts
        + exp_cards_pts
        + exp_misc_pts
    )
    return {
        "p_start": r["p_start"],
        "exp_minutes": r["exp_minutes"],
        "exp_goal_pts": exp_goal_pts,
        "exp_assist_pts": exp_assist_pts,
        "exp_cs_pts": exp_cs_pts,
        "exp_defcon_pts": exp_defcon_pts,
        "exp_bonus_pts": exp_bonus_pts,
        "exp_appearance": exp_appearance,
        "exp_conceded_pts": exp_conceded_pts,
        "exp_saves_pts": exp_saves_pts,
        "exp_cards_pts": exp_cards_pts,
        "exp_misc_pts": exp_misc_pts,
        "exp_points": exp_points,
    }


def _confidence(player: sqlite3.Row, avail: float) -> float:
    """0-1: how much to trust this projection. Driven by minutes reliability,
    availability certainty, and news flags."""
    rel = max(player["minutes"] or 0, player["base_minutes"] or 0)
    minutes_rel = rel / (rel + F.XGI_SHRINK_K)
    conf = 0.55 * minutes_rel + 0.35 * avail + 0.10
    if player["news"]:
        conf *= 0.85
    return round(clamp(conf, 0.05, 0.98), 3)


def project(conn: sqlite3.Connection, from_gw: int, horizon: int | None = None) -> int:
    """Compute and store projections for all players across the horizon.

    A blank gameweek yields a zero row; a double stacks both fixtures.
    Returns the number of (player, gw) rows written.
    """
    horizon = horizon or config.PROJECTION_HORIZON
    ctx = TeamContext.build(conn)
    fixtures = F.upcoming_fixtures_by_team(conn, from_gw, horizon)
    players = conn.execute("SELECT * FROM players").fetchall()
    # Microsecond precision: snapshots are keyed by `as_of`, and two runs in
    # the same second would otherwise collide and overwrite each other.
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    # Two different counts, deliberately kept apart. `games_played` answers "has
    # the season started", which is an event-level question. `played_by_team`
    # answers "how many matches has THIS team completed", which is the only
    # correct denominator for a fixture-level `starts` tally.
    lf = conn.execute("SELECT value FROM meta WHERE key='last_finished_gw'").fetchone()
    games_played = int(lf["value"]) if lf and str(lf["value"]).isdigit() else 0
    played_by_team = F.played_fixtures_by_team(conn)
    # 2A.2 -- per-fixture recency of starting, read once for the whole run.
    # A player with no completed fixtures is ABSENT from this map, and
    # `base_start_rate` must see None rather than a zero: a new signing has
    # not been dropped.
    recency_by_player = F.start_recency_by_player(conn)

    rows: list[dict] = []
    avail_by_player: dict[int, float] = {}
    form_by_player: dict[int, float] = {}
    for p in players:
        avail = _availability(p["status"], p["chance_playing"])
        avail_by_player[p["id"]] = avail
        # FPL's own `form`, kept beside the projection so the regime check can
        # measure whether `ep_next` has simply collapsed onto it.
        raw_form = _field(p, "form", None)
        if raw_form is not None:
            try:
                form_by_player[p["id"]] = float(raw_form)
            except (TypeError, ValueError):
                pass
        conf = _confidence(p, avail)
        team_fx = fixtures.get(p["team_id"], {})
        # group this team's fixtures by gw (handles doubles/blanks)
        by_gw: dict[int, list[F.Fixture]] = {}
        for fx in team_fx:
            by_gw.setdefault(fx.gw, []).append(fx)
        additive = [
            "exp_goal_pts", "exp_assist_pts", "exp_cs_pts", "exp_defcon_pts",
            "exp_bonus_pts", "exp_appearance", "exp_points", "exp_minutes",
            "exp_conceded_pts", "exp_saves_pts", "exp_cards_pts", "exp_misc_pts",
        ]
        for gw in range(from_gw, from_gw + horizon):
            parts = [
                _project_one_fixture(p, fx, ctx, avail,
                                     played_by_team.get(p["team_id"], 0),
                                     recency_by_player.get(p["id"]))
                for fx in by_gw.get(gw, [])
            ]
            acc = {k: sum(part[k] for part in parts) for k in additive}
            # p_start is a per-match property, not additive across a double.
            acc["p_start"] = max((part["p_start"] for part in parts), default=0.0)
            # T-15: FPL's own expected points for the NEXT gameweek are blended
            # in afterwards, in one pass over every row, because the decision to
            # blend at all depends on the whole population (see
            # `apply_ep_next_blend`). `ep_next` is a one-week-ahead number and
            # does not exist for later gameweeks, so h>=2 is always pure Gaffer.
            # The model's own estimate is retained separately so the component
            # breakdown still adds up and the external number is never presented
            # as Gaffer's own.
            model_points = acc["exp_points"]
            ep = p["ep_next"] if "ep_next" in p.keys() else None
            proj = GwProjection(
                player_id=p["id"], gw=gw, confidence=conf, generated_at=now,
                exp_points_model=round(model_points, 3),
                exp_points_ep_next=round(float(ep), 3) if ep is not None else None,
                **{k: round(v, 3) for k, v in acc.items()},
            )
            rows.append(asdict(proj))

    # The h=1 blend, decided over the whole population rather than per player:
    # an external forecast that cannot separate anybody must not be allowed to
    # flatten a ranking. Runs before the snapshot so what is retained for later
    # scoring is exactly what was published.
    regime = apply_ep_next_blend(
        rows, from_gw=from_gw, availability=avail_by_player,
        season_started=games_played > 0, form=form_by_player,
    )
    record_regime(conn, regime)

    from gaffer.store import db

    # Snapshot BEFORE the destructive replace. `projections` is wiped every run,
    # so without this there is no record to score the model against once the
    # results land.
    snapshot_projections(
        conn, rows, from_gw=from_gw, generated_at=now,
        availability=avail_by_player,
    )

    conn.execute("DELETE FROM projections")
    return db.upsert(conn, "projections", rows, ["player_id", "gw"])


def snapshot_projections(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    from_gw: int,
    generated_at: str,
    availability: dict[int, float] | None = None,
    season: str | None = None,
    deadlines: dict[int, str] | None = None,
) -> int:
    """Retain this run's projections keyed by (season, target_gw, player, as_of).

    ``is_pre_deadline`` records whether the snapshot was taken before the target
    event's deadline. Only pre-deadline snapshots are a fair basis for scoring —
    a projection computed after kickoff has seen team news the decision could
    not have. The flag is written once, at snapshot time, and never recomputed.
    """
    from gaffer.store import db

    season = season or season_mod.current(conn)
    availability = availability or {}
    if deadlines is None:
        deadlines = {
            int(r["gw"]): r["kickoff"]
            for r in conn.execute(
                "SELECT gw, MIN(kickoff) AS kickoff FROM fixtures "
                "WHERE kickoff IS NOT NULL GROUP BY gw"
            )
            if r["gw"] is not None
        }

    now_dt = gameweek.parse_deadline(generated_at)
    snaps = []
    for r in rows:
        target = int(r["gw"])
        deadline_raw = deadlines.get(target)
        deadline_dt = gameweek.parse_deadline(deadline_raw)
        # Unknown deadline -> assume pre-deadline only when the target event is
        # at or beyond the event being projected from.
        if deadline_dt is not None and now_dt is not None:
            pre = now_dt <= deadline_dt
        else:
            pre = target >= from_gw
        snaps.append({
            "season": season,
            "target_gw": target,
            "player_id": r["player_id"],
            "as_of": generated_at,
            "model_version": MODEL_VERSION,
            "horizon": target - from_gw,
            "is_pre_deadline": 1 if pre else 0,
            "deadline_time": deadline_raw,
            "p_start": r.get("p_start"),
            "exp_minutes": r.get("exp_minutes"),
            "exp_goal_pts": r.get("exp_goal_pts"),
            "exp_assist_pts": r.get("exp_assist_pts"),
            "exp_cs_pts": r.get("exp_cs_pts"),
            "exp_defcon_pts": r.get("exp_defcon_pts"),
            "exp_bonus_pts": r.get("exp_bonus_pts"),
            "exp_appearance": r.get("exp_appearance"),
            "exp_points": r.get("exp_points"),
            "confidence": r.get("confidence"),
            "availability": availability.get(r["player_id"]),
        })
    if not snaps:
        return 0
    return db.upsert(
        conn, "projection_snapshots", snaps,
        ["season", "target_gw", "player_id", "as_of"],
    )


def latest_pre_deadline_snapshot(
    conn: sqlite3.Connection, target_gw: int, season: str | None = None
) -> dict[int, dict]:
    """The snapshot a fair evaluation must use for ``target_gw``.

    Deterministic rule: among snapshots marked pre-deadline for that event, take
    the LATEST ``as_of`` — the last projection that could still have informed the
    decision. Post-deadline snapshots are never returned.
    """
    season = season or season_mod.current(conn)
    rows = conn.execute(
        "SELECT * FROM projection_snapshots WHERE season=? AND target_gw=? "
        "AND is_pre_deadline=1 ORDER BY as_of",
        (season, target_gw),
    ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:  # ordered ascending, so the last write per player wins
        out[r["player_id"]] = dict(r)
    return out
