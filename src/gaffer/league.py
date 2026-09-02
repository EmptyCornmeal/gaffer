"""League intelligence: rivals, effective ownership, placing probabilities (T-17/T-18).

Global ``selected_by_percent`` is a percentage across millions of managers. What
moves your position in a mini-league is ownership *inside that league*, which in
a four-person league takes exactly four values and is fully observable from three
API calls. The two are different quantities that happen to share a name, and this
module keeps them apart.

Everything here is public and unauthenticated. Rival picks are only readable once
a gameweek's deadline has passed, so "unknown" is a first-class state and is never
rendered as "owns nothing".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

LEAGUE_VERSION = "league-1.0"

# --- classification --------------------------------------------------------
TINY = "tiny_private"        # every rival readable; exact EO
SMALL = "small_private"
MEDIUM = "medium"
LARGE = "large"
GLOBAL = "global"            # auto-joined system leagues (Overall, a club, a region)

#: How many rival squads we will fetch for each class. Unbounded fetching of a
#: 10-million-entry league is not a strategy.
COHORT_LIMIT = {TINY: 50, SMALL: 50, MEDIUM: 50, LARGE: 60, GLOBAL: 60}


def classify(size: int | None, league_type: str | None) -> str:
    """Classify by what we can actually know, not by size alone."""
    if league_type == "s":
        return GLOBAL
    if size is None:
        return MEDIUM
    if size <= 8:
        return TINY
    if size <= 30:
        return SMALL
    if size <= 500:
        return MEDIUM
    return LARGE


# --- data quality ----------------------------------------------------------
PICKS_OK = "revealed"
PICKS_NONE_YET = "no_public_picks_yet"
PICKS_STALE = "stale"
PICKS_FAILED = "fetch_failed"
PICKS_PRIVATE = "unavailable"


@dataclass
class RivalEntry:
    entry_id: int
    entry_name: str = ""
    manager: str = ""
    rank: int | None = None
    total: int = 0
    event_total: int = 0
    starting: list[int] = field(default_factory=list)
    bench: list[int] = field(default_factory=list)
    captain: int | None = None
    vice: int | None = None
    picks_event: int | None = None
    picks_status: str = PICKS_NONE_YET
    chips_used: list[str] = field(default_factory=list)
    hits: int = 0

    @property
    def has_picks(self) -> bool:
        return self.picks_status in (PICKS_OK, PICKS_STALE) and bool(self.starting)

    @property
    def squad(self) -> list[int]:
        return list(self.starting) + list(self.bench)


@dataclass
class LeagueState:
    league_id: int
    name: str
    league_type: str
    classification: str
    size: int | None
    me: int
    entries: list[RivalEntry] = field(default_factory=list)
    cohort_truncated: bool = False
    source_event: int | None = None
    note: str = ""

    @property
    def rivals(self) -> list[RivalEntry]:
        return [e for e in self.entries if e.entry_id != self.me]

    @property
    def my_entry(self) -> RivalEntry | None:
        return next((e for e in self.entries if e.entry_id == self.me), None)

    @property
    def coverage(self) -> float:
        """Share of rivals whose squad we actually know."""
        rv = self.rivals
        if not rv:
            return 0.0
        return sum(1 for r in rv if r.has_picks) / len(rv)

    def data_quality(self) -> dict[str, Any]:
        rv = self.rivals
        return {
            "rivals": len(rv),
            "with_picks": sum(1 for r in rv if r.has_picks),
            "coverage_pct": round(100.0 * self.coverage, 1),
            "cohort_truncated": self.cohort_truncated,
            "picks_source_event": self.source_event,
            "statuses": sorted({r.picks_status for r in rv}) or [PICKS_NONE_YET],
        }


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def _entry_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Standings plus pre-season `new_entries`, de-duplicated by entry id."""
    rows, seen = [], set()
    for block in ("standings", "new_entries"):
        for r in (payload.get(block) or {}).get("results", []) or []:
            eid = r.get("entry")
            if eid is None or eid in seen:
                continue
            seen.add(eid)
            rows.append(r)
    return rows


def fetch_league(
    client: Any, league_id: int, me: int, *, squad_event: int | None,
    max_pages: int = 20,
) -> LeagueState:
    """Ingest one league. Bounded: never walks an unbounded global league."""
    first = client.league_classic(league_id, 1)
    meta = first.get("league", {}) or {}
    rows = _entry_rows(first)
    truncated = False
    page = 1
    while (first.get("standings") or {}).get("has_next") and page < max_pages:
        page += 1
        nxt = client.league_classic(league_id, page)
        rows += _entry_rows(nxt)
        first = nxt
        if not (nxt.get("standings") or {}).get("has_next"):
            break
    else:
        if (first.get("standings") or {}).get("has_next"):
            truncated = True

    # No rows is "not published yet", not "a league of zero people" — the global
    # leagues publish no standings at all before GW1. Recording 0 would make the
    # league look measured when nothing has been measured.
    size = (len(rows) or None) if not truncated else None
    cls = classify(size, meta.get("league_type"))

    state = LeagueState(
        league_id=league_id, name=meta.get("name", str(league_id)),
        league_type=meta.get("league_type", "?"), classification=cls,
        size=size, me=me, cohort_truncated=truncated, source_event=squad_event,
        note="" if rows else "no standings published for this league yet",
    )

    # Bounded cohort: me, plus the nearest rivals by rank.
    limit = COHORT_LIMIT.get(cls, 50)
    rows.sort(key=lambda r: (r.get("rank") or 10**9))
    chosen = rows[:limit]
    if not any(r.get("entry") == me for r in chosen):
        mine = next((r for r in rows if r.get("entry") == me), None)
        if mine:
            chosen = [mine] + chosen[: limit - 1]
    if len(chosen) < len(rows):
        state.cohort_truncated = True
        state.note = (
            f"{len(rows)} entries; compared against the nearest {len(chosen)}."
        )

    for r in chosen:
        e = RivalEntry(
            entry_id=r["entry"],
            entry_name=r.get("entry_name") or "",
            manager=" ".join(x for x in (
                r.get("player_first_name"), r.get("player_last_name")) if x)
            or r.get("player_name", ""),
            rank=r.get("rank"), total=r.get("total") or 0,
            event_total=r.get("event_total") or 0,
        )
        _attach_picks(client, e, squad_event)
        state.entries.append(e)
    return state


def _attach_picks(client: Any, entry: RivalEntry, squad_event: int | None) -> None:
    """Read a rival's squad, respecting the public-picks timing rules."""
    if squad_event is None:
        entry.picks_status = PICKS_NONE_YET
        return
    try:
        payload = client.entry_picks(entry.entry_id, squad_event)
    except Exception as exc:  # noqa: BLE001 - any transport/HTTP failure
        code = getattr(getattr(exc, "response", None), "status_code", None)
        entry.picks_status = PICKS_PRIVATE if code == 404 else PICKS_FAILED
        return
    picks = (payload or {}).get("picks")
    if not isinstance(picks, list) or not picks:
        entry.picks_status = PICKS_FAILED
        return
    entry.picks_event = squad_event
    entry.picks_status = PICKS_OK
    for p in picks:
        pid = p.get("element")
        if not isinstance(pid, int):
            continue
        if (p.get("position") or 99) <= 11:
            entry.starting.append(pid)
        else:
            entry.bench.append(pid)
        if p.get("is_captain"):
            entry.captain = pid
        if p.get("is_vice_captain"):
            entry.vice = pid
    eh = payload.get("entry_history") or {}
    entry.hits = int(eh.get("event_transfers_cost") or 0)
    if payload.get("active_chip"):
        entry.chips_used.append(str(payload["active_chip"]))


# ---------------------------------------------------------------------------
# League-scoped ownership
# ---------------------------------------------------------------------------

@dataclass
class Ownership:
    player_id: int
    owners: int
    captains: int
    n_rivals: int

    @property
    def ownership(self) -> float:
        return self.owners / self.n_rivals if self.n_rivals else 0.0

    @property
    def effective(self) -> float:
        """EO = share owning + share captaining. A captain counts twice."""
        if not self.n_rivals:
            return 0.0
        return (self.owners + self.captains) / self.n_rivals

    @property
    def captain_eo(self) -> float:
        return self.captains / self.n_rivals if self.n_rivals else 0.0


def league_ownership(state: LeagueState) -> dict[int, Ownership]:
    """Ownership across the rivals whose squads we actually know.

    Computed over rivals with revealed picks only — inferring from an unknown
    squad would invent the very number this exists to measure.
    """
    known = [r for r in state.rivals if r.has_picks]
    n = len(known)
    out: dict[int, Ownership] = {}
    for r in known:
        for pid in r.starting:
            o = out.setdefault(pid, Ownership(pid, 0, 0, n))
            o.owners += 1
        if r.captain is not None:
            o = out.setdefault(r.captain, Ownership(r.captain, 0, 0, n))
            o.captains += 1
    for o in out.values():
        o.n_rivals = n
    return out


#: How many ownership rows the capped lists publish. Shields and threats are
#: drawn from every player any rival owns, so they need a cap. Differentials are
#: drawn from the fifteen players *you* own and cannot grow past a squad, so
#: capping them only ever threw real ones away.
OWNERSHIP_ROWS = 10


def shields_and_differentials(
    state: LeagueState, my_squad: list[int], my_captain: int | None = None,
    xp: dict[int, float] | None = None,
) -> dict[str, Any]:
    """What protects your position, and what can move it. Four answers, not two.

    ``shields``            players you own that at least half the league owns
    ``differentials``      players you own that no rival owns
    ``threats``            players your rivals own and you do not
    ``my_captain_eo_pct``  the share of the league that captained who you did

    ``threats`` is the only one of the three lists that names a move you have
    *not* made, and ``my_captain_eo_pct`` is what decides whether a differential
    captain is worth its variance. Both were computed here on every run and
    dropped by every consumer downstream.

    Shields and threats rank by effective ownership, which is what decides how
    far someone else's haul moves you. Differentials cannot: by construction
    every one of them has an effective ownership of exactly zero, so ownership
    has nothing to separate them by, and the list used to come out sorted by
    ``player_id`` — a database identifier standing in for a ranking — and then
    cut to ten, which keeps whichever differentials happen to have the smallest
    ids. Pass ``xp`` (player id -> projected points) to rank them by what they
    are worth. Without it the list is returned COMPLETE and unranked, for a
    caller to rank once it has joined the projections; an unranked list is never
    truncated, because a truncated unranked list is just a lost one.

    ``my_captain_eo_pct`` is ``None`` when no captain was given. Zero is a real
    answer — nobody else captained him — and must not stand in for not knowing.
    """
    own = league_ownership(state)
    mine = set(my_squad)
    n_known = len([r for r in state.rivals if r.has_picks])
    shields, diffs, threats = [], [], []
    for pid, o in own.items():
        entry = {"player_id": pid, "owners": o.owners, "n_rivals": o.n_rivals,
                 "ownership_pct": round(100 * o.ownership, 1),
                 "effective_ownership_pct": round(100 * o.effective, 1),
                 "captain_eo_pct": round(100 * o.captain_eo, 1)}
        if pid in mine and o.ownership >= 0.5:
            shields.append(entry)
        elif pid not in mine:
            threats.append(entry)
    for pid in mine:
        o = own.get(pid)
        if o is None or o.ownership == 0:
            diffs.append({"player_id": pid, "owners": 0, "n_rivals": n_known,
                          "ownership_pct": 0.0, "effective_ownership_pct": 0.0,
                          "captain_eo_pct": 0.0})
    key = lambda e: -e["effective_ownership_pct"]  # noqa: E731
    if xp is None:
        diffs.sort(key=lambda e: e["player_id"])
    else:
        diffs.sort(key=lambda e: (-(xp.get(e["player_id"]) or 0.0), e["player_id"]))
    return {
        "shields": sorted(shields, key=key)[:OWNERSHIP_ROWS],
        "differentials": diffs,
        "threats": sorted(threats, key=key)[:OWNERSHIP_ROWS],
        "my_captain_eo_pct": (None if my_captain is None else round(
            100 * own[my_captain].captain_eo, 1) if my_captain in own else 0.0),
    }


# ---------------------------------------------------------------------------
# Placing probabilities
# ---------------------------------------------------------------------------

#: A placing probability that could not be computed. Consumers must render this
#: as "unknown" — never as a number, and above all never as 100%.
BASIS_UNAVAILABLE = "unavailable"


@dataclass
class RivalGap:
    """The distribution of ``my score minus his``, for ONE named rival.

    3.1/3.2. Maximising expected points and maximising the chance of finishing
    above a particular person are different optimisation problems, and the
    difference is the whole reason a mini-league is not the overall game.
    Gaffer optimised the first and reported the second; this is the quantity
    the second is actually about.

    ``D = S_mine - S_rival`` is evaluated under the SHARED scenarios, so a goal
    that helps one of us is the same goal in the same simulated match. That
    matters more than it sounds: two independently drawn distributions would
    exaggerate the variance of the difference by roughly the amount the two
    squads have in common, which in a seven-person league is most of it.

    DOMAIN: next gameweek only. ``gap`` is the season points already banked and
    is added as a constant, so ``p_above`` is "will I be ahead of him after
    this gameweek" -- NOT "will I finish above him", which needs a model of the
    remaining season that does not exist. See `league.py`'s naming and the
    `domain` block published beside every figure.
    """

    #: The domain, stated ONCE for the whole set rather than repeated on every
    #: row. It is a property of the measurement, not of the rival, and a block
    #: of identical prose per row is both noise and 200 bytes of a capped
    #: response spent saying the same thing six times.
    DOMAIN = {
        "horizon": "next_gameweek",
        "measures": ("whether you are ahead of this manager once the next "
                     "gameweek has been played, not at the end of the season"),
        "basis": "shared fixture scenarios",
    }

    entry_id: int
    name: str
    #: Season points already banked, mine minus his, before this gameweek.
    gap: float
    #: Mean and spread of the DIFFERENCE, this gameweek only.
    mean: float
    std: float
    #: P(I am ahead of him after this gameweek).
    p_above: float
    #: Monte-Carlo 95% interval on `p_above` -- simulation error, not football.
    p_above_ci95: tuple[float, float]
    n_sims: int
    #: How many of his fifteen I also own. Shared players cancel in D.
    overlap: int
    #: True when his squad was never published, so the comparison is a
    #: distribution rather than a team.
    inferred: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "name": self.name,
            "points_gap": round(self.gap, 1),
            "difference_mean": round(self.mean, 2),
            "difference_std": round(self.std, 2),
            "p_above_after_gw": round(self.p_above, 4),
            "p_above_ci95": [round(self.p_above_ci95[0], 4),
                             round(self.p_above_ci95[1], 4)],
            "p_above_ci95_interval_type": "monte_carlo",
            "simulations": self.n_sims,
            "squad_overlap": self.overlap,
            "squad_inferred": self.inferred,
        }


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """A 95% interval on a simulated proportion.

    Wilson rather than the normal approximation: `p_above` is routinely near 0
    or 1 in a seven-person league, and the normal interval there runs outside
    [0, 1] and is narrow exactly where it should be widest.
    """
    if n <= 0:
        return (0.0, 1.0)
    phat = k / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rival_gaps(
    scen: Any, state: LeagueState, my_starting: list[int],
    my_captain: int | None, *, rng_seed: int = 11,
) -> list[RivalGap]:
    """``D = mine - his`` for every rival, under one shared set of scenarios.

    Sorted by how close the contest is -- smallest absolute expected margin
    first -- because the rival who is nearly level is the one a decision can
    actually move, and the one twenty points clear is not.
    """
    out: list[RivalGap] = []
    n = int(getattr(scen, "n_sims", 0) or 0)
    if n == 0 or not state.rivals:
        return out
    me = state.my_entry
    my_total = float(me.total) if me else 0.0
    mine = scen.squad_points(my_starting, captain=my_captain)
    my_set = set(my_starting)
    rng = np.random.default_rng(rng_seed)

    for r in state.rivals:
        if r.has_picks:
            theirs = scen.squad_points(r.starting, captain=r.captain)
            overlap = len(my_set & set(r.starting))
            inferred = False
        else:
            # No published squad. Carrying him at his current points would say
            # he scores nothing, which is a stronger claim than "we cannot see
            # his team". A distribution centred on our own mean is the honest
            # stand-in, and `inferred` says the row is one.
            theirs = np.asarray(mine).mean() + rng.normal(0.0, 12.0, n)
            overlap = 0
            inferred = True
        gap = my_total - (float(r.total) - r.hits)
        diff = np.asarray(mine) - np.asarray(theirs) + gap
        k = int((diff > 0).sum())
        out.append(RivalGap(
            entry_id=int(r.entry_id), name=str(r.manager or r.entry_name), gap=gap,
            mean=float(diff.mean()), std=float(diff.std()),
            p_above=k / n, p_above_ci95=_wilson(k, n), n_sims=n,
            overlap=overlap, inferred=inferred,
        ))
    out.sort(key=lambda g: abs(g.mean))
    return out


def differential_leverage(
    scen: Any, state: LeagueState, my_starting: list[int],
    my_captain: int | None, top: int = 8,
) -> list[dict[str, Any]]:
    """Which of my differentials actually move the contest, and which only differ.

    3.6. A differential is a player my rivals do not own. That says nothing
    about whether he creates SEPARATION: a nailed defender who returns two
    points every week differs from everyone's squad and moves nothing, while a
    forward with the same expected points and twice the spread decides weeks.
    Ownership is the definition; leverage is the question.

    The measure is his contribution to the standard deviation of the gap:

        leverage_i = cov(points_i, D) / std(D)

    which is the component of ``std(D)`` attributable to him, and which sums
    across the squad to ``std(D)`` exactly. It is in POINTS, so it reads
    directly: "he is worth 1.9 points of the spread between you and this
    league". Correlation is reported beside it because a player can carry
    spread and still be the wrong kind of spread -- one who moves with the
    rivals' own players, through a shared fixture, separates less than his
    variance suggests.

    Averaged over rivals with published squads. A rival whose team is unknown
    contributes no opinion rather than a guessed one.
    """
    n = int(getattr(scen, "n_sims", 0) or 0)
    known = [r for r in state.rivals if r.has_picks]
    if n == 0 or not known or not my_starting:
        return []
    mine = np.asarray(scen.squad_points(my_starting, captain=my_captain))
    owned_by_rivals: set[int] = set()
    for r in known:
        owned_by_rivals |= set(r.squad)

    diffs = [pid for pid in my_starting if pid not in owned_by_rivals]
    if not diffs:
        return []

    # One gap series per rival, then average the leverage across them: a
    # differential that separates from one rival and not another is a different
    # object from one that separates from nobody, and the spread across rivals
    # is what says which it is.
    gaps = []
    for r in known:
        theirs = np.asarray(scen.squad_points(r.starting, captain=r.captain))
        gaps.append(mine - theirs)

    out: list[dict[str, Any]] = []
    for pid in diffs:
        x = np.asarray(scen.row(pid), dtype=np.float64)
        mult = 2.0 if pid == my_captain else 1.0
        x = x * mult
        levs, cors = [], []
        for d in gaps:
            sd = float(np.std(d))
            if sd <= 1e-9:
                continue
            cov = float(np.cov(x, d, ddof=0)[0, 1])
            levs.append(cov / sd)
            sx = float(np.std(x))
            cors.append(cov / (sx * sd) if sx > 1e-9 else 0.0)
        if not levs:
            continue
        out.append({
            "player_id": int(pid),
            "leverage_points": round(float(np.mean(levs)), 3),
            "leverage_spread_across_rivals": round(float(np.std(levs)), 3),
            "correlation_with_gap": round(float(np.mean(cors)), 3),
            "own_std": round(float(np.std(x)), 3),
            "captained": pid == my_captain,
        })
    out.sort(key=lambda r: -abs(r["leverage_points"]))
    return out[:top]


@dataclass
class MoveEffect:
    """What one candidate move does to the contest with ONE named rival.

    3.3/3.4/3.11. Expected points is one axis and it is not the objective in a
    mini-league; this carries the others beside it so a trade is visible rather
    than implied.
    """

    entry_id: int
    name: str
    d_expected_points: float
    d_p_above: float
    d_p_above_ci95: tuple[float, float]
    d_variance_of_gap: float
    p_above_before: float
    p_above_after: float
    n_sims: int

    @property
    def resolved(self) -> bool:
        """False when the paired interval spans zero: the moves are tied.

        3.11. With 2,000 scenarios an ordinary probability carries about a
        point of simulation error, so a recommendation resting on 62.1% against
        63.0% is resting on noise. Saying "tied" is `certainty earned` as
        arithmetic rather than as a slogan.
        """
        lo, hi = self.d_p_above_ci95
        return not (lo <= 0.0 <= hi)

    @property
    def variance_reduction_per_point(self) -> float | None:
        """Variance of the gap removed per expected point given up.

        A DIAGNOSTIC, never the ranking. As a ranking it misbehaves exactly
        where covering decisions live: the ratio explodes as the sacrifice
        approaches zero, it is meaningless when the cover is also the better
        points pick (a negative sacrifice), and the largest variance reduction
        need not be the largest gain in the probability of finishing above him.
        The objective-aligned quantity is `d_p_above`, and that is what ranks.
        """
        sacrifice = -self.d_expected_points
        if sacrifice <= 0.05:
            return None
        return round((-self.d_variance_of_gap) / sacrifice, 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "name": self.name,
            "d_expected_points": round(self.d_expected_points, 3),
            "d_p_above": round(self.d_p_above, 4),
            "d_p_above_ci95": [round(self.d_p_above_ci95[0], 4),
                               round(self.d_p_above_ci95[1], 4)],
            "d_p_above_ci95_interval_type": "monte_carlo_paired",
            "resolved": self.resolved,
            "p_above_before": round(self.p_above_before, 4),
            "p_above_after": round(self.p_above_after, 4),
            "d_variance_of_gap": round(self.d_variance_of_gap, 3),
            "variance_reduction_per_point_given_up":
                self.variance_reduction_per_point,
            "simulations": self.n_sims,
        }


def move_effects(
    scen: Any, state: LeagueState,
    hold_starting: list[int], hold_captain: int | None,
    move_starting: list[int], move_captain: int | None,
    *, hit_cost: float = 0.0,
) -> list[MoveEffect]:
    """Per rival: what this move does to `P(I am ahead of him)`.

    PAIRED, and that is the point. The same scenario set scores the hold and
    the move, so the difference is taken WITHIN each simulated week and its
    interval comes from the distribution of those differences. Combining two
    marginal intervals instead would be far too wide -- most scenarios agree
    about who is ahead, and the disagreement is the whole signal -- and would
    report real edges as ties.
    """
    out: list[MoveEffect] = []
    n = int(getattr(scen, "n_sims", 0) or 0)
    if n == 0 or not state.rivals:
        return out
    me = state.my_entry
    my_total = float(me.total) if me else 0.0
    hold = np.asarray(scen.squad_points(hold_starting, captain=hold_captain))
    move = np.asarray(scen.squad_points(move_starting, captain=move_captain))
    move = move - float(hit_cost)

    for r in state.rivals:
        if not r.has_picks:
            continue
        theirs = np.asarray(scen.squad_points(r.starting, captain=r.captain))
        gap = my_total - (float(r.total) - r.hits)
        d_hold = hold - theirs + gap
        d_move = move - theirs + gap
        ahead_hold = (d_hold > 0).astype(np.float64)
        ahead_move = (d_move > 0).astype(np.float64)
        per_scenario = ahead_move - ahead_hold
        mean = float(per_scenario.mean())
        # Standard error of the PAIRED difference. Scenarios where both agree
        # contribute exactly zero, which is why this is tight.
        se = float(per_scenario.std(ddof=1) / math.sqrt(n)) if n > 1 else 0.0
        out.append(MoveEffect(
            entry_id=int(r.entry_id), name=str(r.manager or r.entry_name),
            d_expected_points=float(move.mean() - hold.mean()),
            d_p_above=mean,
            d_p_above_ci95=(mean - 1.96 * se, mean + 1.96 * se),
            d_variance_of_gap=float(d_move.var() - d_hold.var()),
            p_above_before=float(ahead_hold.mean()),
            p_above_after=float(ahead_move.mean()),
            n_sims=n,
        ))
    # 3.4 -- ranked on the OBJECTIVE-aligned quantity. The rival whose contest
    # this move moves most is the one worth naming first, and an unresolved
    # delta ranks below every resolved one however large its point estimate.
    out.sort(key=lambda e: (e.resolved, abs(e.d_p_above)), reverse=True)
    return out


@dataclass
class PlacingResult:
    p_first: float
    p_target: float
    target: int
    expected_position: float
    n_sims: int
    ci_halfwidth: float
    basis: str
    coverage_pct: float
    caveats: list[str] = field(default_factory=list)
    #: The gameweek these probabilities describe the standings AFTER.
    gameweek: int | None = None

    def as_dict(self) -> dict[str, Any]:
        # 1.2 -- SCOPE. These are next-GAMEWEEK quantities and were published as
        # `p_first`, `p_target` and `expected_position`, which read as
        # season-end. On 2026-09-01 the manager was leading his league and
        # `p_first` was 0.6225: the probability of still leading after GW3, not
        # of winning the league. Read as a season probability it argues for
        # taking far more risk than the number supports. The names now carry
        # the horizon, and `domain` states it in full.
        return {
            "p_first_after_gw": round(self.p_first, 4),
            "p_target_after_gw": round(self.p_target, 4),
            "target_position": self.target,
            "expected_position_after_gw": round(self.expected_position, 2),
            "domain": {
                "horizon": "next_gameweek",
                "gameweek": self.gameweek,
                "measures": (
                    "the standings immediately after gameweek "
                    f"{self.gameweek}, not the end of the season"
                    if self.gameweek is not None else
                    "the standings after the next gameweek, not the season"),
                "rivals_assumed": "rivals keep their current squads",
            },
            "simulations": self.n_sims,
            "ci95_halfwidth": round(self.ci_halfwidth, 4),
            "basis": self.basis,
            "available": self.basis != BASIS_UNAVAILABLE,
            "rival_coverage_pct": self.coverage_pct,
            "caveats": self.caveats,
        }


def placing_probabilities(
    scen: Any, state: LeagueState, my_starting: list[int],
    my_captain: int | None, *, target: int = 1,
    gameweeks_remaining: int = 1, rng_seed: int = 5,
    gameweek: int | None = None,
) -> PlacingResult:
    """P(finishing at or above ``target``) under shared football scenarios.

    The user and every rival are scored under the SAME simulated matches, so a
    goal that helps you is the same goal that helps them.

    Only rivals with revealed squads are simulated; the rest are carried at their
    current points with a widening uncertainty band. Coverage is reported so a
    thin sample cannot masquerade as a precise probability.
    """
    known = [r for r in state.rivals if r.has_picks]
    caveats: list[str] = []
    n = getattr(scen, "n_sims", 0)
    if n == 0:
        caveats.append("no scenarios available")
        return PlacingResult(0.0, 0.0, target, 0.0, 0, 1.0, BASIS_UNAVAILABLE,
                             round(100 * state.coverage, 1), caveats,
                             gameweek=gameweek)
    if not state.rivals:
        # Nobody to finish above or below. Pre-season the global leagues publish
        # no standings at all, and simulating an empty field returns "you finish
        # first in every scenario" — a 100% chance of winning the Overall league.
        # The honest answer is that there is no field yet, not that you have won.
        return PlacingResult(
            0.0, 0.0, target, 0.0, n, 1.0, BASIS_UNAVAILABLE,
            round(100 * state.coverage, 1),
            ["no rivals are published in this league yet, so there is no field "
             "to place against"],
            gameweek=gameweek,
        )

    me = state.my_entry
    my_base = float(me.total) if me else 0.0
    mine = scen.squad_points(my_starting, captain=my_captain) + my_base

    rng = np.random.default_rng(rng_seed)
    cols = [mine]
    for r in state.rivals:
        if r.has_picks:
            cols.append(scen.squad_points(r.starting, captain=r.captain)
                        + float(r.total) - r.hits)
        else:
            # Unknown squad: carry their points and widen with the horizon
            # rather than pretending they own nothing.
            spread = 12.0 * max(1, gameweeks_remaining) ** 0.5
            cols.append(float(r.total) + rng.normal(mine.mean() - my_base, spread, n))
    if len(state.rivals) > len(known):
        caveats.append(
            f"{len(state.rivals) - len(known)} of {len(state.rivals)} rival squads "
            "are unknown and were modelled as a distribution, not a team")
    if state.cohort_truncated:
        caveats.append(state.note or "compared against a bounded cohort")
    if gameweeks_remaining > 1:
        caveats.append(
            "Gaffer's multi-week mean projections are materially weaker than its "
            "one-week ones, so probabilities beyond the next gameweek are "
            "directional only")

    mat = np.vstack(cols)                       # (1 + rivals, n_sims)
    better = (mat[1:] > mat[0]).sum(axis=0)     # rivals finishing above me
    position = better + 1
    p_first = float((position == 1).mean())
    p_target = float((position <= target).mean())
    se = float(np.sqrt(max(p_target * (1 - p_target), 1e-12) / n))
    return PlacingResult(
        p_first=p_first, p_target=p_target, target=target,
        expected_position=float(position.mean()), n_sims=n,
        ci_halfwidth=1.96 * se, basis="shared fixture scenarios",
        coverage_pct=round(100 * state.coverage, 1), caveats=caveats,
        gameweek=gameweek,
    )


# ---------------------------------------------------------------------------
# Posture
# ---------------------------------------------------------------------------

@dataclass
class Posture:
    stance: str            # protect | neutral | chase | desperate
    reason: str
    variance_preference: float   # -1 minimise .. +1 maximise

    def as_dict(self) -> dict[str, Any]:
        return {"stance": self.stance, "reason": self.reason,
                "variance_preference": round(self.variance_preference, 3)}


def posture(
    *, points_gap: float, gameweeks_remaining: int, league_size: int,
    target: int = 1, coverage: float = 1.0,
) -> Posture:
    """Risk posture as a function of the situation, not a fixed dial.

    Trailing with little time left means variance is your friend; leading means
    it is your enemy. The old template/balanced/differential dial encoded neither.
    """
    if coverage < 0.34:
        return Posture("neutral",
                       "too little rival data to justify departing from expected "
                       "points", 0.0)
    horizon = max(1, gameweeks_remaining)
    # Roughly how many points a gameweek's variance can swing.
    swing = 18.0 * horizon ** 0.5
    z = points_gap / swing if swing else 0.0
    if z >= 1.0:
        return Posture("protect",
                       f"{points_gap:.0f} ahead with {horizon} GW(s) left "
                       f"({z:.1f} swings): reduce variance", -min(1.0, z / 2))
    if z <= -1.5:
        return Posture("desperate",
                       f"{abs(points_gap):.0f} behind with {horizon} GW(s) left: "
                       "only high variance closes this", 1.0)
    if z <= -0.4:
        return Posture("chase",
                       f"{abs(points_gap):.0f} behind with {horizon} GW(s) left: "
                       "take differentials", min(1.0, -z / 1.5))
    return Posture("neutral",
                   f"within {abs(points_gap):.0f} points with {horizon} GW(s) "
                   "left: expected points is the right objective", 0.0)


def points_gap_to_leader(state: LeagueState) -> float:
    me = state.my_entry
    if me is None or not state.entries:
        return 0.0
    best = max(e.total for e in state.entries)
    return float(me.total - best)
