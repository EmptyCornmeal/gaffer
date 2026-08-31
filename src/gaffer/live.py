"""Live gameweek state (T-22).

Nothing existed between deadlines. The proxy already exposed the live endpoint and
no code path called it, so during the only three hours a week when FPL is actually
interesting, Gaffer had nothing to say.

This module is pure: it takes the two public payloads (``event/{gw}/live/`` and
``fixtures/``) plus a squad, and returns a scored gameweek. No HTTP, no clock
beyond an injected ``now``, no database. That is what makes every match state
below testable from a recorded fixture rather than from a live Saturday.

Three separations are load-bearing and are never collapsed:

``confirmed``    points FPL has awarded and will not revise
``provisional``  bonus computed from live BPS, which moves until the match ends
``predicted``    projected points for players who have not kicked a ball yet

Presenting provisional bonus as confirmed is the single most common way a live
FPL tool lies to you, so the three travel separately all the way to the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from gaffer import config

LIVE_VERSION = "live-1.0"

# --- match states ----------------------------------------------------------
STATE_SCHEDULED = "scheduled"        # not kicked off
STATE_LIVE = "live"                  # in play
STATE_HALF_TIME = "half_time"        # in play, clock parked on 45
STATE_AWAITING_BONUS = "awaiting_bonus"   # 90 played, bonus not yet final
STATE_FINISHED = "finished"          # played and fully processed
STATE_POSTPONED = "postponed"        # no date, or removed from the event
STATE_ABANDONED = "abandoned"        # started long ago, never completed
ALL_STATES = frozenset({
    STATE_SCHEDULED, STATE_LIVE, STATE_HALF_TIME, STATE_AWAITING_BONUS,
    STATE_FINISHED, STATE_POSTPONED, STATE_ABANDONED,
})

#: A started fixture still incomplete this long after kick-off is not "live".
#: 90 minutes of football plus a half-time break plus generous stoppage/VAR.
ABANDON_AFTER = timedelta(hours=4)

#: FPL's bonus ladder. Ties share the higher award and consume the ranks below.
BONUS_LADDER = (3, 2, 1)

# --- squad rules used by autosubs ------------------------------------------
POS_MIN = dict(config.FORMATION_MIN)          # GKP 1, DEF 3, MID 2, FWD 1
POS_MAX = {"GKP": 1, "DEF": 5, "MID": 5, "FWD": 3}


def parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return None if dt.tzinfo is None else dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Fixture state
# ---------------------------------------------------------------------------

@dataclass
class FixtureState:
    id: int
    event: int | None
    team_h: int
    team_a: int
    state: str
    minutes: int
    kickoff: datetime | None
    started: bool
    finished: bool
    finished_provisional: bool

    @property
    def bonus_final(self) -> bool:
        """True once this match's bonus is settled and inside ``total_points``.

        Not ``finished`` alone. A1: FPL flips a fixture's ``finished`` only when
        the WHOLE event is processed, so the flag is per-gameweek wearing a
        per-fixture name. Read live on 2026-08-31: GW1's ten fixtures were all
        ``(finished=True, finished_provisional=True)``, while GW2's nine played
        fixtures were all ``(finished=False, finished_provisional=True)`` three
        days after they were played, held there by one straggler still to come.

        So matches sat in AWAITING_BONUS for days and ``provisional_bonus`` kept
        computing a BPS award for bonus FPL had settled long before and had
        already folded into the live row. ``finished_provisional`` is the flag
        that is actually per-fixture: it says this match is done and its bonus is
        decided, whatever the rest of the gameweek is still doing.
        """
        return bool(self.finished or self.finished_provisional)

    @property
    def in_play(self) -> bool:
        return self.state in (STATE_LIVE, STATE_HALF_TIME)

    @property
    def counts_as_played(self) -> bool:
        """Has this fixture reached a point where a 0-minute player is out?

        Only once the match is over. Mid-match a benched player may still come on,
        so autosubbing him would be guessing.

        A postponed fixture counts too. It is over in the only sense this
        question asks about: it will not be played inside this gameweek, so a
        player left in it has blanked and FPL substitutes him. Leaving postponed
        out kept those players permanently mid-match, which is why a postponed
        captain never handed the armband to the vice.
        """
        return self.state in (STATE_AWAITING_BONUS, STATE_FINISHED,
                              STATE_POSTPONED)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "event": self.event, "team_h": self.team_h,
                "team_a": self.team_a, "state": self.state,
                "minutes": self.minutes,
                "kickoff": self.kickoff.isoformat() if self.kickoff else None,
                "bonus_final": self.bonus_final}


def classify_fixture(raw: dict[str, Any], now: datetime, gw: int | None = None
                     ) -> FixtureState:
    """Derive a single, unambiguous state from FPL's four overlapping flags.

    FPL exposes ``started`` / ``finished`` / ``finished_provisional`` / ``minutes``
    and they disagree in edge cases: a finished match has bonus pending, a
    postponed one loses its ``event``, an abandoned one stays "started" forever.
    Collapsing them here means the UI never has to reason about the combination.
    """
    event = raw.get("event")
    ko = parse_time(raw.get("kickoff_time"))
    started = bool(raw.get("started"))
    finished = bool(raw.get("finished"))
    prov = bool(raw.get("finished_provisional"))
    minutes = int(raw.get("minutes") or 0)

    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)

    if event is None or ko is None:
        state = STATE_POSTPONED
    elif finished:
        state = STATE_FINISHED
    elif prov or (started and minutes >= 90):
        state = STATE_AWAITING_BONUS
    elif started:
        if ko is not None and now - ko > ABANDON_AFTER:
            state = STATE_ABANDONED
        elif minutes == 45:
            state = STATE_HALF_TIME
        else:
            state = STATE_LIVE
    else:
        state = STATE_SCHEDULED

    return FixtureState(
        id=int(raw.get("id", 0)), event=event,
        team_h=int(raw.get("team_h") or 0), team_a=int(raw.get("team_a") or 0),
        state=state, minutes=minutes, kickoff=ko, started=started,
        finished=finished, finished_provisional=prov,
    )


def fixture_states(fixtures: list[dict[str, Any]], gw: int, now: datetime
                   ) -> dict[int, FixtureState]:
    """Every fixture belonging to ``gw``, keyed by fixture id."""
    return {
        int(f.get("id", 0)): classify_fixture(f, now, gw)
        for f in fixtures or [] if f.get("event") == gw
    }


# ---------------------------------------------------------------------------
# Provisional bonus
# ---------------------------------------------------------------------------

def bonus_from_bps(bps: dict[int, int]) -> dict[int, int]:
    """Official BPS -> bonus allocation, ties included.

    The rule is not "top three get 3/2/1". Ties share the higher award and
    *consume* the places below them:

      * two tied on top   -> both get 3, the next player gets 1 (no 2 awarded)
      * three tied on top -> all get 3, nothing else is awarded
      * tie for second    -> both get 2, no 1 awarded
      * tie for third     -> both get 1

    Getting this wrong systematically over-credits whoever is second, which is
    exactly the player a live tool is most often asked about.
    """
    if not bps:
        return {}
    groups: dict[int, list[int]] = {}
    for pid, score in bps.items():
        groups.setdefault(int(score), []).append(pid)

    out: dict[int, int] = {}
    rank = 0                       # index into BONUS_LADDER
    for score in sorted(groups, reverse=True):
        if rank >= len(BONUS_LADDER):
            break
        award = BONUS_LADDER[rank]
        tied = groups[score]
        for pid in tied:
            out[pid] = award
        rank += len(tied)          # a tie of N consumes N places
    return out


def fixture_bps(raw: dict[str, Any]) -> dict[int, int]:
    """Extract per-player BPS from a fixture's ``stats`` block."""
    for block in raw.get("stats") or []:
        if block.get("identifier") != "bps":
            continue
        out: dict[int, int] = {}
        for side in ("h", "a"):
            for row in block.get(side) or []:
                pid = row.get("element")
                if isinstance(pid, int):
                    out[pid] = int(row.get("value") or 0)
        return out
    return {}


def provisional_bonus(
    fixtures: list[dict[str, Any]], states: dict[int, FixtureState],
) -> dict[int, int]:
    """Bonus that FPL has NOT yet awarded, computed from live BPS.

    Skips fixtures whose bonus is already final — those points are in the live
    endpoint's ``total_points`` and adding ours would double-count them.
    """
    out: dict[int, int] = {}
    for raw in fixtures or []:
        st = states.get(int(raw.get("id", 0)))
        if st is None or st.bonus_final or not st.started:
            continue
        for pid, award in bonus_from_bps(fixture_bps(raw)).items():
            out[pid] = out.get(pid, 0) + award
    return out


# ---------------------------------------------------------------------------
# Per-player live state
# ---------------------------------------------------------------------------

@dataclass
class PlayerLive:
    id: int
    minutes: int = 0
    confirmed: int = 0            # points FPL has awarded
    provisional: int = 0          # bonus we computed, not yet awarded
    predicted: float = 0.0        # projection for a player yet to play
    played: bool = False
    finished: bool = False        # every fixture of his this GW is over
    yet_to_play: bool = False
    states: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        """Everything, clearly the sum of three separately-reported parts."""
        return self.confirmed + self.provisional + self.predicted

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "minutes": self.minutes, "confirmed": self.confirmed,
            "provisional": self.provisional, "predicted": round(self.predicted, 2),
            "total": round(self.total, 2), "played": self.played,
            "finished": self.finished, "yet_to_play": self.yet_to_play,
            "fixture_states": self.states,
        }


def remaining_fixtures(fx: list[FixtureState], minutes: int) -> list[FixtureState]:
    """The fixtures in ``fx`` that can still deliver points to this player.

    Both of the numbers a caller has are gameweek aggregates: one projection for
    the week and one minutes figure for the week. So "has he played yet" cannot
    be answered from the player at all in a double gameweek — only the calendar
    knows, and it is asked here rather than inferred from his minutes.

    A fixture that has not kicked off is always still to come. One that is under
    way counts only while he has no minutes anywhere: with an aggregate we cannot
    tell which of his matches those minutes came from, and a player already on
    the pitch is scoring into ``confirmed`` rather than into a projection.
    """
    pending = [f for f in fx if not f.counts_as_played]
    if minutes == 0:
        return pending
    return [f for f in pending if f.state == STATE_SCHEDULED]


def remaining_xp(xp: float, remaining: int, total: int) -> float:
    """The share of a gameweek projection that is still to be played.

    The projection is one number for the whole gameweek, so in a double it
    already covers both matches. An even split across the club's fixtures is the
    only division the data supports — neither the artifact nor the projections
    table carries a per-fixture breakdown — and it is right in both directions
    that matter: a player with one of two still to come is worth about half of
    it rather than nothing, and one who was left out of the first of two is
    worth about half of it rather than all of it.
    """
    if total <= 0 or remaining <= 0:
        return 0.0
    return float(xp) * remaining / total


def player_live(
    live: dict[str, Any], states: dict[int, FixtureState],
    prov_bonus: dict[int, int], team_of: dict[int, int],
    predictions: dict[int, float] | None = None,
) -> dict[int, PlayerLive]:
    """Fold the live endpoint into per-player state.

    A player is ``yet_to_play`` when one of his fixtures this gameweek can still
    deliver points — which is *not* the same as "has zero minutes". Confusing the
    two is what makes a live tool declare a substitution before the second half
    has kicked off.

    ``finished`` is the mirror image: every fixture of his is over, *including*
    the gameweek in which his club has no fixture at all. A blank is not a
    gameweek that never ends, and a player in one has to become substitutable —
    with ``bool(fx) and ...`` he never did.
    """
    predictions = predictions or {}
    per_fixture: dict[int, list[FixtureState]] = {}
    for st in states.values():
        per_fixture.setdefault(st.team_h, []).append(st)
        per_fixture.setdefault(st.team_a, []).append(st)

    out: dict[int, PlayerLive] = {}
    for el in (live or {}).get("elements") or []:
        pid = el.get("id")
        if not isinstance(pid, int):
            continue
        stats = el.get("stats") or {}
        mins = int(stats.get("minutes") or 0)
        pts = int(stats.get("total_points") or 0)
        # FPL's live ``total_points`` already contains whatever bonus the element
        # row is carrying, and mid-match that includes *provisional* bonus. Our
        # BPS-derived award is then a second copy of the same points, and the
        # armband doubles the error: a captained Haaland on 8 (1 appearance +
        # 4 goal + 3 bonus) shipped as (8 + 3) x 2 = 22 against a true 16.
        # The per-fixture ``bonus_final`` skip in ``provisional_bonus`` only
        # covers *finished* matches, so it cannot see this case at all.
        # Trust the row over our own arithmetic, per player: where FPL has
        # published a bonus figure it is authoritative, and where it has not we
        # still supply one. Reading the row also makes the fix correct whether or
        # not FPL populates this field mid-match, which is not ours to assume.
        awarded_bonus = int(stats.get("bonus") or 0)
        fx = per_fixture.get(team_of.get(pid, -1), [])
        remaining = remaining_fixtures(fx, mins)
        out[pid] = PlayerLive(
            id=pid, minutes=mins, confirmed=pts,
            provisional=0 if awarded_bonus else int(prov_bonus.get(pid, 0)),
            predicted=remaining_xp(predictions.get(pid, 0.0), len(remaining),
                                   len(fx)),
            played=mins > 0,
            finished=all(f.counts_as_played for f in fx),
            yet_to_play=bool(remaining),
            states=sorted({f.state for f in fx}),
        )

    # Players with no live row yet: the endpoint lags kick-off, and it has no row
    # at all for a club that is not playing. Both are zero-minute players, so both
    # are read from the calendar alone.
    for pid, team in team_of.items():
        if pid in out:
            continue
        fx = per_fixture.get(team, [])
        remaining = remaining_fixtures(fx, 0)
        out[pid] = PlayerLive(
            id=pid,
            predicted=remaining_xp(predictions.get(pid, 0.0), len(remaining),
                                   len(fx)),
            finished=all(f.counts_as_played for f in fx),
            yet_to_play=bool(remaining), states=sorted({f.state for f in fx}))
    return out


# ---------------------------------------------------------------------------
# Autosubs
# ---------------------------------------------------------------------------

@dataclass
class Autosubs:
    xi: list[int]
    bench: list[int]
    subs_in: list[int] = field(default_factory=list)
    subs_out: list[int] = field(default_factory=list)
    captain: int | None = None
    captain_source: str = "captain"     # captain | vice | none
    multiplier: int = 2
    provisional: bool = True
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "xi": self.xi, "bench": self.bench, "subs_in": self.subs_in,
            "subs_out": self.subs_out, "captain": self.captain,
            "captain_source": self.captain_source, "multiplier": self.multiplier,
            "provisional": self.provisional, "notes": self.notes,
        }


def _legal(counts: dict[str, int]) -> bool:
    return all(counts.get(p, 0) >= lo for p, lo in POS_MIN.items()) and \
        all(counts.get(p, 0) <= hi for p, hi in POS_MAX.items())


def apply_autosubs(
    starting: list[int], bench: list[int], positions: dict[int, str],
    live: dict[int, PlayerLive], *, captain: int | None = None,
    vice: int | None = None, bench_boost: bool = False,
    triple_captain: bool = False,
) -> Autosubs:
    """FPL's substitution rules, applied to the live state.

    The rules, in the order FPL applies them:

      1. A starter is only replaced once **all** his fixtures are over and he has
         zero minutes. A player yet to kick off is not "out".
      2. The goalkeeper can only be replaced by the bench goalkeeper.
      3. Outfield subs are tried in bench order, and each is only made if the
         **resulting** XI is still a legal formation.
      4. A bench player who did not play cannot come on.
      5. Bench Boost plays all fifteen, so no substitutions happen at all.
      6. If the captain records zero minutes once his match is over, the armband
         passes to the vice. If the vice also blanks, nobody is multiplied.
    """
    xi = list(starting)
    bench = list(bench)
    notes: list[str] = []

    if bench_boost:
        notes.append("Bench Boost is active: all 15 score and no substitutions "
                     "are made.")
        cap, src, mult = _armband(captain, vice, live, triple_captain)
        return Autosubs(xi=xi, bench=bench, captain=cap, captain_source=src,
                        multiplier=mult, provisional=_any_unfinished(live, xi + bench),
                        notes=notes)

    def out(pid: int) -> bool:
        st = live.get(pid)
        return bool(st and st.finished and st.minutes == 0)

    def came_on(pid: int) -> bool:
        st = live.get(pid)
        return bool(st and st.minutes > 0)

    subs_in: list[int] = []
    subs_out: list[int] = []

    # --- goalkeeper: like for like only -----------------------------------
    gk_start = [p for p in xi if positions.get(p) == "GKP"]
    gk_bench = [p for p in bench if positions.get(p) == "GKP"]
    for gk in gk_start:
        if out(gk) and gk_bench and came_on(gk_bench[0]):
            replacement = gk_bench[0]
            xi[xi.index(gk)] = replacement
            bench[bench.index(replacement)] = gk
            subs_in.append(replacement)
            subs_out.append(gk)
            notes.append("Goalkeeper substitution: only the bench keeper is "
                         "eligible.")

    # --- outfield: bench order, formation must stay legal -------------------
    for slot in [p for p in bench if positions.get(p) != "GKP"]:
        blanks = [p for p in xi if out(p) and positions.get(p) != "GKP"]
        if not blanks:
            break
        if not came_on(slot):
            continue
        for blank in blanks:
            trial = [slot if p == blank else p for p in xi]
            counts: dict[str, int] = {}
            for p in trial:
                counts[positions.get(p, "?")] = counts.get(positions.get(p, "?"), 0) + 1
            if _legal(counts):
                xi = trial
                bench[bench.index(slot)] = blank
                subs_in.append(slot)
                subs_out.append(blank)
                break
        else:
            notes.append(
                f"Bench player {slot} could not come on: no substitution keeps "
                "the formation legal.")

    if not subs_in and any(out(p) for p in starting):
        notes.append("A starter blanked but no legal replacement played.")

    cap, src, mult = _armband(captain, vice, live, triple_captain)
    if src == "vice":
        notes.append("Captain recorded no minutes, so the armband passed to the "
                     "vice-captain.")
    elif src == "none":
        notes.append("Captain and vice both recorded no minutes: no player is "
                     "multiplied this gameweek.")

    return Autosubs(
        xi=xi, bench=bench, subs_in=subs_in, subs_out=subs_out, captain=cap,
        captain_source=src, multiplier=mult,
        provisional=_any_unfinished(live, xi + bench), notes=notes)


def _any_unfinished(live: dict[int, PlayerLive], ids: list[int]) -> bool:
    """Substitutions stay projected until every relevant fixture is over."""
    return any(not (live.get(p) and live[p].finished) for p in ids)


def _armband(
    captain: int | None, vice: int | None, live: dict[int, PlayerLive],
    triple_captain: bool = False,
) -> tuple[int | None, str, int]:
    """Who wears the armband, and what it is worth.

    The multiplier is decided here and nowhere else. It used to be recomputed in
    ``score_squad`` and left at 2 on the ``Autosubs`` record, so the league-swing
    arithmetic — which reads it from there — understated a Triple Captain week by
    a third.
    """
    mult = 3 if triple_captain else 2

    def blanked(pid: int | None) -> bool:
        st = live.get(pid) if pid is not None else None
        return bool(st and st.finished and st.minutes == 0)

    if captain is not None and not blanked(captain):
        return captain, "captain", mult
    if vice is not None and not blanked(vice):
        return vice, "vice", mult
    return None, "none", 1


def entry_baseline_and_hits(
    history: dict[str, Any] | None, gw: int,
) -> tuple[int, int]:
    """Season points carried INTO ``gw``, and the transfer cost paid FOR it.

    ``summary_overall_points`` cannot supply the baseline: once the gameweek
    starts scoring it already contains the points this view is computing, so a
    live total built on it counts them twice. The cumulative ``total_points`` at
    the previous event is the only figure that is exactly "before this gameweek",
    and it is already net of earlier hits.

    Hits were previously hardcoded to zero, which made a -8 week read four points
    better than it was.
    """
    rows = [r for r in ((history or {}).get("current") or [])
            if isinstance(r, dict) and isinstance(r.get("event"), int)]
    prior = [r for r in rows if r["event"] < gw]
    baseline = 0
    if prior:
        last = max(prior, key=lambda r: r["event"])
        total = last.get("total_points")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            baseline = int(total)
        else:  # no cumulative column: rebuild it from the per-gameweek rows
            baseline = sum(
                int(r.get("points") or 0) - int(r.get("event_transfers_cost") or 0)
                for r in prior)
    this = next((r for r in rows if r["event"] == gw), None)
    hits = int((this or {}).get("event_transfers_cost") or 0)
    return baseline, hits


# ---------------------------------------------------------------------------
# Squad scoring
# ---------------------------------------------------------------------------

@dataclass
class SquadLive:
    entry_id: int | None
    confirmed: int
    provisional: int
    predicted: float
    bench_points: int
    players_played: int
    players_yet_to_play: int
    autosubs: Autosubs
    baseline: int = 0            # points carried from earlier gameweeks
    hits: int = 0
    #: Every player whose points actually land in this total. Under Bench Boost
    #: that is all fifteen, which is exactly why it exists: ``autosubs.xi`` is
    #: eleven names whatever the chip says, so anything reading it to answer
    #: "who is scoring for this manager" is wrong in the one week a bench is
    #: the whole point. Recorded once, by the scorer that already knows.
    scoring: tuple[int, ...] = ()

    @property
    def current(self) -> float:
        """What the score says right now, bonus included but flagged as such."""
        return self.confirmed + self.provisional - self.hits

    @property
    def projected(self) -> float:
        return self.current + self.predicted

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "confirmed": self.confirmed,
            "provisional_bonus": self.provisional,
            "predicted_remaining": round(self.predicted, 2),
            "current": round(self.current, 2),
            "projected": round(self.projected, 2),
            "bench_points": self.bench_points,
            "players_played": self.players_played,
            "players_yet_to_play": self.players_yet_to_play,
            "hits": self.hits,
            "season_total_before": self.baseline,
            "season_total_projected": round(self.baseline + self.projected, 2),
            "autosubs": self.autosubs.as_dict(),
        }


def score_squad(
    starting: list[int], bench: list[int], positions: dict[int, str],
    live: dict[int, PlayerLive], *, captain: int | None = None,
    vice: int | None = None, bench_boost: bool = False,
    triple_captain: bool = False, entry_id: int | None = None,
    baseline: int = 0, hits: int = 0,
) -> SquadLive:
    """Score one entry from the live state. Used for the user AND every rival.

    Both sides must be passed the *same* ``live`` mapping — scoring rivals from a
    second fetch would let the two disagree about a goal that had just gone in.
    """
    subs = apply_autosubs(starting, bench, positions, live, captain=captain,
                          vice=vice, bench_boost=bench_boost,
                          triple_captain=triple_captain)
    mult = subs.multiplier

    scoring = list(subs.xi) + (list(subs.bench) if bench_boost else [])
    confirmed = provisional = 0
    predicted = 0.0
    for pid in scoring:
        st = live.get(pid)
        if st is None:
            continue
        m = mult if pid == subs.captain else 1
        confirmed += st.confirmed * m
        provisional += st.provisional * m
        predicted += st.predicted * m

    bench_pts = 0
    if not bench_boost:
        for pid in subs.bench:
            st = live.get(pid)
            if st is not None:
                bench_pts += st.confirmed + st.provisional

    relevant = [live.get(p) for p in list(subs.xi) + list(subs.bench)]
    return SquadLive(
        entry_id=entry_id, confirmed=confirmed, provisional=provisional,
        predicted=predicted, bench_points=bench_pts,
        players_played=sum(1 for s in relevant if s and s.played),
        players_yet_to_play=sum(1 for s in relevant if s and s.yet_to_play),
        autosubs=subs, baseline=baseline, hits=hits, scoring=tuple(scoring),
    )


# ---------------------------------------------------------------------------
# Rivals: effective ownership, published rows
# ---------------------------------------------------------------------------

def effective_multiplier(pid: int, squad: SquadLive) -> int:
    """How many copies of ``pid``'s points land in ``squad``'s total.

    Zero when he does not score for this manager at all — benched without Bench
    Boost, or simply not owned. One when he scores once. The armband multiplier
    when he is the captain, which is 3 under Triple Captain and 1 when captain
    and vice both blanked.

    This is *effective* ownership, and it is the only quantity that matters when
    two managers are compared: owning a player is one way to differ, and both
    owning him while only one of you captained him is the other, commoner one.

    Lifted out of ``largest_swing``, where it lived as a closure, so the
    published rival rows multiply by the same rule the swing measures with.
    Restating it would have been a second reading of one rulebook, which is the
    exact class of bug this project keeps finding.
    """
    if pid not in set(squad.scoring):
        return 0
    return squad.autosubs.multiplier if pid == squad.autosubs.captain else 1


def differential_ids(mine: SquadLive, theirs: SquadLive) -> list[int]:
    """The players that separate these two managers, ascending.

    A player who scores identically for both of you cannot move the gap between
    you however many points he takes, so he is not here. Everyone else is,
    whichever squad he is in: the list spans BOTH sides, so it contains players
    of yours the rival does not have as well as his that you do not.

    Published as bare ids because both squads are already published in full —
    yours in ``players``, his in his own ``rival_squads`` entry — so this is a
    pointer into data the artifact carries, not a third copy of it. Six rivals
    cost about 350 bytes. One caveat follows from that: an id here has no row
    when neither side can publish one — a player this build has no live state
    for at all — because he still separates you, and silently dropping him would
    understate the difference rather than report a gap.
    """
    ids = set(mine.scoring) | set(theirs.scoring)
    return [p for p in sorted(ids)
            if effective_multiplier(p, mine) != effective_multiplier(p, theirs)]


def rival_player_row(
    pid: int, squad: SquadLive, live: dict[int, PlayerLive],
    names: dict[int, str], positions: dict[int, str],
) -> dict[str, Any] | None:
    """One published row of a rival's fifteen, or None if he has no live state.

    Deliberately NOT ``PlayerLive.as_dict()`` with a name bolted on, which is
    what the manager's own ``players`` block publishes. Seven rivals times
    fifteen is 105 rows in an artifact the browser re-reads whenever the live
    proxy cannot answer, and measured against real GW2 data the full-fat row
    costs 258 bytes against this one's 151 — 23.2 kB of rival rows against
    13.6 kB, on a file that is 7.2 kB without them. ``played``, ``finished`` and
    ``fixture_states`` are the fields dropped: all three are properties of the
    FOOTBALL rather than of the manager, identical for every rival who owns the
    player, and none of them is needed to answer either question this block
    exists for — what is his score, and who has not kicked off yet.

    What is kept is what cannot be recovered from anywhere else: the three kinds
    of points, which never merge (see this module's header); the multiplier,
    which is a fact about this manager and not about the player; and the
    product, which is what that player actually contributes.

    ``product`` is ``(confirmed + provisional) * multiplier`` — the contribution
    to the manager's CURRENT score, so ``sum(product) - hits == gw_points``
    exactly, and a rival's total arrives checkable in the same way the manager's
    own does. ``predicted`` is left out of it on purpose: it is a projection,
    and folding a projection into a scoreline is the thing this module exists
    not to do — which does mean ``predicted`` is this player's OWN share and has
    to be multiplied by ``multiplier`` to reach his contribution to the
    manager's projected total. The scoreline and the projection are two
    different sums and they are kept looking like it.
    """
    st = live.get(pid)
    if st is None:
        return None
    mult = effective_multiplier(pid, squad)
    return {
        "element": pid,
        "name": names.get(pid, str(pid)),
        "pos": positions.get(pid),
        "minutes": st.minutes,
        "confirmed": st.confirmed,
        "provisional": st.provisional,
        "predicted": round(st.predicted, 2),
        "multiplier": mult,
        "product": round((st.confirmed + st.provisional) * mult, 2),
        "yet_to_play": st.yet_to_play,
    }


def rival_squad_dict(
    squad: SquadLive, mine: SquadLive, live: dict[int, PlayerLive],
    names: dict[int, str], positions: dict[int, str], *,
    name: str, place: int,
) -> dict[str, Any]:
    """One rival, scored, with the fifteen rows his total is made of.

    All fifteen, not the scoring eleven. The eleven would have been 2.8 kB
    cheaper and the arithmetic would still have closed — a benched player
    carries ``multiplier`` 0 and contributes nothing — but "how are they doing"
    is asked alongside "which of their players have not kicked off", and a bench
    player who is yet to play is one of them: his starter blanking is exactly
    how he comes on. Publishing eleven would also have left this block's row
    count disagreeing with ``rivals[].yet_to_play``, which counts all fifteen.
    Under Bench Boost the same fifteen rows simply all carry a multiplier,
    because ``SquadLive.scoring`` already knows, so the chip needs no case here.

    The autosub block is ``Autosubs.as_dict()`` verbatim rather than a chosen
    subset of it: a subset is a second shape of the same thing and drifts. It
    costs about 220 bytes a rival and it is what says whether the substitutions
    below are settled or still projected.
    """
    return {
        "entry_id": squad.entry_id,
        "name": name,
        "provisional_position": place,
        "gw_points": round(squad.current, 2),
        "hits": squad.hits,
        "yet_to_play": squad.players_yet_to_play,
        "autosubs": squad.autosubs.as_dict(),
        "players": [
            row for row in (
                rival_player_row(p, squad, live, names, positions)
                for p in list(squad.autosubs.xi) + list(squad.autosubs.bench))
            if row is not None
        ],
        "differential": differential_ids(mine, squad),
    }


# ---------------------------------------------------------------------------
# League swing
# ---------------------------------------------------------------------------

def largest_swing(
    mine: SquadLive, rivals: list[SquadLive], live: dict[int, PlayerLive],
    names: dict[int, str] | None = None,
) -> dict[str, Any] | None:
    """Which live player has moved the league most, and by how much.

    What moves a mini-league is not ownership but *effective* ownership: how many
    copies of a player's points land in your total against how many land in your
    rival's. Owning him is only one way to differ. You both own him and only one
    of you captained him is the other, and it is the commoner of the two — the
    ownership-only version of this returned "no swing" for the single most
    ordinary way a week turns.

    Measured against the closest rival, so it answers "what is deciding my week",
    not "who scored the most points".

    C21. The candidates are each manager's *scoring* set, not his XI. Under
    Bench Boost all fifteen score, so reading ``autosubs.xi`` here — eleven
    names whatever the chip says — made a rival's bench invisible to this
    function in the one week his bench decides the league. The two managers are
    read independently: only one of them may have played the chip.
    """
    names = names or {}
    if not rivals:
        return None
    closest = min(
        rivals,
        key=lambda r: abs((r.baseline + r.current) - (mine.baseline + mine.current)))
    my_ids = set(mine.scoring)
    their_ids = set(closest.scoring)
    best_pid, best_delta = None, 0.0
    # Iterated in ascending id, so an exact tie resolves to the lowest id in both
    # implementations. Set iteration order would not: CPython lays small ints out
    # by value modulo the table size, so `{9, 40} ^ set()` comes out [40, 9]
    # while the TypeScript port, which sorts, would answer 9.
    for pid in sorted(my_ids | their_ids):
        st = live.get(pid)
        if st is None or not (st.confirmed or st.provisional):
            continue
        edge = effective_multiplier(pid, mine) - effective_multiplier(pid, closest)
        if edge == 0:                     # he scores identically for both of you
            continue
        delta = (st.confirmed + st.provisional) * edge
        if abs(delta) > abs(best_delta):
            best_pid, best_delta = pid, delta
    if best_pid is None:
        return None
    shared = best_pid in my_ids and best_pid in their_ids
    return {
        "player_id": best_pid,
        "name": names.get(best_pid, str(best_pid)),
        "swing": round(best_delta, 2),
        # Named for the ordinary case, and it keeps that name because it is in
        # the published artifact. It means "he is scoring for you", which under
        # Bench Boost includes your bench.
        "in_your_xi": best_pid in my_ids,
        "against": closest.entry_id,
        "note": ("a player you both own but captain differently" if shared
                 else "a differential you own" if best_pid in my_ids
                 else "a differential your closest rival owns"),
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

# Reasons a live view legitimately has nothing to show. These are states, not
# errors: the UI must render them as such rather than as an empty scoreboard.
UNAVAILABLE_NO_GAMEWEEK = "no_gameweek"
UNAVAILABLE_NOT_STARTED = "not_started"
UNAVAILABLE_NO_SQUAD = "no_squad"
UNAVAILABLE_NO_LIVE_DATA = "no_live_data"


def assemble(
    *, gw: int, live_payload: dict[str, Any] | None,
    fixtures_payload: list[dict[str, Any]] | None,
    squad: dict[str, Any] | None, positions: dict[int, str],
    team_of: dict[int, int], now: datetime,
    predictions: dict[int, float] | None = None,
    rivals: list[dict[str, Any]] | None = None,
    names: dict[int, str] | None = None,
    entry_id: int | None = None, baseline: int = 0, hits: int = 0,
    active_chip: str | None = None, as_of: str | None = None,
) -> dict[str, Any]:
    """Build the whole live view from already-fetched payloads.

    Deliberately pure — every argument is data. The caller does the I/O, so every
    match state below is reachable from a recorded fixture instead of requiring a
    live Saturday afternoon.
    """
    names = names or {}
    rivals = rivals or []
    # A5. A manager is a member of his own mini-league, so the rivals list he
    # arrives with contains him. The table below prepends a synthetic "You" row,
    # which listed him twice — and the duplicate is a rival at distance ZERO from
    # himself, so ``largest_swing`` chose it as the closest, found ``edge == 0``
    # for every player because both squads were his, and returned None on every
    # single run. Filtered here rather than only in the callers: ``_live_rivals``
    # in pipeline.py and ``gatherRivals`` in web/src/lib/live/source.ts both drop
    # him too, and this is what stops a third caller quietly reintroducing it.
    if entry_id is not None:
        rivals = [r for r in rivals if r.get("entry_id") != entry_id]
    states = fixture_states(fixtures_payload or [], gw, now)
    base = {
        "live_version": LIVE_VERSION,
        "gameweek": gw,
        "as_of": as_of,
        "fixtures": [s.as_dict() for s in sorted(states.values(), key=lambda s: s.id)],
        "fixture_summary": _fixture_summary(states),
    }

    if not states:
        return {**base, "available": False, "unavailable_reason": UNAVAILABLE_NO_GAMEWEEK,
                "note": f"no fixtures are scheduled for gameweek {gw}"}
    if not any(s.started for s in states.values()):
        return {**base, "available": False, "unavailable_reason": UNAVAILABLE_NOT_STARTED,
                "note": "no match in this gameweek has kicked off yet"}
    if squad is None or not squad.get("starting"):
        return {**base, "available": False, "unavailable_reason": UNAVAILABLE_NO_SQUAD,
                "note": "your squad is not readable, so there is nothing to score"}
    if not (live_payload or {}).get("elements"):
        return {**base, "available": False, "unavailable_reason": UNAVAILABLE_NO_LIVE_DATA,
                "note": "matches have started but the live endpoint is not "
                        "serving player data yet"}

    prov = provisional_bonus(fixtures_payload or [], states)
    pl = player_live(live_payload or {}, states, prov, team_of, predictions)

    mine = score_squad(
        squad["starting"], squad.get("bench", []), positions, pl,
        captain=squad.get("captain"), vice=squad.get("vice"),
        bench_boost=active_chip == "bboost",
        triple_captain=active_chip == "3xc",
        entry_id=entry_id, baseline=baseline, hits=hits)

    rival_states = [
        score_squad(
            r["starting"], r.get("bench", []), positions, pl,
            captain=r.get("captain"), vice=r.get("vice"),
            bench_boost=r.get("active_chip") == "bboost",
            triple_captain=r.get("active_chip") == "3xc",
            entry_id=r.get("entry_id"), baseline=int(r.get("total") or 0),
            hits=int(r.get("hits") or 0))
        for r in rivals if r.get("starting")
    ]
    swing = largest_swing(mine, rival_states, pl, names)

    def rival_name(eid: int | None) -> str:
        return next((x.get("name") or str(eid) for x in rivals
                     if x.get("entry_id") == eid), str(eid))

    my_row = {"entry_id": mine.entry_id, "name": "You", "you": True,
              "current": round(mine.baseline + mine.current, 2),
              "projected": round(mine.baseline + mine.projected, 2),
              "gw_points": round(mine.current, 2),
              "yet_to_play": mine.players_yet_to_play}
    ranked = sorted(
        [my_row]
        + [{"entry_id": r.entry_id,
            "name": rival_name(r.entry_id),
            "you": False,
            "current": round(r.baseline + r.current, 2),
            "projected": round(r.baseline + r.projected, 2),
            "gw_points": round(r.current, 2),
            "yet_to_play": r.players_yet_to_play}
           for r in rival_states],
        key=lambda e: -e["projected"])
    for i, row in enumerate(ranked, start=1):
        row["provisional_position"] = i
    place_of = {row["entry_id"]: row["provisional_position"] for row in ranked}

    return {
        **base,
        "available": True,
        "unavailable_reason": None,
        "active_chip": active_chip,
        "squad": mine.as_dict(),
        "players": [
            {**pl[p].as_dict(), "name": names.get(p, str(p)),
             "pos": positions.get(p), "in_xi": p in mine.autosubs.xi,
             "is_captain": p == mine.autosubs.captain}
            for p in list(mine.autosubs.xi) + list(mine.autosubs.bench)
            if p in pl
        ],
        "rivals": ranked,
        # B5. Every rival's fifteen, with the multiplier that scores each of
        # them and the product it produces. `rivals` above is aggregates only,
        # `strategy.json`'s `owners` is a count rather than a roster, and no
        # table in the database holds a rival's picks — so "how are they doing",
        # which is asked as often as "how am I doing", could only be answered
        # with a number nobody could check and no way to see whose players had
        # not kicked off. The detail was already in memory here and was thrown
        # away one line before the artifact.
        #
        # `sum(product) - hits == gw_points` holds for every entry, which is the
        # same guarantee the manager's own scorecard gives, computed by the same
        # scorer over the same `pl` — never a second fetch, or the two halves of
        # the league could disagree about a goal that had just gone in.
        "rival_squads": sorted(
            (rival_squad_dict(r, mine, pl, names, positions,
                              name=rival_name(r.entry_id),
                              place=place_of.get(r.entry_id, 0))
             for r in rival_states),
            key=lambda d: d["provisional_position"]),
        # A6. The manager's own row, lifted out of the table so a consumer does
        # not have to hunt the league for ``you``. Nothing wrote this key, and
        # ``mcp_server.publish`` reads ``live["me"]`` (and ``me.substitutions``,
        # and ``me.yet_to_play``) directly — so every one of them published None
        # while the numbers sat two lines away in ``rivals``.
        "me": {
            "entry_id": mine.entry_id,
            "current": my_row["current"],
            "projected": my_row["projected"],
            "gw_points": my_row["gw_points"],
            "yet_to_play": mine.players_yet_to_play,
            "provisional_position": my_row["provisional_position"],
            "substitutions": mine.autosubs.as_dict(),
        },
        "largest_swing": swing,
        "separation": {
            "confirmed": mine.confirmed,
            "provisional_bonus": mine.provisional,
            "predicted_remaining": round(mine.predicted, 2),
            "note": "Provisional bonus is computed from live BPS and changes "
                    "until each match is finalised. It is not confirmed.",
        },
    }


def _fixture_summary(states: dict[int, FixtureState]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for s in states.values():
        counts[s.state] = counts.get(s.state, 0) + 1
    return {
        "total": len(states),
        "by_state": counts,
        "all_finished": bool(states) and all(
            s.state in (STATE_FINISHED, STATE_POSTPONED) for s in states.values()),
        "bonus_final": bool(states) and all(
            s.bonus_final or s.state in (STATE_POSTPONED, STATE_SCHEDULED)
            for s in states.values()),
    }
