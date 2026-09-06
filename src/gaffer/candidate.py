"""One decision, one world (A-C1).

A decision used to be a bag of sibling fields. ``transfers_out`` said sell six
players, ``starting`` named an eleven drawn from the squad *before* those sales,
``comparison`` carried the expected value of the move, and ``hit_cost`` belonged
to whichever plan the comparison happened to be about. Every field was computed
correctly. Together they described more than one world.

The GW2 2026-27 snapshot is the worked example and the reason this module
exists: it recommends selling Raya, Calafiori, van Ewijk, Maguire, Vuskovic and
Tzolis, and simultaneously fields Raya, Calafiori, Maguire and Tzolis. Nothing
downstream could detect that, because nothing downstream knew the two lists were
supposed to describe the same squad. The review then scored the *held* eleven,
called it ``recommended_points``, and set it beside an expected value belonging
to the move and a realised total belonging to a different action entirely.

So: **a candidate owns its own world.** You give it a before-state and an
action; it *derives* the after-state, derives the hit, and refuses to exist if
the eleven, the bench, the captain or the money do not reconcile with what the
action actually did. Nothing may hand-assemble those fields alongside it.

External review, 2026-09-06 (GPT-6 Astra), put the requirement precisely:

    Downstream code should request "score candidate X," rather than collect
    starting from one place and hit_cost from another.

Three rules that are easy to get wrong and are enforced here rather than
documented:

* **A rejected candidate may not supply a selected candidate's fields.** A plan
  Gaffer priced and declined can have any expected value it likes. It is not
  allowed to be where "the decision's expected value" comes from.
* **Unknown is not empty.** Before the season's first deadline FPL publishes no
  picks. That state must stay ``known=False`` and must not decay into a squad of
  zero players that then "reconciles" trivially with an invented eleven.
* **The baseline is a candidate too.** Holding is an action. It gets an id, an
  after-state and a distribution reference like everything else, so a comparison
  is between two members of one set rather than between an object and a memory.

This module owns no football. It contains no projection, no scenario draw and no
opinion about which candidate is better. It is the shape a decision has to have
before any of those can be trusted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from gaffer import config

CANDIDATE_VERSION = "candidate-1.0"

#: Actions a candidate may describe. ``HOLD`` is the baseline and is never
#: implicit: a set without one has no reference point and is refused.
KIND_HOLD = "hold"
KIND_TRANSFER = "transfer"
KIND_CHIP = "chip"

#: Chips that replace the transfer economy rather than paying into it. A hit
#: under either of these is not a smaller hit, it is a category error.
FREE_TRANSFER_CHIPS = frozenset({"wildcard", "freehit"})

XI_SIZE = 11
BENCH_SIZE = 4


class CandidateError(ValueError):
    """A candidate that does not describe one coherent world.

    Deliberately a hard failure rather than a warning. Every historical defect
    this module exists to prevent was silent, internally consistent per field,
    and published.
    """


@dataclass(frozen=True)
class BeforeState:
    """What was true at the information cutoff, before any action.

    ``known=False`` is a first-class state. It means FPL had published no picks
    — which is exactly true before the season's first deadline — and it must
    survive into the artifact instead of being rendered as an empty squad.
    """

    known: bool
    squad: tuple[int, ...] = ()
    bank: int | None = None                     # tenths of a million
    free_transfers: int | None = None
    chips_available: tuple[str, ...] = ()
    source: str = ""                            # e.g. "entry_picks"
    source_event: int | None = None
    unresolved: tuple[str, ...] = ()            # named gaps, never silent
    prices: Mapping[int, int] = field(default_factory=dict)   # selling/purchase
    clubs: Mapping[int, int] = field(default_factory=dict)
    positions: Mapping[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.known and len(set(self.squad)) != len(self.squad):
            raise CandidateError("the held squad lists a player twice")
        if self.known and self.squad and len(self.squad) != config.SQUAD_SIZE:
            raise CandidateError(
                f"a known squad has {config.SQUAD_SIZE} players, "
                f"got {len(self.squad)}")
        if not self.known and self.squad:
            raise CandidateError(
                "an unknown squad state may not carry players; that is how an "
                "invented eleven acquires a squad to reconcile against")

    @property
    def fingerprint(self) -> str:
        """Identity of the world every candidate in a set must share."""
        parts = [
            "known" if self.known else "unknown",
            ",".join(str(p) for p in sorted(self.squad)),
            str(self.bank), str(self.free_transfers),
            ",".join(sorted(self.chips_available)),
            self.source, str(self.source_event),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "known": self.known,
            "squad": list(self.squad),
            "bank": self.bank,
            "free_transfers": self.free_transfers,
            "chips_available": list(self.chips_available),
            "source": self.source,
            "source_event": self.source_event,
            "unresolved": list(self.unresolved),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class Action:
    """What a candidate proposes to do. Transfers are paired by construction."""

    kind: str
    transfers_out: tuple[int, ...] = ()
    transfers_in: tuple[int, ...] = ()
    chip: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in (KIND_HOLD, KIND_TRANSFER, KIND_CHIP):
            raise CandidateError(f"unknown action kind {self.kind!r}")
        if len(self.transfers_out) != len(self.transfers_in):
            raise CandidateError(
                "a transfer sells and buys the same number of players: "
                f"{len(self.transfers_out)} out, {len(self.transfers_in)} in")
        if set(self.transfers_out) & set(self.transfers_in):
            raise CandidateError("a player cannot be both sold and bought")
        if self.kind == KIND_HOLD and (self.transfers_out or self.transfers_in):
            raise CandidateError("a hold makes no transfers")

    @property
    def n_transfers(self) -> int:
        return len(self.transfers_in)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "transfers_out": list(self.transfers_out),
            "transfers_in": list(self.transfers_in),
            "chip": self.chip,
            "n_transfers": self.n_transfers,
        }


def apply_action(before: BeforeState, action: Action) -> tuple[int, ...]:
    """THE transition function. Nothing else may compute an after-state.

    A single implementation is the whole point: two of them drift, and the
    version that drifted is the one that published a squad containing four
    players it had just sold.
    """
    if not before.known:
        if action.kind != KIND_HOLD and action.n_transfers:
            raise CandidateError(
                "cannot derive an after-state from an unknown before-state")
        return ()
    held = list(before.squad)
    for pid in action.transfers_out:
        if pid not in held:
            raise CandidateError(f"cannot sell {pid}: not in the held squad")
        held.remove(pid)
    for pid in action.transfers_in:
        if pid in held:
            raise CandidateError(f"cannot buy {pid}: already in the squad")
        held.append(pid)
    return tuple(sorted(held))


def hit_for(before: BeforeState, action: Action) -> int:
    """THE hit rule. Derived from the action, never carried beside it.

    ``comparison.hit_cost`` in the legacy payload described the hypothetical
    being compared, not the recommendation: GW3 2026-27 recorded a cost of 4
    while recommending zero transfers. A hit that is derived cannot disagree
    with the action it is supposed to price.
    """
    if action.chip in FREE_TRANSFER_CHIPS:
        return 0
    free = before.free_transfers
    if free is None:
        if action.n_transfers:
            raise CandidateError(
                "cannot price a transfer without a known free-transfer count")
        return 0
    return max(0, action.n_transfers - int(free)) * config.HIT_COST


@dataclass(frozen=True)
class Candidate:
    """One action, its derived world, and the eleven it actually fields.

    Build with :meth:`create`. The constructor takes an already-derived
    after-state so the dataclass stays a value object, and ``create`` is the
    only supported way to obtain one.
    """

    candidate_id: str
    action: Action
    after_squad: tuple[int, ...]
    xi: tuple[int, ...]
    bench: tuple[int, ...]                      # ordered: 1st sub first
    captain: int | None
    vice: int | None
    hit: int
    #: References, never copies. The distribution and expectation live where
    #: they were computed; a candidate says which one is its own.
    expectation: float | None = None
    distribution_ref: str | None = None
    scenario_set_id: str | None = None
    legality: tuple[str, ...] = ()              # named violations, empty = legal
    label: str = ""
    basis: str = ""

    @classmethod
    def create(
        cls, *, candidate_id: str, before: BeforeState, action: Action,
        xi: Sequence[int] = (), bench: Sequence[int] = (),
        captain: int | None = None, vice: int | None = None,
        expectation: float | None = None, distribution_ref: str | None = None,
        scenario_set_id: str | None = None, label: str = "", basis: str = "",
    ) -> Candidate:
        after = apply_action(before, action)
        hit = hit_for(before, action)
        cand = cls(
            candidate_id=candidate_id, action=action, after_squad=after,
            xi=tuple(xi), bench=tuple(bench), captain=captain, vice=vice,
            hit=hit, expectation=expectation, distribution_ref=distribution_ref,
            scenario_set_id=scenario_set_id, label=label, basis=basis,
            legality=tuple(_legality(before, after)),
        )
        cand.validate(before)
        return cand

    def validate(self, before: BeforeState) -> None:
        """Every invariant that makes this one world rather than several."""
        if not before.known:
            # Nothing may be asserted about a squad nobody has published. An
            # eleven is still allowed — it is a suggested build — but it may not
            # claim to be the result of an action on a squad.
            if self.after_squad:
                raise CandidateError(
                    "an unknown before-state cannot produce an after-state")
            if self.hit:
                raise CandidateError("an unknown before-state cannot be charged a hit")
            return

        sold, bought = set(self.action.transfers_out), set(self.action.transfers_in)
        after = set(self.after_squad)

        if sold & after:
            raise CandidateError(
                f"sold players are still in the squad: {sorted(sold & after)}")
        if not bought <= after:
            raise CandidateError(
                f"bought players are missing from the squad: {sorted(bought - after)}")
        if len(after) != config.SQUAD_SIZE:
            raise CandidateError(f"after-state holds {len(after)} players")

        if self.xi or self.bench:
            xi, bench = set(self.xi), set(self.bench)
            if len(xi) != XI_SIZE:
                raise CandidateError(f"an XI has {XI_SIZE} players, got {len(xi)}")
            if len(bench) != BENCH_SIZE:
                raise CandidateError(
                    f"a bench has {BENCH_SIZE} players, got {len(bench)}")
            if xi & bench:
                raise CandidateError(
                    f"players are in the XI and on the bench: {sorted(xi & bench)}")
            if xi | bench != after:
                missing = sorted(after - (xi | bench))
                extra = sorted((xi | bench) - after)
                raise CandidateError(
                    "XI plus bench must be exactly the after-state squad; "
                    f"missing {missing}, not owned {extra}")
            if self.captain is not None and self.captain not in xi:
                raise CandidateError("the captain is not in the XI")
            if self.vice is not None and self.vice not in xi:
                raise CandidateError("the vice-captain is not in the XI")
            if (self.captain is not None and self.captain == self.vice):
                raise CandidateError("captain and vice-captain are the same player")

        expected_hit = hit_for(before, self.action)
        if self.hit != expected_hit:
            raise CandidateError(
                f"hit {self.hit} does not match the action "
                f"({self.action.n_transfers} transfers, "
                f"{before.free_transfers} free, chip {self.action.chip}) "
                f"which costs {expected_hit}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "label": self.label,
            "basis": self.basis,
            "action": self.action.as_dict(),
            "after_squad": list(self.after_squad),
            "xi": list(self.xi),
            "bench": list(self.bench),
            "captain": self.captain,
            "vice": self.vice,
            "hit": self.hit,
            "expectation": self.expectation,
            "distribution_ref": self.distribution_ref,
            "scenario_set_id": self.scenario_set_id,
            "legality": list(self.legality),
        }


def _legality(before: BeforeState, after: Sequence[int]) -> list[str]:
    """Named rule violations, reported rather than raised.

    Illegality is a fact about a candidate, not a reason it cannot be
    described: an over-budget plan is worth showing precisely so it can be
    rejected for a stated reason. Only *incoherence* raises.
    """
    if not before.known or not after:
        return []
    out: list[str] = []
    if before.positions:
        counts: dict[str, int] = {}
        for pid in after:
            p = before.positions.get(pid)
            if p:
                counts[p] = counts.get(p, 0) + 1
        for pos, want in config.SQUAD_QUOTA.items():
            if counts.get(pos, 0) != want:
                out.append(f"quota:{pos}={counts.get(pos, 0)}!={want}")
    if before.clubs:
        per: dict[int, int] = {}
        for pid in after:
            c = before.clubs.get(pid)
            if c is not None:
                per[c] = per.get(c, 0) + 1
        for club, n in sorted(per.items()):
            if n > config.CLUB_LIMIT:
                out.append(f"club:{club}={n}>{config.CLUB_LIMIT}")
    if before.prices and before.bank is not None:
        held = set(before.squad)
        spend = sum(before.prices.get(p, 0) for p in after if p not in held)
        raise_ = sum(before.prices.get(p, 0) for p in held if p not in set(after))
        if spend - raise_ > before.bank:
            out.append(f"bank:need={spend - raise_}>have={before.bank}")
    return out


@dataclass
class CandidateSet:
    """Every world one decision considered, and which of them was chosen.

    The set is the unit of trust. A candidate is coherent on its own; only the
    set can say that the hold and the move were priced against the same
    before-state, that the selected action is one of the things actually
    considered, and that the number labelled "the decision's expected value"
    belongs to the candidate that was selected rather than to one that was not.
    """

    before: BeforeState
    candidates: dict[str, Candidate]
    baseline_id: str
    selected_id: str
    information_cutoff: str = ""
    gameweek: int | None = None
    season: str = ""
    versions: dict[str, Any] = field(default_factory=dict)
    scenario_set_id: str | None = None
    selection_reason: str = ""

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.baseline_id not in self.candidates:
            raise CandidateError(
                f"baseline {self.baseline_id!r} is not in the candidate set")
        if self.selected_id not in self.candidates:
            raise CandidateError(
                f"selected {self.selected_id!r} is not in the candidate set")
        for cid, cand in self.candidates.items():
            if cand.candidate_id != cid:
                raise CandidateError(
                    f"candidate keyed {cid!r} calls itself {cand.candidate_id!r}")
            cand.validate(self.before)
        base = self.candidates[self.baseline_id]
        if base.action.kind != KIND_HOLD and base.action.n_transfers:
            raise CandidateError("the baseline candidate must be a hold")

    @property
    def selected(self) -> Candidate:
        return self.candidates[self.selected_id]

    @property
    def baseline(self) -> Candidate:
        return self.candidates[self.baseline_id]

    def rejected(self) -> list[Candidate]:
        return [c for cid, c in self.candidates.items() if cid != self.selected_id]

    def expected_delta(self) -> float | None:
        """Selected minus baseline, and only ever those two.

        The legacy ``comparison.move_expected`` was the expectation of whatever
        plan the optimiser had priced, which in a ``too_close`` week was a
        candidate nobody selected. Reading it as "the decision's expected value"
        is what produced a verdict about an action that was never taken.
        """
        s, b = self.selected.expectation, self.baseline.expectation
        if s is None or b is None:
            return None
        return s - b

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_version": CANDIDATE_VERSION,
            "information_cutoff": self.information_cutoff,
            "gameweek": self.gameweek,
            "season": self.season,
            "versions": dict(self.versions),
            "scenario_set_id": self.scenario_set_id,
            "before_state": self.before.as_dict(),
            "candidates": [c.as_dict() for c in self.candidates.values()],
            "baseline_candidate_id": self.baseline_id,
            "selected_candidate_id": self.selected_id,
            "selection_reason": self.selection_reason,
            "expected_delta_selected_minus_baseline": self.expected_delta(),
        }


def candidate_id_for(action: Action, before: BeforeState) -> str:
    """A stable id: same before-state and same action, same id."""
    parts = [
        before.fingerprint, action.kind, action.chip or "",
        ",".join(str(p) for p in sorted(action.transfers_out)),
        ",".join(str(p) for p in sorted(action.transfers_in)),
    ]
    return "c_" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]
