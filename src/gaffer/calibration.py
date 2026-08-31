"""In-season calibration: how far the numbers Gaffer SHIPPED have actually held up.

`backtest.py` measures the model on a historical archive, offline, against seasons
that were already finished when the code was written. That is the right way to
choose a model and the wrong way to answer "how much should I trust this 7.3?",
because nothing in it was ever published to anybody before a kickoff.

This module measures the other thing. Every scheduled run freezes what Gaffer was
about to say: `data/state/decisions.ndjson` stores the *whole simulated
distribution* of the recommended squad before each deadline, and
`data/state/projections.ndjson` stores the per-player numbers behind it. Neither
is ever rewritten once a deadline passes. Joined to what actually happened they
are a record of the engine's own claims, scored — 78 stored distributions that
were each consulted once, for one percentile, and then never aggregated.

Two measurements:

**The PIT check (distributional).** If the published distribution is honest, the
realised total should land *uniformly* inside it. Cluster low and the simulation
runs hot; cluster high and it runs cold. This is the only check that grades the
spread Gaffer publishes rather than its central estimate, and nothing else in the
project measures it at all.

**Per-player projection error, in-season.** The frozen pre-deadline `exp_points`
against the points the player actually scored — binned, with baselines.

Four rules stop this becoming another confident number.

**n travels with every figure.** Every statistic here is returned inside a dict
carrying its own `n`. There is no path by which a mean can be read without the
count behind it, because a calibration statistic on n=1 that does not shout n=1
is worse than no calibration statistic at all.

**The sample is stated as a power, not as an apology.** A PIT check needs roughly
`(1.36 / shift)**2` gameweeks to reject uniformity at 95% for a shift of that
size, so catching a *moderate* (0.15) miscalibration takes about 83 gameweeks —
more than a season. That number is published, so a reader can see what this test
could ever detect instead of being told it is "early days" and left to assume the
answer is coming soon. It mostly is not.

**The reference class is narrow, and it is stated.** `outcome_distribution`
simulates the squad Gaffer RECOMMENDED; the realised total is the manager's own.
Those coincide only in a gameweek where he followed the advice, so the two
populations are reported separately and never silently pooled.

**A missing field is not a defect.** A GW1 snapshot carries no `move_expected`
because before the season's first deadline FPL exposes no picks and there is
nothing to compare against. It is counted, named, and not called corruption.

The module makes no network call and opens no database, for the same reason
`backtest.live_start_audit` does not: realised results are passed in, so every
figure here is reproducible in CI from files that are in the repository.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gaffer.review import PERCENTILE_BASIS, outcome_percentile

SCHEMA_VERSION = 1
CALIBRATION_VERSION = "calibration-1.0"

# --- states ----------------------------------------------------------------
#: Enough observations to report the statistic as a finding.
STATUS_MEASURED = "measured"
#: The machinery ran, the numbers exist, and the sample cannot support a claim.
#: This is a result, not a failure: it populates itself as the season runs.
STATUS_INSUFFICIENT = "insufficient_data"
#: The inputs needed were not supplied at all.
STATUS_UNAVAILABLE = "unavailable"
ALL_STATUSES = frozenset({STATUS_MEASURED, STATUS_INSUFFICIENT, STATUS_UNAVAILABLE})

#: Gameweeks before a PIT statistic may be reported as a finding rather than as
#: a running total. Eight is not a power calculation — see
#: `gameweeks_for_a_moderate_shift`, which is — it is the point below which the
#: *shape* of the sample is not worth drawing at all.
MIN_PIT_GAMEWEEKS = 8

#: Kolmogorov-Smirnov 95% critical constant: reject uniformity when D exceeds
#: this over sqrt(n).
KS_CRITICAL_95 = 1.36

#: A miscalibration worth acting on, in PIT units. A distribution whose realised
#: outcomes sit 0.15 low on average is running visibly hot.
MODERATE_SHIFT = 0.15

#: Player-gameweeks before per-player error is reported as a finding.
MIN_PROJECTION_ROWS = 100

#: Distinct gameweeks before per-player error is reported as a finding. 600 rows
#: from ONE gameweek is not 600 independent observations: they share a fixture
#: list, one round of team news and one week's weather. `backtest.pre_season`
#: makes the same point about GW1 — "ONE gameweek, not an average" — and it is
#: the reason this floor exists separately from the row floor.
MIN_PROJECTION_GAMEWEEKS = 3

#: Bins in the predicted-versus-realised curve. Matches `backtest._calibration`
#: so the site renders both through one component.
PROJECTION_BINS = 8

#: An FPL "haul". Carried per bin because a mean hides the thing a manager is
#: actually buying.
HAUL_POINTS = 10

#: What a PIT value is a percentile OF. Imported rather than restated: the same
#: sentence must travel with the number wherever it is published.
PIT_BASIS = PERCENTILE_BASIS


# ---------------------------------------------------------------------------
# Reading the record
# ---------------------------------------------------------------------------

def read_ndjson(path: Path | str) -> tuple[list[dict[str, Any]], int]:
    """Rows, and the count of lines that would not parse.

    A damaged archive degrades rather than stopping the measurement, matching
    `store.persist.restore`. The skipped count is published, so "we measured 600
    rows" can never quietly mean "we measured 600 of 1,842".
    """
    p = Path(path)
    if not p.exists():
        return [], 0
    rows: list[dict[str, Any]] = []
    skipped = 0
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(obj, dict):
            rows.append(obj)
        else:
            skipped += 1
    return rows, skipped


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """The stored JSON payload, whether persisted as text or already parsed."""
    raw = row.get("payload")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _matches(row: Mapping[str, Any], season: str | None, entry_id: int | None) -> bool:
    if season is not None and row.get("season") != season:
        return False
    return not (entry_id is not None and row.get("entry_id") != entry_id)


def final_snapshots(
    rows: Iterable[Mapping[str, Any]], *,
    season: str | None = None, entry_id: int | None = None,
) -> dict[int, dict[str, Any]]:
    """The last pre-deadline snapshot per event — the reviewable record.

    Same rule as `snapshots.final_pre_deadline`, applied to the NDJSON the
    pipeline persists, so this runs on a checkout with no database at all.
    """
    best: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not row.get("is_pre_deadline") or not _matches(row, season, entry_id):
            continue
        try:
            event = int(row["target_event"])
        except (KeyError, TypeError, ValueError):
            continue
        cur = best.get(event)
        if cur is None or str(row.get("as_of", "")) > str(cur.get("as_of", "")):
            best[event] = dict(row)
    return best


def snapshots_by_as_of(
    rows: Iterable[Mapping[str, Any]], *,
    season: str | None = None, entry_id: int | None = None,
) -> dict[tuple[int, str], dict[str, Any]]:
    """Every pre-deadline snapshot, keyed by the event and the exact run stamp.

    A review names the snapshot it read. Scoring against a *different* snapshot
    of the same gameweek would silently regrade a decision, which is the whole
    thing `snapshots` exists to prevent.
    """
    out: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        if not row.get("is_pre_deadline") or not _matches(row, season, entry_id):
            continue
        try:
            event = int(row["target_event"])
        except (KeyError, TypeError, ValueError):
            continue
        out[(event, str(row.get("as_of", "")))] = dict(row)
    return out


def reviews_by_event(
    rows: Iterable[Mapping[str, Any]], *,
    season: str | None = None, entry_id: int | None = None,
) -> dict[int, dict[str, Any]]:
    """One review per finished event, newest generation wins."""
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not _matches(row, season, entry_id):
            continue
        try:
            event = int(row["event"])
        except (KeyError, TypeError, ValueError):
            continue
        cur = out.get(event)
        if cur is None or str(row.get("generated_at", "")) >= str(cur.get("generated_at", "")):
            out[event] = dict(row)
    return out


def as_review_row(
    review: Mapping[str, Any], *, season: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Wrap a freshly built review so it counts before it has been persisted.

    The pipeline writes the artifacts and dumps the NDJSON afterwards, so at the
    moment `review.json` is assembled the review it contains is not yet in
    `reviews.ndjson`. Without this the published calibration would be exactly one
    gameweek behind its own artifact, every single week — a block saying n=1 on
    the same page as a review of gameweek 2.
    """
    return {
        "season": season or review.get("season"),
        "entry_id": review.get("entry_id"),
        "event": review.get("event"),
        "generated_at": (review.get("generated_at") or generated_at
                         or datetime.now(UTC).isoformat(timespec="seconds")),
        "snapshot_as_of": review.get("snapshot_as_of"),
        "schema_version": review.get("schema_version"),
        "payload": dict(review),
    }


def infer_scope(rows: Iterable[Mapping[str, Any]]) -> tuple[str | None, int | None]:
    """The season and entry the newest stored row describes.

    Callers that know (the pipeline, the MCP server) pass them explicitly. This
    is for a bare checkout, and it picks the newest rather than merging: two
    entries' percentiles pooled into one uniformity test is not a calibration
    statistic, it is two of them averaged.
    """
    newest: Mapping[str, Any] | None = None
    for row in rows:
        if newest is None or str(row.get("as_of", "")) > str(newest.get("as_of", "")):
            newest = row
    if newest is None:
        return None, None
    entry = newest.get("entry_id")
    try:
        entry_id = int(entry) if entry is not None else None
    except (TypeError, ValueError):
        entry_id = None
    season = newest.get("season")
    return (season if isinstance(season, str) else None), entry_id


# ---------------------------------------------------------------------------
# Observations: one finished gameweek, scored against its own published spread
# ---------------------------------------------------------------------------

def observations(
    decision_rows: Iterable[Mapping[str, Any]],
    review_rows: Iterable[Mapping[str, Any]], *,
    season: str | None = None, entry_id: int | None = None,
) -> list[dict[str, Any]]:
    """One row per finished gameweek that has both a snapshot and a result.

    Each row recomputes the percentile from the stored distribution and compares
    it against the one the review published. They should agree exactly; the
    comparison is published because a silent disagreement between two of Gaffer's
    own artifacts is the failure mode this whole module exists to catch.
    """
    decision_rows = list(decision_rows)
    finals = final_snapshots(decision_rows, season=season, entry_id=entry_id)
    exact = snapshots_by_as_of(decision_rows, season=season, entry_id=entry_id)
    out: list[dict[str, Any]] = []
    for event, review in sorted(
            reviews_by_event(review_rows, season=season, entry_id=entry_id).items()):
        rp = _payload(review)
        named = rp.get("snapshot_as_of") or review.get("snapshot_as_of")
        snap = exact.get((event, str(named))) if named else None
        matched = "named_by_the_review" if snap is not None else "final_pre_deadline"
        if snap is None:
            snap = finals.get(event)
        if snap is None:
            out.append({
                "event": event, "percentile": None, "realised": None,
                "snapshot_as_of": None, "snapshot_match": "no_snapshot",
                "distribution_size": 0, "followed_advice": None,
                "expected_at_decision": None, "published_percentile": None,
                "agrees_with_published": None,
                "note": "the review exists but no pre-deadline snapshot for this "
                        "event survives in the persisted record",
            })
            continue
        sp = _payload(snap)
        dist = sp.get("outcome_distribution")
        dist = [float(x) for x in dist] if isinstance(dist, Sequence) and not isinstance(
            dist, (str, bytes)) else None
        comparison = rp.get("comparison") or {}
        quality = rp.get("quality") or {}
        realised = comparison.get("actual_points")
        realised = float(realised) if isinstance(realised, (int, float)) else None
        pct = outcome_percentile(dist, realised)
        published = quality.get("outcome_percentile")
        published = float(published) if isinstance(published, (int, float)) else None
        agrees = None
        if pct is not None and published is not None:
            agrees = abs(round(pct, 3) - round(published, 3)) < 1e-9
        expected = quality.get("expected_at_decision")
        out.append({
            "event": event,
            "percentile": None if pct is None else round(pct, 4),
            "realised": realised,
            "snapshot_as_of": snap.get("as_of"),
            "snapshot_match": matched,
            "distribution_size": len(dist or []),
            "followed_advice": comparison.get("followed_advice"),
            "expected_at_decision": (
                float(expected) if isinstance(expected, (int, float)) else None),
            "published_percentile": published,
            "agrees_with_published": agrees,
        })
    return out


def awaiting_result(
    decision_rows: Iterable[Mapping[str, Any]],
    review_rows: Iterable[Mapping[str, Any]], *,
    season: str | None = None, entry_id: int | None = None,
) -> list[int]:
    """Events with a frozen distribution and no result yet — the pipeline of n."""
    finals = final_snapshots(decision_rows, season=season, entry_id=entry_id)
    done = set(reviews_by_event(review_rows, season=season, entry_id=entry_id))
    return sorted(e for e in finals if e not in done)


# ---------------------------------------------------------------------------
# The PIT check
# ---------------------------------------------------------------------------

def ks_statistic(values: Sequence[float]) -> float | None:
    """Kolmogorov-Smirnov distance between the sample and U(0, 1)."""
    n = len(values)
    if n == 0:
        return None
    xs = sorted(values)
    d = 0.0
    for i, u in enumerate(xs, start=1):
        d = max(d, i / n - u, u - (i - 1) / n)
    return d


def gameweeks_for_shift(shift: float = MODERATE_SHIFT) -> int:
    """Gameweeks a KS test needs to detect a PIT shift of `shift` at 95%.

    Published rather than derived silently, because the honest answer for a
    single manager's single season is "more gameweeks than a season has", and a
    reader is entitled to know that before reading any verdict below it.
    """
    if shift <= 0:
        raise ValueError("shift must be positive")
    return math.ceil((KS_CRITICAL_95 / shift) ** 2)


def pit_statistics(values: Sequence[float]) -> dict[str, Any]:
    """Uniformity of a set of PIT values. Always carries its own `n`.

    Under a calibrated simulation the realised total is a draw from the
    published distribution, so its percentile is uniform on [0, 1]: mean 0.5,
    standard deviation 1/sqrt(12). Everything here is that one fact, applied.
    """
    n = len(values)
    if n == 0:
        return {
            "n": 0, "mean": None, "mean_ci95": None, "median": None,
            "ks_d": None, "ks_critical_95": None, "rejects_uniform_at_95": None,
            "below_0_15": 0, "above_0_85": 0,
            "smallest_detectable_shift": None,
            "direction": None,
        }
    xs = sorted(float(v) for v in values)
    mean = sum(xs) / n
    # The mean of n draws from U(0,1) has standard error 1/sqrt(12n). At n=1 the
    # interval is [-0.07, 1.07] — which is the point.
    se = 1.0 / math.sqrt(12.0 * n)
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    d = ks_statistic(xs)
    crit = KS_CRITICAL_95 / math.sqrt(n)
    mid = n // 2
    median = xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2
    if hi < 0.5:
        direction = "runs_hot"
    elif lo > 0.5:
        direction = "runs_cold"
    else:
        direction = "no_detectable_bias"
    return {
        "n": n,
        "mean": round(mean, 4),
        "mean_ci95": [round(lo, 4), round(hi, 4)],
        "median": round(median, 4),
        "ks_d": None if d is None else round(d, 4),
        "ks_critical_95": round(crit, 4),
        "rejects_uniform_at_95": None if d is None else bool(d > crit),
        "below_0_15": sum(1 for x in xs if x <= 0.15),
        "above_0_85": sum(1 for x in xs if x >= 0.85),
        # A KS test at this n can only see a shift this large or larger. Capped
        # at 1.0 because a "shift" beyond the unit interval is not a thing.
        "smallest_detectable_shift": round(min(1.0, crit), 4),
        "direction": direction,
    }


def _pit_verdict(stats: Mapping[str, Any], minimum: int) -> str:
    n = int(stats.get("n") or 0)
    if n == 0:
        return ("No gameweek has both a frozen distribution and a result yet, so "
                "the simulation's spread is unmeasured this season.")
    if n < minimum:
        shortfall = minimum - n
        return (
            f"n={n}. Not enough to report. A KS test on {n} gameweek"
            f"{'' if n == 1 else 's'} can only detect a shift of "
            f"{stats.get('smallest_detectable_shift')} in percentile units, which "
            f"is not a miscalibration anybody would ship — so nothing here is a "
            f"finding. {shortfall} more finished gameweek"
            f"{'' if shortfall == 1 else 's'} before this line changes, and see "
            "`gameweeks_for_a_moderate_shift` for what it takes to detect a "
            "miscalibration worth acting on.")
    direction = stats.get("direction")
    if direction == "runs_hot":
        return (f"n={n}. Realised totals land low in the published distributions "
                f"(mean percentile {stats.get('mean')}, 95% CI "
                f"{stats.get('mean_ci95')}) — the simulation runs hot.")
    if direction == "runs_cold":
        return (f"n={n}. Realised totals land high in the published distributions "
                f"(mean percentile {stats.get('mean')}, 95% CI "
                f"{stats.get('mean_ci95')}) — the simulation runs cold.")
    return (f"n={n}. Realised totals land where the published distributions said "
            f"they would (mean percentile {stats.get('mean')}, 95% CI "
            f"{stats.get('mean_ci95')}, uniform expectation 0.5). No detectable "
            "bias — which at this n is a weak statement, not a clean bill.")


def pit_check(
    obs: Sequence[Mapping[str, Any]], *, minimum: int = MIN_PIT_GAMEWEEKS,
) -> dict[str, Any]:
    """Grade the published spread against the results it was published before.

    The headline population is gameweeks the manager actually followed the
    advice in, because that is the only population where the distribution and
    the realised total describe the same fifteen players. Everything else is
    reported beside it and labelled, never merged into it.
    """
    scored = [o for o in obs if isinstance(o.get("percentile"), (int, float))]
    followed = [o for o in scored if o.get("followed_advice") is True]
    diverged = [o for o in scored if o.get("followed_advice") is False]
    unknown = [o for o in scored if o.get("followed_advice") is None]

    headline = pit_statistics([float(o["percentile"]) for o in followed])
    everything = pit_statistics([float(o["percentile"]) for o in scored])
    disagreements = [o["event"] for o in scored if o.get("agrees_with_published") is False]

    status = (STATUS_UNAVAILABLE if not scored else
              STATUS_MEASURED if headline["n"] >= minimum else STATUS_INSUFFICIENT)
    return {
        "status": status,
        "reportable": status == STATUS_MEASURED,
        "reporting_floor_gameweeks": minimum,
        "gameweeks_short_of_the_floor": max(0, minimum - int(headline["n"])),
        "verdict": _pit_verdict(headline, minimum),
        "basis": PIT_BASIS,
        "followed_the_advice": headline,
        "every_gameweek": {
            **everything,
            "caveat": (
                "mixes reference classes. In a gameweek the manager did not "
                "follow the advice, the percentile places HIS total inside the "
                "distribution of the squad GAFFER recommended, so it measures "
                "the result against the advice rather than against the team he "
                "fielded. Reported for completeness; not the headline."),
        },
        "interval_note": (
            "`mean_ci95` is a normal approximation around the mean of n draws "
            "from U(0,1), standard error 1/sqrt(12n). At small n it runs outside "
            "[0, 1]; that is the interval telling you the sample is useless, and "
            "it is left unclamped on purpose."),
        "gameweeks_followed": len(followed),
        "gameweeks_diverged": len(diverged),
        "gameweeks_followed_unknown": len(unknown),
        "gameweeks_for_a_moderate_shift": gameweeks_for_shift(MODERATE_SHIFT),
        "moderate_shift": MODERATE_SHIFT,
        "power_note": (
            f"detecting a {MODERATE_SHIFT} shift in percentile units at 95% needs "
            f"about {gameweeks_for_shift(MODERATE_SHIFT)} gameweeks, which is more "
            "than a season. This check can catch a grossly wrong spread; it can "
            "never certify a nearly-right one."),
        "per_gameweek": [
            {k: o.get(k) for k in (
                "event", "percentile", "realised", "followed_advice",
                "distribution_size", "snapshot_as_of", "snapshot_match",
                "published_percentile", "agrees_with_published")}
            for o in obs
        ],
        "recomputation": {
            "checked": len([o for o in scored if o.get("agrees_with_published") is not None]),
            "disagreed_with_the_published_percentile": disagreements,
            "note": ("every percentile above is recomputed here from the stored "
                     "distribution and compared with the one the review "
                     "published. A disagreement means two of Gaffer's own "
                     "artifacts describe the same gameweek differently."),
        },
    }


# ---------------------------------------------------------------------------
# Per-player projection error, in-season
# ---------------------------------------------------------------------------

def latest_projection_rows(
    rows: Iterable[Mapping[str, Any]], *, season: str | None = None,
    events: Iterable[int] | None = None,
) -> dict[tuple[int, int], dict[str, Any]]:
    """The newest frozen pre-deadline row per (event, player).

    Mirrors `projection.latest_pre_deadline_snapshot`: intermediate re-runs
    inside one gameweek are history, and only the last thing said before the
    deadline was ever shipped.
    """
    wanted = None if events is None else {int(e) for e in events}
    best: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        if not row.get("is_pre_deadline"):
            continue
        if season is not None and row.get("season") != season:
            continue
        try:
            gw, pid = int(row["target_gw"]), int(row["player_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if wanted is not None and gw not in wanted:
            continue
        key = (gw, pid)
        cur = best.get(key)
        if cur is None or str(row.get("as_of", "")) >= str(cur.get("as_of", "")):
            best[key] = dict(row)
    return best


def bins(pairs: Sequence[tuple[float, float]], count: int = PROJECTION_BINS
         ) -> list[dict[str, float]]:
    """Predicted against realised, in equal-count bins ordered by prediction.

    Equal-count rather than equal-width on purpose: FPL predictions pile up near
    zero, so equal-width bins put 80% of the pool in one bar and say nothing
    about the players anybody actually owns.
    """
    if count < 2 or len(pairs) < count * 5:
        return []
    ordered = sorted(pairs, key=lambda pr: pr[0])
    n = len(ordered)
    size = n // count
    out: list[dict[str, float]] = []
    for i in range(count):
        lo = i * size
        hi = n if i == count - 1 else (i + 1) * size
        chunk = ordered[lo:hi]
        if not chunk:
            continue
        out.append({
            "pred": round(sum(p for p, _ in chunk) / len(chunk), 2),
            "actual": round(sum(a for _, a in chunk) / len(chunk), 2),
            "haul_rate": round(
                100.0 * sum(1 for _, a in chunk if a >= HAUL_POINTS) / len(chunk), 1),
            "n": len(chunk),
        })
    return out


def lookup_bin(prediction: float, curve: Sequence[Mapping[str, Any]]
               ) -> dict[str, Any] | None:
    """The measured bin nearest a given prediction, and whether it contains it.

    A projection of 7.3 when the highest measured bin averages 4.05 is outside
    the range anything has been measured on. Saying "the nearest bin realised
    3.27" without saying that would be the confident answer this module exists
    to replace.
    """
    if not curve:
        return None
    nearest = min(curve, key=lambda b: abs(float(b.get("pred", 0.0)) - prediction))
    preds = [float(b.get("pred", 0.0)) for b in curve]
    inside = min(preds) <= prediction <= max(preds)
    return {
        "prediction": round(float(prediction), 2),
        "nearest_bin": dict(nearest),
        "within_the_measured_range": inside,
        "measured_range": [round(min(preds), 2), round(max(preds), 2)],
        "caveat": (None if inside else
                   "this prediction is outside every bin that has been measured "
                   "this season; the nearest bin is shown, and it does not "
                   "describe a projection this size"),
    }


def _curve_summary(curve: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The top bin, stated. A pooled MAE hides the end of the curve anybody buys.

    The pool-wide mean error can be flat while the highest predictions are the
    ones that miss — which is exactly what one gameweek of this season shows —
    and the top bin is the only part of the curve a captaincy decision is made in.
    """
    if not curve:
        return None
    top = max(curve, key=lambda b: float(b.get("pred", 0.0)))
    gap = float(top.get("pred", 0.0)) - float(top.get("actual", 0.0))
    return {
        "top_bin": dict(top),
        "top_bin_gap": round(gap, 2),
        "top_bin_direction": ("over" if gap > 0.05 else
                              "under" if gap < -0.05 else "none"),
        "note": ("the highest-predicted bin, where captains and the differential "
                 "picks live. `top_bin_gap` is predicted minus realised, in "
                 f"points, over n={top.get('n')} player-gameweeks."),
    }


def _error_stats(triples: Sequence[tuple[float, float, float]]) -> dict[str, Any]:
    """MAE, bias and skill against a pool-mean baseline. Carries its own `n`."""
    n = len(triples)
    if n == 0:
        return {"n": 0, "mae": None, "mean_predicted": None, "mean_actual": None,
                "bias": None, "bias_direction": None,
                "baseline_mae": None, "skill_vs_pool_mean": None}
    mean_pred = sum(p for p, _, _ in triples) / n
    mean_actual = sum(a for _, a, _ in triples) / n
    mae = sum(abs(p - a) for p, a, _ in triples) / n
    base = sum(abs(mean_actual - a) for _, a, _ in triples) / n
    bias = mean_pred - mean_actual
    return {
        "n": n,
        "mae": round(mae, 3),
        "mean_predicted": round(mean_pred, 3),
        "mean_actual": round(mean_actual, 3),
        "bias": round(bias, 3),
        "bias_direction": ("over" if bias > 0.05 else
                           "under" if bias < -0.05 else "none"),
        "baseline_mae": round(base, 3),
        "skill_vs_pool_mean": (None if base <= 0 else round(1.0 - mae / base, 3)),
    }


def _persistence_stats(
    pairs_by_gw: Mapping[int, list[tuple[int, float, float, float]]],
) -> dict[str, Any]:
    """The crudest available forecast: last gameweek's points, repeated.

    Available only from the second measured gameweek onwards, so it is null for
    a season one gameweek old — and null is published rather than omitted, so a
    reader can tell "not yet" from "we did not try".
    """
    gws = sorted(pairs_by_gw)
    triples: list[tuple[float, float, float]] = []
    used: list[int] = []
    for i, gw in enumerate(gws):
        if i == 0:
            continue
        prev = {pid: actual for pid, _, actual, _ in pairs_by_gw[gws[i - 1]]}
        rows = [(prev[pid], actual, minutes)
                for pid, _, actual, minutes in pairs_by_gw[gw] if pid in prev]
        if rows:
            triples.extend(rows)
            used.append(gw)
    if not triples:
        return {"n": 0, "mae": None, "gameweeks": [],
                "unavailable_reason": "needs two measured gameweeks; there are "
                                      f"{len(gws)}"}
    n = len(triples)
    return {"n": n, "mae": round(sum(abs(p - a) for p, a, _ in triples) / n, 3),
            "gameweeks": used}


def projection_check(
    projection_rows: Iterable[Mapping[str, Any]],
    outcomes: Mapping[int, Mapping[int, Mapping[str, Any]]] | None, *,
    season: str | None = None, bin_count: int = PROJECTION_BINS,
    minimum_rows: int = MIN_PROJECTION_ROWS,
    minimum_gameweeks: int = MIN_PROJECTION_GAMEWEEKS,
) -> dict[str, Any]:
    """Frozen pre-deadline `exp_points` against what each player actually scored.

    ``outcomes`` maps a finished gameweek to ``{player_id: {"total_points": int,
    "minutes": int}}``. It is passed in rather than fetched, exactly as
    `backtest.live_start_audit` takes its outcomes: this module makes no network
    call, so every figure it produces can be reproduced in CI.
    """
    if not outcomes:
        return {
            "status": STATUS_UNAVAILABLE,
            "reportable": False,
            "n": 0,
            "unavailable_reason": (
                "no realised per-player results were supplied. This module makes "
                "no network call by design; the caller joins "
                "`data/state/projections.ndjson` to the finished gameweek."),
        }
    latest = latest_projection_rows(
        projection_rows, season=season, events=list(outcomes))
    by_gw: dict[int, list[tuple[int, float, float, float]]] = {}
    for (gw, pid), row in latest.items():
        result = (outcomes.get(gw) or {}).get(pid)
        if result is None:
            continue
        pred = row.get("exp_points")
        actual = result.get("total_points")
        if not isinstance(pred, (int, float)) or not isinstance(actual, (int, float)):
            continue
        minutes = result.get("minutes")
        minutes = float(minutes) if isinstance(minutes, (int, float)) else 0.0
        by_gw.setdefault(gw, []).append((pid, float(pred), float(actual), minutes))

    triples = [(p, a, m) for rows in by_gw.values() for _, p, a, m in rows]
    played = [(p, a, m) for p, a, m in triples if m > 0]
    pooled = _error_stats(triples)
    measured_gws = sorted(by_gw)
    enough_rows = pooled["n"] >= minimum_rows
    enough_gws = len(measured_gws) >= minimum_gameweeks
    if pooled["n"] == 0:
        status, reason = STATUS_UNAVAILABLE, "no projection row joined a result"
    elif enough_rows and enough_gws:
        status, reason = STATUS_MEASURED, None
    elif not enough_gws:
        status = STATUS_INSUFFICIENT
        reason = (
            f"{pooled['n']} player-gameweeks, but all of them from "
            f"{len(measured_gws)} gameweek{'' if len(measured_gws) == 1 else 's'} "
            f"({minimum_gameweeks} needed). Rows from one gameweek share a fixture "
            "list, one round of team news and one set of results, so they are not "
            f"{pooled['n']} independent observations. The numbers below are real "
            "and they are one week's numbers.")
    else:
        status = STATUS_INSUFFICIENT
        reason = f"{pooled['n']} player-gameweeks, below the {minimum_rows} needed"
    per_gw = []
    for gw in measured_gws:
        rows = by_gw[gw]
        stamps = [str(r.get("as_of") or "") for (g, _), r in latest.items() if g == gw]
        per_gw.append({
            "gameweek": gw, "as_of": max(stamps) if stamps else None,
            **_error_stats([(p, a, m) for _, p, a, m in rows]),
        })
    curve = bins([(p, a) for p, a, _ in triples], bin_count)
    return {
        "status": status,
        "reportable": status == STATUS_MEASURED,
        "insufficient_reason": reason,
        "minimum_rows": minimum_rows,
        "minimum_gameweeks": minimum_gameweeks,
        "unit": ("one player-gameweek: the frozen pre-deadline `exp_points` "
                 "against the points the player actually scored"),
        "snapshot": "data/state/projections.ndjson, is_pre_deadline = 1",
        "gameweeks_measured": measured_gws,
        "pooled": pooled,
        "per_gameweek": per_gw,
        "curve": curve,
        "curve_summary": _curve_summary(curve),
        "appeared": {
            **_error_stats(played),
            "curve": bins([(p, a) for p, a, _ in played], bin_count),
            "caveat": (
                "conditioned on a POST-MATCH fact — that the player played at "
                "all. It is not an error rate the model could have been held to "
                "before the deadline, and the apparent under-prediction here is "
                "mostly that selection, not pessimism. Shown because the pooled "
                "row is dominated by players who never left the bench."),
        },
        "baselines": {
            "pool_mean": (
                "predict every player the gameweek's own mean realised score. An "
                "ORACLE baseline — it already knows the mean it is predicting — "
                "so `skill_vs_pool_mean` understates the model rather than "
                "flattering it."),
            "persistence": _persistence_stats(by_gw),
        },
        "limitations": [
            "FPL revises points after `data_checked` (bonus, appeals). This block "
            "is recomputed from the frozen snapshot on every run, so a revision "
            "propagates rather than being frozen into a published number.",
            "Only gameweeks the caller supplied results for are measured; an "
            "in-progress gameweek must not be passed in, because a provisional "
            "total would be scored as if it were final.",
        ],
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _gw_label(events: Sequence[int]) -> str:
    if not events:
        return "no gameweek"
    if len(events) == 1:
        return f"GW{events[0]}"
    return "GW" + ", GW".join(str(e) for e in events)


def _headline(pit: Mapping[str, Any], proj: Mapping[str, Any]) -> str:
    parts = [str(pit.get("verdict") or "")]
    pooled = proj.get("pooled") or {}
    n = int(pooled.get("n") or 0)
    events = list(proj.get("gameweeks_measured") or [])
    if proj.get("status") == STATUS_UNAVAILABLE:
        parts.append("Per-player error is not measured in this build.")
    else:
        sentence = (
            f"Per-player, n={n} player-gameweeks over {_gw_label(events)}: mean "
            f"absolute error {pooled.get('mae')} points against "
            f"{pooled.get('baseline_mae')} for a pool-mean baseline.")
        if not proj.get("reportable"):
            sentence += f" Not reportable: {proj.get('insufficient_reason')}"
        parts.append(sentence)
    return " ".join(p for p in parts if p)


def build(
    *,
    decision_rows: Iterable[Mapping[str, Any]],
    review_rows: Iterable[Mapping[str, Any]],
    projection_rows: Iterable[Mapping[str, Any]] = (),
    outcomes: Mapping[int, Mapping[int, Mapping[str, Any]]] | None = None,
    season: str | None = None,
    entry_id: int | None = None,
    generated_at: str | None = None,
    minimum_gameweeks: int = MIN_PIT_GAMEWEEKS,
    skipped_lines: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """The whole in-season calibration block, from rows already in memory."""
    decision_rows = list(decision_rows)
    review_rows = list(review_rows)
    obs = observations(decision_rows, review_rows, season=season, entry_id=entry_id)
    pit = pit_check(obs, minimum=minimum_gameweeks)
    proj = projection_check(projection_rows, outcomes, season=season)
    if pit["reportable"] or proj.get("reportable"):
        status = STATUS_MEASURED
    elif pit["status"] == STATUS_UNAVAILABLE and proj["status"] == STATUS_UNAVAILABLE:
        status = STATUS_UNAVAILABLE
    else:
        status = STATUS_INSUFFICIENT
    return {
        "schema_version": SCHEMA_VERSION,
        "calibration_version": CALIBRATION_VERSION,
        "season": season,
        "entry_id": entry_id,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "status": status,
        "headline": _headline(pit, proj),
        "measures": "what Gaffer published before each deadline, scored against "
                    "what happened. Nothing here is computed on an archive.",
        "distribution": pit,
        "projection": proj,
        "awaiting_result": awaiting_result(
            decision_rows, review_rows, season=season, entry_id=entry_id),
        "sources": {
            "decisions": "data/state/decisions.ndjson",
            "reviews": "data/state/reviews.ndjson",
            "projections": "data/state/projections.ndjson",
            "unparseable_lines": dict(skipped_lines or {}),
        },
        "limitations": [
            "Every figure is one manager's season. There is no cross-sectional "
            "sample to borrow strength from, and there will not be one.",
            "A snapshot without `decision.comparison.move_expected` is correct, "
            "not corrupt: before the season's first deadline FPL exposes no "
            "picks, so there is no held squad to compare a move against.",
            "The percentile's reference class is the RECOMMENDED squad's own "
            "simulated range. It is not a rank against other managers.",
            "GW1 is a different regime from the rest of the season: no "
            "season-to-date history exists, so the projection behind it comes "
            "from prior-season rates and the price prior alone. A season whose "
            "only measured gameweek is GW1 has measured that regime, not this "
            "one.",
        ],
    }


def build_from_state(
    state_dir: Path | str | None = None, *,
    outcomes: Mapping[int, Mapping[int, Mapping[str, Any]]] | None = None,
    season: str | None = None,
    entry_id: int | None = None,
    generated_at: str | None = None,
    minimum_gameweeks: int = MIN_PIT_GAMEWEEKS,
    extra_review_rows: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Read the persisted NDJSON and build the block. No network, no database.

    ``extra_review_rows`` carries reviews that exist but have not been dumped to
    NDJSON yet — see `as_review_row`. They are appended, and `reviews_by_event`
    keeps the newest generation per event, so a re-run cannot double-count one.
    """
    if state_dir is None:
        from gaffer import config
        state_dir = Path(config.DATA_DIR) / "state"
    root = Path(state_dir)
    decisions, d_bad = read_ndjson(root / "decisions.ndjson")
    reviews, r_bad = read_ndjson(root / "reviews.ndjson")
    reviews.extend(dict(r) for r in extra_review_rows)
    projections, p_bad = read_ndjson(root / "projections.ndjson")
    if season is None or entry_id is None:
        inferred_season, inferred_entry = infer_scope(decisions)
        season = season if season is not None else inferred_season
        entry_id = entry_id if entry_id is not None else inferred_entry
    return build(
        decision_rows=decisions, review_rows=reviews, projection_rows=projections,
        outcomes=outcomes, season=season, entry_id=entry_id,
        generated_at=generated_at, minimum_gameweeks=minimum_gameweeks,
        skipped_lines={"decisions.ndjson": d_bad, "reviews.ndjson": r_bad,
                       "projections.ndjson": p_bad},
    )
