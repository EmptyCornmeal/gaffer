"""Post-gameweek review (T-23).

The hard part of a review is not arithmetic, it is refusing to cheat. Once the
results are in, *every* alternative looks obvious, and a review built after the
fact will always conclude that Gaffer was nearly right and the user was nearly
wrong. Two rules keep this honest:

**No hindsight.** The only decision record admissible here is the immutable
pre-deadline snapshot (``gaffer.snapshots``). Alternatives are evaluated with the
projections and scenario distributions that existed *before* the deadline. The
"best possible XI in hindsight" is computed and shown — but explicitly labelled
as unknowable at the time, and never used to score a decision.

**Decision quality is not outcome.** A +EV captain who blanks was a good call.
A -EV punt that hauls was a bad call that got paid. The stored pre-deadline
distribution gives the outcome's percentile, which is what separates the two, and
the verdict wording is driven by EV and percentile independently.

Reviews are season-aware and idempotent, and can be regenerated when FPL revises
points after bonus or appeal — the *decision* side is read from the immutable
snapshot and is never rewritten here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gaffer import season as season_mod
from gaffer import snapshots

REVIEW_SCHEMA_VERSION = 1
REVIEW_VERSION = "review-1.0"

# --- verdicts: EV and outcome, never conflated -----------------------------
VERDICT_GOOD_LUCKY = "good_decision_lucky"
VERDICT_GOOD_UNLUCKY = "good_decision_unlucky"
VERDICT_GOOD_NORMAL = "good_decision"
VERDICT_BAD_LUCKY = "bad_decision_lucky"
VERDICT_BAD_UNLUCKY = "bad_decision_unlucky"
VERDICT_BAD_NORMAL = "bad_decision"
VERDICT_UNKNOWN = "not_assessable"

#: Outcome percentiles outside this band are called unusual. Inside it, the
#: result is simply what the distribution said would probably happen.
LUCKY_ABOVE = 0.85
UNLUCKY_BELOW = 0.15


@dataclass
class Attribution:
    """Where the gameweek's points actually came from, in FPL's own units."""

    captaincy: float = 0.0        # points the armband added over a plain start
    hit_cost: int = 0             # what transfers cost
    transfers: float = 0.0        # in-players minus out-players, this GW
    bench: float = 0.0            # points left on the bench
    chip: float = 0.0             # points the chip added
    starting_xi: float = 0.0      # the XI's own scoring
    autosubs: float = 0.0         # points rescued by substitutions

    def as_dict(self) -> dict[str, Any]:
        return {k: round(v, 2) if isinstance(v, float) else v
                for k, v in self.__dict__.items()}


@dataclass
class Comparison:
    """Four worlds: advised, actual, hold, and (labelled) hindsight."""

    recommended_points: float | None
    actual_points: float | None
    hold_points: float | None
    hindsight_points: float | None
    followed_advice: bool | None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        def r(v):
            return None if v is None else round(v, 2)
        return {
            "recommended_points": r(self.recommended_points),
            "actual_points": r(self.actual_points),
            "hold_points": r(self.hold_points),
            "hindsight_points": r(self.hindsight_points),
            "hindsight_is_unknowable": True,
            "followed_advice": self.followed_advice,
            "note": self.note,
        }


@dataclass
class Quality:
    """Decision quality and outcome luck, measured separately."""

    expected_at_decision: float | None
    realised: float | None
    percentile: float | None
    positive_ev: bool | None
    verdict: str
    explanation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected_at_decision": (None if self.expected_at_decision is None
                                     else round(self.expected_at_decision, 2)),
            "realised": None if self.realised is None else round(self.realised, 2),
            "outcome_percentile": (None if self.percentile is None
                                   else round(self.percentile, 3)),
            "positive_ev": self.positive_ev,
            "verdict": self.verdict,
            "explanation": self.explanation,
        }


@dataclass
class Review:
    season: str
    entry_id: int
    event: int
    generated_at: str
    snapshot_as_of: str | None
    comparison: Comparison
    attribution: Attribution
    quality: Quality
    lesson: dict[str, Any] | None
    league: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    #: The measurable per-gameweek signals the learning loop pattern-matches on.
    #: Stored with the review so a later lesson can read history without
    #: recomputing it from raw results.
    facts: dict[str, Any] = field(default_factory=dict)
    schema_version: int = REVIEW_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "review_version": REVIEW_VERSION,
            "season": self.season,
            "entry_id": self.entry_id,
            "event": self.event,
            "generated_at": self.generated_at,
            "snapshot_as_of": self.snapshot_as_of,
            "has_snapshot": self.snapshot_as_of is not None,
            "comparison": self.comparison.as_dict(),
            "attribution": self.attribution.as_dict(),
            "quality": self.quality.as_dict(),
            "lesson": self.lesson,
            "league": self.league,
            "limitations": self.limitations,
            "facts": self.facts,
        }


# ---------------------------------------------------------------------------
# Decision quality
# ---------------------------------------------------------------------------

def outcome_percentile(distribution: list[float] | None, realised: float | None
                       ) -> float | None:
    """Where the result landed in the distribution we published *before* kickoff.

    This is the number that separates a bad decision from bad luck, and it is
    only meaningful because the distribution was stored pre-deadline.
    """
    if not distribution or realised is None:
        return None
    arr = sorted(distribution)
    below = sum(1 for x in arr if x < realised)
    equal = sum(1 for x in arr if x == realised)
    return (below + 0.5 * equal) / len(arr)


def assess(
    *, expected: float | None, realised: float | None,
    percentile: float | None, hold_expected: float | None,
) -> Quality:
    """Judge the decision on its EV and the outcome on its percentile.

    These are answered independently on purpose. Collapsing them is exactly the
    error a results-driven review makes: it praises whatever worked.
    """
    if expected is None or realised is None:
        return Quality(expected, realised, percentile, None, VERDICT_UNKNOWN,
                       "No pre-deadline record exists for this gameweek, so the "
                       "decision cannot be assessed — only the result is known.")

    positive_ev = hold_expected is None or expected >= hold_expected
    lucky = percentile is not None and percentile >= LUCKY_ABOVE
    unlucky = percentile is not None and percentile <= UNLUCKY_BELOW

    if positive_ev:
        verdict = (VERDICT_GOOD_LUCKY if lucky else
                   VERDICT_GOOD_UNLUCKY if unlucky else VERDICT_GOOD_NORMAL)
    else:
        verdict = (VERDICT_BAD_LUCKY if lucky else
                   VERDICT_BAD_UNLUCKY if unlucky else VERDICT_BAD_NORMAL)

    ev_phrase = ("was the higher-expected-value choice at the deadline"
                 if positive_ev else
                 "had lower expected value than holding at the deadline")
    if lucky:
        luck_phrase = (f"the result landed in the top "
                       f"{100 * (1 - (percentile or 0)):.0f}% of what was "
                       "simulated — an unusually good outcome")
    elif unlucky:
        luck_phrase = (f"the result landed in the bottom "
                       f"{100 * (percentile or 0):.0f}% of what was simulated — "
                       "an unusually bad outcome")
    elif percentile is not None:
        luck_phrase = ("the result was close to what the distribution predicted, "
                       "so luck is not the story")
    else:
        luck_phrase = "no stored distribution, so outcome luck cannot be measured"

    tail = ""
    if positive_ev and unlucky:
        tail = " A good decision with a bad result is still a good decision."
    elif not positive_ev and lucky:
        tail = " A bad decision that got paid is still a bad decision."

    return Quality(expected, realised, percentile, positive_ev, verdict,
                   f"The decision {ev_phrase}; {luck_phrase}.{tail}")


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def attribute(
    *, xi: list[int], bench: list[int], captain: int | None,
    vice_used: bool, points: dict[int, int], multiplier: int = 2,
    hits: int = 0, chip: str | None = None, subs_in: list[int] | None = None,
    subs_out: list[int] | None = None,
    transfers_in: list[int] | None = None,
    transfers_out: list[int] | None = None,
) -> Attribution:
    """Split the realised score into the decisions that produced it.

    Every term is measured against the world where that one decision was not
    made, holding everything else fixed — so the parts describe choices rather
    than merely re-adding the same points under different headings.
    """
    subs_in = subs_in or []
    subs_out = subs_out or []
    p = {k: int(v or 0) for k, v in points.items()}

    xi_points = sum(p.get(i, 0) for i in xi)
    cap_points = p.get(captain, 0) * (multiplier - 1) if captain else 0
    bench_points = sum(p.get(i, 0) for i in bench)
    # Autosub value: what the replacements scored instead of the zeros they
    # replaced. Those zeros are zero by definition, so it is the subs' points.
    autosub_points = sum(p.get(i, 0) for i in subs_in)
    chip_points = 0.0
    if chip == "bboost":
        chip_points = float(bench_points)
    elif chip == "3xc" and captain:
        chip_points = float(p.get(captain, 0))

    transfer_delta = 0.0
    if transfers_in or transfers_out:
        transfer_delta = (sum(p.get(i, 0) for i in (transfers_in or []))
                          - sum(p.get(i, 0) for i in (transfers_out or [])))

    return Attribution(
        captaincy=float(cap_points),
        hit_cost=int(hits),
        transfers=float(transfer_delta),
        bench=float(bench_points),
        chip=chip_points,
        starting_xi=float(xi_points),
        autosubs=float(autosub_points),
    )


# ---------------------------------------------------------------------------
# The lesson
# ---------------------------------------------------------------------------

#: Every lesson must be one of these, tied to a measurable pattern. Free-form
#: encouragement is not a lesson; it is filler that trains you to ignore the box.
LESSON_MINUTES = "minutes_overconfidence"
LESSON_CAPTAIN = "captaincy_calibration"
LESSON_BENCH = "bench_allocation"
LESSON_HITS = "hit_threshold"
LESSON_FIXTURES = "fixture_strength_error"
LESSON_POSTURE = "league_risk_posture"
LESSON_CHIP = "chip_timing"
LESSON_NONE = "no_pattern_yet"
ALL_LESSONS = frozenset({
    LESSON_MINUTES, LESSON_CAPTAIN, LESSON_BENCH, LESSON_HITS,
    LESSON_FIXTURES, LESSON_POSTURE, LESSON_CHIP, LESSON_NONE,
})

#: How many gameweeks of the same signal before it is called a pattern. One
#: gameweek of anything is noise, and saying so is more useful than a lesson.
PATTERN_MIN_WEEKS = 2


def lesson_from_history(
    recent: list[dict[str, Any]], *, min_weeks: int = PATTERN_MIN_WEEKS,
) -> dict[str, Any]:
    """One evidence-based lesson, or an honest "not yet".

    ``recent`` is a list of per-gameweek fact dicts, most recent first. A signal
    must repeat across ``min_weeks`` gameweeks before it is reported, because a
    single bad captain pick is a distribution doing its job.
    """
    if len(recent) < min_weeks:
        return {
            "key": LESSON_NONE,
            "text": (f"Only {len(recent)} reviewed gameweek(s) so far — not "
                     "enough to separate a pattern from variance."),
            "evidence": [], "weeks": len(recent),
        }

    window = recent[:6]

    def count(pred) -> int:
        return sum(1 for r in window if pred(r))

    # Ordered by how much each pattern actually costs over a season.
    candidates = [
        (LESSON_MINUTES,
         count(lambda r: r.get("zero_minute_starters", 0) >= 2),
         "Two or more of your starters recorded zero minutes in {n} of the last "
         "{w} gameweeks. Minutes are the weakest part of the model; treat a "
         "start probability under 60% as a bench player, not a starter."),
        (LESSON_CAPTAIN,
         count(lambda r: (r.get("captain_percentile") or 1.0) <= 0.25),
         "Your captain landed in the bottom quartile of his own simulated range "
         "in {n} of the last {w} gameweeks. That is captaincy variance, not "
         "captaincy error — but if it persists, prefer the higher-floor armband."),
        (LESSON_HITS,
         count(lambda r: r.get("hits", 0) > 0
               and (r.get("transfer_delta") or 0) < r.get("hits", 0)),
         "You paid a hit that did not clear its own cost in {n} of the last {w} "
         "gameweeks. Gaffer's actionable bar is 1.0 expected points over holding; "
         "a -4 needs to clear four."),
        (LESSON_BENCH,
         count(lambda r: r.get("bench_points", 0) >= 12),
         "You left 12+ points on the bench in {n} of the last {w} gameweeks. "
         "That is a lineup-order problem, not a squad problem."),
        (LESSON_FIXTURES,
         count(lambda r: abs(r.get("forecast_error") or 0) >= 15),
         "Your projected total missed the realised one by 15+ points in {n} of "
         "the last {w} gameweeks, in the same direction. The fixture-strength "
         "model is the likeliest source."),
    ]
    key, n, template = max(candidates, key=lambda c: c[1])
    if n < min_weeks:
        return {
            "key": LESSON_NONE,
            "text": ("No pattern repeats across enough gameweeks yet. The last "
                     f"{len(window)} weeks look like normal variance."),
            "evidence": [], "weeks": len(window),
        }
    return {
        "key": key,
        "text": template.format(n=n, w=len(window)),
        "evidence": [
            {"event": r.get("event"), "detail": r.get("summary", "")}
            for r in window
        ],
        "weeks": len(window),
        "occurrences": n,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save(conn: sqlite3.Connection, review: Review) -> None:
    """Store (or correct) a review. Idempotent per (season, entry, event).

    Rewritable because FPL revises points after bonus and appeals. The decision
    side is read from the immutable snapshot, so a correction can only change
    what *happened*, never what was advised.
    """
    conn.execute(
        "INSERT INTO gw_reviews (season, entry_id, event, generated_at, "
        "snapshot_as_of, schema_version, payload) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(season, entry_id, event) DO UPDATE SET "
        "generated_at=excluded.generated_at, payload=excluded.payload, "
        "snapshot_as_of=excluded.snapshot_as_of, "
        "schema_version=excluded.schema_version",
        (review.season, review.entry_id, review.event, review.generated_at,
         review.snapshot_as_of, review.schema_version,
         json.dumps(review.as_dict(), default=str)),
    )
    conn.commit()


def load(
    conn: sqlite3.Connection, entry_id: int, event: int,
    season: str | None = None,
) -> dict[str, Any] | None:
    season = season or season_mod.current(conn)
    r = conn.execute(
        "SELECT payload FROM gw_reviews WHERE season=? AND entry_id=? AND event=?",
        (season, entry_id, event)).fetchone()
    return json.loads(r["payload"]) if r else None


def load_all(
    conn: sqlite3.Connection, entry_id: int, season: str | None = None,
) -> list[dict[str, Any]]:
    season = season or season_mod.current(conn)
    return [
        json.loads(r["payload"]) for r in conn.execute(
            "SELECT payload FROM gw_reviews WHERE season=? AND entry_id=? "
            "ORDER BY event DESC", (season, entry_id))
    ]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build(
    conn: sqlite3.Connection, *, entry_id: int, event: int,
    actual: dict[str, Any], points: dict[int, int], now: datetime | None = None,
    hindsight_points: float | None = None, league: list[dict[str, Any]] | None = None,
    season: str | None = None,
) -> Review:
    """Assemble one gameweek's review from its immutable snapshot and the result.

    ``actual`` is what the user really did (from ``entry/{id}/event/{gw}/picks/``),
    ``points`` is the realised per-player score. Everything judgemental comes from
    the snapshot; nothing here reads a projection made after the deadline.
    """
    season = season or season_mod.current(conn)
    now = now or datetime.now(UTC)
    snap = snapshots.final_pre_deadline(conn, entry_id, event, season)

    limits: list[str] = []
    rec_pts = hold_pts = exp = pctile = None
    followed = None
    snapshot_as_of = None

    if snap is None:
        limits.append(
            "No pre-deadline snapshot exists for this gameweek, so decision "
            "quality cannot be assessed — only the outcome is shown.")
    else:
        snapshot_as_of = snap.as_of
        dec = (snap.payload.get("decision") or {})
        cmpd = dec.get("comparison") or {}
        exp = cmpd.get("move_expected")
        hold_pts = cmpd.get("hold_expected")
        rec_ids = set(dec.get("transfers_in") or [])
        actual_in = set(actual.get("transfers_in") or [])
        followed = rec_ids == actual_in
        rec_pts = sum(int(points.get(p, 0) or 0)
                      for p in dec.get("starting") or [])
        if dec.get("captain") is not None:
            rec_pts += int(points.get(dec["captain"], 0) or 0)

    actual_pts = actual.get("total_points")
    dist = (snap.payload.get("outcome_distribution") if snap else None)
    pctile = outcome_percentile(dist, actual_pts)
    if snap is not None and not dist:
        limits.append(
            "The snapshot stored no outcome distribution, so the result's "
            "percentile — and therefore luck — cannot be measured.")

    attribution = attribute(
        xi=actual.get("starting") or [], bench=actual.get("bench") or [],
        captain=actual.get("captain"), vice_used=bool(actual.get("vice_used")),
        points=points, multiplier=int(actual.get("multiplier") or 2),
        hits=int(actual.get("hits") or 0), chip=actual.get("chip"),
        subs_in=actual.get("subs_in"), subs_out=actual.get("subs_out"),
        transfers_in=actual.get("transfers_in"),
        transfers_out=actual.get("transfers_out"))

    quality = assess(expected=exp, realised=actual_pts, percentile=pctile,
                     hold_expected=hold_pts)

    prior = load_all(conn, entry_id, season)
    facts = [{"event": event, **_facts(actual, points, attribution, pctile)}] + [
        {"event": r.get("event"), **(r.get("facts") or {})} for r in prior
        if r.get("event") != event
    ]
    lesson = lesson_from_history(facts)

    limits.append(
        "The hindsight column is the best XI available with the benefit of the "
        "results; it was not knowable before the deadline and is never used to "
        "score a decision.")

    review = Review(
        season=season, entry_id=entry_id, event=event,
        generated_at=now.astimezone(UTC).isoformat(timespec="seconds"),
        snapshot_as_of=snapshot_as_of,
        comparison=Comparison(
            recommended_points=rec_pts, actual_points=actual_pts,
            hold_points=hold_pts, hindsight_points=hindsight_points,
            followed_advice=followed,
            note=("You made the recommended move." if followed else
                  "You did something different from the recommendation."
                  if followed is False else
                  "No recommendation was recorded for this gameweek.")),
        attribution=attribution, quality=quality, lesson=lesson,
        league=league or [], limitations=limits,
        facts=_facts(actual, points, attribution, pctile))
    return review


def _facts(actual, points, attribution, pctile) -> dict[str, Any]:
    """The measurable per-gameweek signals the learning loop pattern-matches on."""
    xi = actual.get("starting") or []
    zeros = sum(1 for p in xi if int(points.get(p, 0) or 0) == 0)
    return {
        "zero_minute_starters": zeros,
        "bench_points": attribution.bench,
        "hits": attribution.hit_cost,
        "transfer_delta": attribution.transfers,
        "captain_percentile": pctile,
        "forecast_error": None,
        "summary": f"{zeros} blank starter(s), {attribution.bench:.0f} on the bench",
    }
