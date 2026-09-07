"""The football Gaffer believed in, frozen before the deadline (A-C6).

A decision now has one identity (`gaffer.candidate`). This is the other half:
one set of *football outcomes*, frozen at the information cutoff, that every
candidate — Gaffer's and Myles's — is scored against.

Why it has to be stored rather than recomputed. `projections` is wiped and
rewritten every run, `scenarios.simulate` reads it, and the FPL API keeps
moving. So a scenario matrix drawn after a deadline is a *different* matrix,
built from information the decision never had. Re-simulating to grade a
decision is the same error as re-projecting to grade it, one level down.

What that buys, and it is the whole point:

    Under the exact model and information Gaffer had before the deadline, how
    did Gaffer's selected candidate and Myles's actual action compare across
    the same simulated football?

The old `outcome_distribution` could not answer that. It stored 500 samples of
*one* squad's total — a summary, not the football — so a second action could
only be scored by drawing new worlds, and two different draws cannot be
compared. Here the matrix is per player, so any legal candidate is scored by
indexing into it. Nothing draws twice.

**This freezes belief, not truth.** `scenarios.simulate` has known coherence
defects (`tests/test_scenario_invariants.py`): 13,346 of 479,016 no-appearance
player-scenarios score points, because conceded points, saves and DEFCON are
not gated on the appearance draw, and the clean-sheet contradiction runs at a
mean 0.0555. Those are recorded in every frozen file and are NOT repaired here.
A frozen record of a model later found wrong is still exactly what is needed to
ask what that model implied at the time; a frozen record of a *silently
different* model is worth nothing.

Storage is NDJSON in `data/state/`, with the rest of the longitudinal state and
for the same reasons (`gaffer.store.persist`): GitHub Actions runners are
ephemeral, `data/*.db` is gitignored, and `ci.yml` fails the build on a tracked
binary. Measured on the real GW4 draw, 495 players x 2,000 scenarios:

    raw text          3.01 MB      compressed by git   0.469 MB
    npz equivalent    0.45 MB      compressed by git   0.454 MB

Three per cent, for a file that diffs, greps and reviews like everything else
in the directory. Points are stored as integers because FPL scores are integers
— verified, not assumed: freezing refuses a matrix whose values are not whole.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from gaffer import candidate as CAND
from gaffer import config

EVIDENCE_VERSION = "evidence-1.0"

#: Chips that change how a squad is scored, as opposed to what it costs.
CHIP_TRIPLE_CAPTAIN = "3xc"
CHIP_BENCH_BOOST = "bboost"

#: Where the action came from, and it is never inferred.
PROV_PREDEADLINE = "captured_predeadline"
PROV_RECONSTRUCTED = "reconstructed_postdeadline"

#: What `scenarios.simulate` is KNOWN to get wrong, recorded in every frozen
#: file so a later reader cannot mistake a frozen belief for a validated one.
#: Measured 2026-09-06 and pinned by `tests/test_scenario_invariants.py`; NOT
#: repaired here, deliberately. See `gaffer.model.scenarios` and A-C5.
SCENARIO_LIMITATIONS: tuple[str, ...] = (
    "conceded points, saves and DEFCON are not gated on the appearance draw: "
    "13,346 of 479,016 no-appearance player-scenarios scored points in the "
    "2026-09-06 GW4 draw (3,767 positive, 9,579 negative)",
    "the clean-sheet probability and the opposing lineup's goal lambda are two "
    "estimates of one quantity and disagree; the contradiction ran at a mean "
    "0.0555 and a maximum 0.2727 across 20 fixture-sides",
    "starts are drawn per player rather than as a legal eleven per club",
    "bonus is a mean-preserving proxy, not FPL's BPS allocation",
)

#: How close to the deadline a run must be before it may freeze. The FIRST run
#: inside the window wins and later runs refuse, so a gameweek has exactly one
#: matrix rather than one per refresh. Six hours because the scheduled refresh
#: has been landing roughly every 100 minutes in practice, not every 15 as the
#: cron implies, and a window that misses every run freezes nothing at all.
#: The cost is explicit rather than hidden: the recorded `information_cutoff`
#: says how old the belief is, and late team news is outside it.
FREEZE_WINDOW_HOURS = 6.0


class EvidenceError(RuntimeError):
    """Frozen evidence that is missing, malformed, or being asked to change."""


class ImmutableError(EvidenceError):
    """A write that would rewrite football already frozen."""


class InsufficientState(EvidenceError):
    """An action that cannot be scored honestly from what is recorded.

    Raised rather than defaulted. Substituting zeros for an eleven we could not
    reconstruct would produce a number, and the number would be wrong in the
    one direction nobody checks.
    """


# ---------------------------------------------------------------------------
# The frozen object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenEvidence:
    """One gameweek's football, as the model saw it before the deadline."""

    evidence_id: str
    season: str
    gameweek: int
    deadline: str
    information_cutoff: str
    #: Everything that decides what the numbers mean. A candidate scored under
    #: a different model version is not comparable, and this is how a reader
    #: finds out rather than assuming.
    model_version: str
    sim_version: str
    rules_version: str
    scenario_seed: int
    n_sims: int
    player_ids: tuple[int, ...]
    positions: Mapping[int, str]
    points: np.ndarray                       # (n_players, n_sims) int16
    appeared: np.ndarray                     # (n_players, n_sims) bool
    #: The world every candidate here shares, by fingerprint rather than by
    #: copy: `gaffer.candidate.BeforeState.fingerprint`.
    before_state_fingerprint: str
    candidate_ids: tuple[str, ...]
    selected_candidate_id: str
    baseline_candidate_id: str
    content_digest: str
    #: What the generator is known to get wrong, travelling with the belief it
    #: produced. Never a reason to discard the freeze.
    limitations: tuple[str, ...] = ()
    scenario_diagnostics: Mapping[str, Any] = field(default_factory=dict)
    evidence_version: str = EVIDENCE_VERSION

    # -- identity ----------------------------------------------------------

    @property
    def index(self) -> dict[int, int]:
        return {pid: i for i, pid in enumerate(self.player_ids)}

    @property
    def scenario_set_id(self) -> str:
        """Identity of the FOOTBALL, not of the recipe that made it.

        `seed:n_sims` was the old id and it is the same string in every
        gameweek — the seed is fixed — so two entirely different matrices
        claimed one identity. A content digest cannot do that.
        """
        return f"{self.season}:gw{self.gameweek}:{self.content_digest[:12]}"

    def as_scenario_set(self) -> Any:
        """Rehydrate a real `ScenarioSet`, so scoring uses production code.

        Deliberately not a private re-implementation of squad scoring. If the
        live autosub rules change, the frozen path changes with them, and the
        two can never quietly disagree about what an eleven is worth.
        """
        from gaffer.model import scenarios as SC

        return SC.ScenarioSet(
            points=self.points.astype(np.float32),
            player_ids=list(self.player_ids),
            index=self.index,
            n_sims=self.n_sims,
            seed=self.scenario_seed,
            appeared=self.appeared,
            meta={"frozen": True, "evidence_id": self.evidence_id},
            diagnostics=dict(self.scenario_diagnostics),
        )

    # -- scoring -----------------------------------------------------------

    def score(self, cand: CAND.Candidate, *, autosubs: bool = True) -> np.ndarray:
        """Net points for one candidate, in every frozen scenario.

        The ONLY way a candidate becomes a number here. Both arms of any
        comparison call this, on this object, so they cannot index different
        football: there is one matrix and no path that draws another.

        Net of the hit, once. `Candidate.hit` is derived from the action by
        `gaffer.candidate`, so a candidate cannot carry a hit its transfers do
        not justify, and this cannot double-count one.
        """
        if not cand.xi:
            raise InsufficientState(
                f"candidate {cand.candidate_id} has no XI, so it cannot be "
                "scored; a missing eleven is not an eleven of zeros")
        missing = [p for p in cand.xi if p not in self.index]
        if missing:
            raise InsufficientState(
                f"candidate {cand.candidate_id} fields {missing}, who are not "
                "in this evidence; scoring them as zero would invent a result")

        chip = cand.action.chip
        mult = 3 if chip == CHIP_TRIPLE_CAPTAIN else 2
        bench_boost = chip == CHIP_BENCH_BOOST
        ss = self.as_scenario_set()

        if bench_boost:
            # Every one of the fifteen plays, so no substitution can occur and
            # the autosub question does not arise.
            total = ss.squad_points(list(cand.xi), captain=None,
                                    bench=list(cand.bench),
                                    captain_multiplier=mult, bench_boost=True)
        elif autosubs and cand.bench and self.positions:
            total = ss.points_with_autosubs(
                list(cand.xi), list(cand.bench), dict(self.positions),
                captain=None, captain_multiplier=mult)
        else:
            total = ss.squad_points(list(cand.xi), captain=None,
                                    captain_multiplier=mult)

        total = np.asarray(total, dtype=np.float32) + self._armband(cand, mult)
        return total - float(cand.hit)

    def _armband(self, cand: CAND.Candidate, mult: int) -> np.ndarray:
        """The captain's extra points, passing to the vice where FPL passes it.

        `ScenarioSet.points_with_autosubs` says in its own docstring that it
        does NOT move the armband, and that the resulting understatement is
        acceptable for a chip valuation. It is not acceptable here: the whole
        comparison can turn on one captaincy, and a captain who did not play is
        exactly the case a distribution is being asked about.

        This is not an approximation. The appearance mask already records, per
        scenario, whether each of them played, so the rule is applied rather
        than estimated: the armband moves to the vice if and only if the
        captain recorded no minutes, and evaporates if neither played.
        """
        n = self.n_sims
        if cand.captain is None:
            return np.zeros(n, dtype=np.float32)
        ci = self.index.get(cand.captain)
        if ci is None:
            raise InsufficientState(
                f"captain {cand.captain} is not in this evidence")
        cap_played = self.appeared[ci]
        extra = np.where(cap_played, self.points[ci].astype(np.float32), 0.0)
        if cand.vice is not None:
            vi = self.index.get(cand.vice)
            if vi is None:
                raise InsufficientState(
                    f"vice-captain {cand.vice} is not in this evidence")
            extra = np.where(~cap_played & self.appeared[vi],
                             self.points[vi].astype(np.float32), extra)
        return extra * (mult - 1)

    def compare(self, a: CAND.Candidate, b: CAND.Candidate) -> dict[str, Any]:
        """Two candidates, one set of worlds. Facts only, no verdict.

        Deliberately returns no judgement. `gaffer.review` suspended its
        categorical grade because `positive_ev` described Gaffer's action and
        the realised total described a different one; being able to score both
        does not by itself repair that, because the model producing these
        expectations may simply be wrong. What is honest to publish is the
        paired difference and how often each wins, which is what this returns.
        """
        sa, sb = self.score(a), self.score(b)
        d = sb - sa
        return {
            "evidence_id": self.evidence_id,
            "scenario_set_id": self.scenario_set_id,
            "n_sims": self.n_sims,
            "a_candidate_id": a.candidate_id,
            "b_candidate_id": b.candidate_id,
            "model_expected_a": round(float(sa.mean()), 3),
            "model_expected_b": round(float(sb.mean()), 3),
            "model_relative_delta": round(float(d.mean()), 3),
            "p_b_beats_a": round(float((d > 0).mean()), 4),
            "p_a_beats_b": round(float((d < 0).mean()), 4),
            "p_tie": round(float((d == 0).mean()), 4),
            "delta_percentiles": {
                str(q): round(float(np.percentile(d, q)), 2)
                for q in (5, 25, 50, 75, 95)
            },
            "delta_sd": round(float(d.std(ddof=1)), 3),
            "basis": (
                "one frozen pre-deadline scenario matrix; both candidates "
                "indexed the same player rows and the same scenario columns"),
            "not_a_verdict": (
                "these are the model's own implications. They say what Gaffer "
                "believed, not whether either action was right — the model may "
                "be wrong, and its known coherence defects travel with it in "
                "`limitations`."),
        }

    # -- persistence -------------------------------------------------------

    def to_lines(self) -> list[str]:
        """Canonical NDJSON. Deterministic: same evidence, same bytes."""
        header = {
            "record": "header",
            "evidence_version": self.evidence_version,
            "evidence_id": self.evidence_id,
            "season": self.season,
            "gameweek": self.gameweek,
            "deadline": self.deadline,
            "information_cutoff": self.information_cutoff,
            "model_version": self.model_version,
            "sim_version": self.sim_version,
            "rules_version": self.rules_version,
            "scenario_seed": self.scenario_seed,
            "n_sims": self.n_sims,
            "n_players": len(self.player_ids),
            "before_state_fingerprint": self.before_state_fingerprint,
            "candidate_ids": list(self.candidate_ids),
            "selected_candidate_id": self.selected_candidate_id,
            "baseline_candidate_id": self.baseline_candidate_id,
            "content_digest": self.content_digest,
            "scenario_set_id": self.scenario_set_id,
            "limitations": list(self.limitations),
            "scenario_diagnostics": dict(self.scenario_diagnostics),
        }
        return [json.dumps(header, sort_keys=True, separators=(",", ":")),
                *_player_lines(self.player_ids, self.positions,
                               self.points, self.appeared)]

    def save(self, path: Path | str) -> Path:
        """Write once. A second write to the same path is refused."""
        p = Path(path)
        if p.exists():
            raise ImmutableError(
                f"{p} already exists; frozen evidence is written once. To "
                "record a different belief, freeze it under a new gameweek or "
                "a new evidence id — never over this one.")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(self.to_lines()) + "\n", encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: Path | str) -> FrozenEvidence:
        p = Path(path)
        if not p.exists():
            raise EvidenceError(f"no frozen evidence at {p}")
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            raise EvidenceError(f"{p} is empty")
        head = json.loads(lines[0])
        if head.get("record") != "header":
            raise EvidenceError(f"{p} does not begin with a header record")
        if head.get("evidence_version") != EVIDENCE_VERSION:
            raise EvidenceError(
                f"{p} is {head.get('evidence_version')}, this build reads "
                f"{EVIDENCE_VERSION}")

        pids: list[int] = []
        pos: dict[int, str] = {}
        pts: list[list[int]] = []
        app: list[list[bool]] = []
        for ln in lines[1:]:
            r = json.loads(ln)
            if r.get("record") != "player":
                raise EvidenceError(f"{p}: unexpected record {r.get('record')!r}")
            pid = int(r["p"])
            pids.append(pid)
            pos[pid] = r["pos"]
            pts.append([int(x) for x in r["s"].split()])
            app.append([c == "1" for c in r["a"]])

        n_sims = int(head["n_sims"])
        if len(pids) != int(head["n_players"]):
            raise EvidenceError(
                f"{p}: header says {head['n_players']} players, file has {len(pids)}")
        if len(set(pids)) != len(pids):
            raise EvidenceError(f"{p}: a player id appears twice")
        for i, row in enumerate(pts):
            if len(row) != n_sims or len(app[i]) != n_sims:
                raise EvidenceError(
                    f"{p}: player {pids[i]} has {len(row)} points and "
                    f"{len(app[i])} appearance flags, expected {n_sims} of each")

        points = np.array(pts, dtype=np.int16)
        appeared = np.array(app, dtype=bool)
        digest = _digest(tuple(pids), pos, points, appeared)
        if digest != head["content_digest"]:
            raise EvidenceError(
                f"{p}: content digest {digest} does not match the header's "
                f"{head['content_digest']} — the file has been edited since it "
                "was frozen, and nothing downstream may treat it as evidence")

        return cls(
            evidence_id=head["evidence_id"], season=head["season"],
            gameweek=int(head["gameweek"]), deadline=head["deadline"],
            information_cutoff=head["information_cutoff"],
            model_version=head["model_version"], sim_version=head["sim_version"],
            rules_version=head["rules_version"],
            scenario_seed=int(head["scenario_seed"]), n_sims=n_sims,
            player_ids=tuple(pids), positions=pos,
            points=points, appeared=appeared,
            before_state_fingerprint=head["before_state_fingerprint"],
            candidate_ids=tuple(head["candidate_ids"]),
            selected_candidate_id=head["selected_candidate_id"],
            baseline_candidate_id=head["baseline_candidate_id"],
            content_digest=digest,
            limitations=tuple(head.get("limitations") or ()),
            scenario_diagnostics=head.get("scenario_diagnostics") or {},
            evidence_version=head["evidence_version"],
        )


# ---------------------------------------------------------------------------
# Freezing
# ---------------------------------------------------------------------------


def _player_lines(pids, positions, points, appeared) -> list[str]:
    out = []
    for i, pid in enumerate(pids):
        out.append(json.dumps({
            "record": "player",
            "p": int(pid),
            "pos": positions.get(pid, "MID"),
            "s": " ".join(str(int(v)) for v in points[i]),
            "a": "".join("1" if v else "0" for v in appeared[i]),
        }, sort_keys=True, separators=(",", ":")))
    return out


def _digest(pids, positions, points, appeared) -> str:
    h = hashlib.sha256()
    for ln in _player_lines(pids, positions, points, appeared):
        h.update(ln.encode())
        h.update(b"\n")
    return h.hexdigest()[:32]


def freeze(
    scen: Any, cand_set: CAND.CandidateSet, *, season: str, gameweek: int,
    deadline: str, information_cutoff: str, positions: Mapping[int, str],
    model_version: str, rules_version: str, limitations: Sequence[str] = (),
) -> FrozenEvidence:
    """Turn a live draw plus its candidate set into one immutable object.

    Refuses rather than rounds. FPL scores are whole numbers and the frozen
    matrix stores integers; if a future model produces fractional points,
    silently rounding them would make the freeze a lossy record of a belief
    nobody could reconstruct.
    """
    raw = np.asarray(scen.points, dtype=np.float64)
    if raw.size == 0:
        raise EvidenceError("cannot freeze an empty scenario set")
    if np.abs(raw - np.rint(raw)).max() > 1e-6:
        raise EvidenceError(
            "scenario points are not whole numbers, so storing them as "
            "integers would lose information; the frozen format needs "
            "widening before this model can be frozen")
    appeared = getattr(scen, "appeared", None)
    if appeared is None:
        raise EvidenceError(
            "this scenario set carries no appearance mask, so autosubs and "
            "the armband could not be resolved from it")

    points = np.rint(raw).astype(np.int16)
    appeared = np.asarray(appeared, dtype=bool)
    pids = tuple(int(p) for p in scen.player_ids)
    pos = {int(p): positions.get(int(p), "MID") for p in pids}
    digest = _digest(pids, pos, points, appeared)
    ev_id = "ev_" + hashlib.sha256(
        f"{season}|{gameweek}|{information_cutoff}|{digest}".encode()
    ).hexdigest()[:16]

    return FrozenEvidence(
        evidence_id=ev_id, season=season, gameweek=int(gameweek),
        deadline=deadline, information_cutoff=information_cutoff,
        model_version=model_version,
        sim_version=str((getattr(scen, "meta", {}) or {}).get(
            "sim_version", "unknown")),
        rules_version=rules_version,
        scenario_seed=int(getattr(scen, "seed", 0) or 0),
        n_sims=int(getattr(scen, "n_sims", points.shape[1])),
        player_ids=pids, positions=pos, points=points, appeared=appeared,
        before_state_fingerprint=cand_set.before.fingerprint,
        candidate_ids=tuple(cand_set.candidates),
        selected_candidate_id=cand_set.selected_id,
        baseline_candidate_id=cand_set.baseline_id,
        content_digest=digest,
        limitations=tuple(limitations),
        scenario_diagnostics=dict(getattr(scen, "diagnostics", {}) or {}),
    )


def candidate_set_from_payload(cs: Mapping[str, Any]) -> CAND.CandidateSet:
    """Rebuild the candidate set from its serialised form, re-validating it.

    The pipeline holds the set as a dict by the time the snapshot is written,
    and freezing wants the real object. Reconstructing rather than trusting the
    dict is deliberate: every candidate is re-checked against the before-state
    on the way in, so evidence can never be frozen around a set that does not
    reconcile. The fingerprint is asserted rather than assumed, because a
    before-state that rebuilds into a different world is precisely the failure
    this whole layer exists to make impossible.
    """
    bs = cs["before_state"]
    before = CAND.BeforeState(
        known=bool(bs["known"]), squad=tuple(bs["squad"]), bank=bs["bank"],
        free_transfers=bs["free_transfers"],
        chips_available=tuple(bs.get("chips_available") or ()),
        source=bs.get("source", ""), source_event=bs.get("source_event"),
        unresolved=tuple(bs.get("unresolved") or ()))
    if before.fingerprint != bs.get("fingerprint"):
        raise EvidenceError(
            "the before-state does not rebuild to its own fingerprint "
            f"({before.fingerprint} vs {bs.get('fingerprint')})")
    cands: dict[str, CAND.Candidate] = {}
    for c in cs["candidates"]:
        a = c["action"]
        cands[c["candidate_id"]] = CAND.Candidate.create(
            candidate_id=c["candidate_id"], before=before,
            action=CAND.Action(
                kind=a["kind"], transfers_out=tuple(a["transfers_out"]),
                transfers_in=tuple(a["transfers_in"]), chip=a.get("chip")),
            xi=tuple(c["xi"]), bench=tuple(c["bench"]), captain=c.get("captain"),
            vice=c.get("vice"), expectation=c.get("expectation"),
            label=c.get("label", ""), basis=c.get("basis", ""))
    return CAND.CandidateSet(
        before=before, candidates=cands,
        baseline_id=cs["baseline_candidate_id"],
        selected_id=cs["selected_candidate_id"],
        gameweek=cs.get("gameweek"), season=cs.get("season", ""),
        versions=dict(cs.get("versions") or {}))


def path_for(season: str, gameweek: int, data_dir: Path | None = None) -> Path:
    root = Path(data_dir or config.DATA_DIR) / "state" / "evidence"
    return root / f"{season}-gw{int(gameweek):02d}.ndjson"


def exists(season: str, gameweek: int, data_dir: Path | None = None) -> bool:
    return path_for(season, gameweek, data_dir).exists()


def load_for(season: str, gameweek: int,
             data_dir: Path | None = None) -> FrozenEvidence | None:
    p = path_for(season, gameweek, data_dir)
    return FrozenEvidence.load(p) if p.exists() else None


def should_freeze(now: Any, deadline: str | None, season: str, gameweek: int,
                  data_dir: Path | None = None) -> tuple[bool, str]:
    """Whether THIS run is the one that freezes, and why.

    One matrix per gameweek, created by the first run inside the window. Later
    runs in the same window see the file and decline: a pseudo-immutable
    artifact rewritten every ninety minutes is not immutable, it is just the
    latest one wearing the word.
    """
    from datetime import datetime

    if not deadline:
        return False, "no deadline is known for this gameweek"
    if exists(season, gameweek, data_dir):
        return False, "already frozen for this gameweek"
    try:
        dl = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    except ValueError:
        return False, f"deadline {deadline!r} is not a timestamp"
    hours = (dl - now).total_seconds() / 3600.0
    if hours <= 0:
        return False, "the deadline has passed; belief can no longer be frozen"
    if hours > FREEZE_WINDOW_HOURS:
        return False, (f"{hours:.1f}h before the deadline, outside the "
                       f"{FREEZE_WINDOW_HOURS:.0f}h freeze window")
    return True, f"{hours:.1f}h before the deadline, inside the freeze window"


# ---------------------------------------------------------------------------
# The action Myles actually took
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedAction:
    """What was really done, as a candidate, with its provenance attached.

    A candidate like any other — same before-state, same contract, same
    scoring. There is deliberately no parallel "human score" structure: the
    moment the human's action has its own shape, it acquires its own arithmetic
    and the comparison stops being like-for-like. That is the defect this
    programme exists to remove, not to reintroduce on the other side.
    """

    season: str
    gameweek: int
    evidence_id: str
    provenance: str
    recorded_at: str
    candidate: CAND.Candidate
    source: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if self.provenance not in (PROV_PREDEADLINE, PROV_RECONSTRUCTED):
            raise EvidenceError(f"unknown provenance {self.provenance!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "record": "action",
            "season": self.season,
            "gameweek": self.gameweek,
            "evidence_id": self.evidence_id,
            "provenance": self.provenance,
            "recorded_at": self.recorded_at,
            "source": self.source,
            "notes": self.notes,
            "candidate": self.candidate.as_dict(),
            "provenance_means": (
                "recorded before the deadline, so it is what he intended with "
                "the information he had"
                if self.provenance == PROV_PREDEADLINE else
                "rebuilt from FPL's published picks after the deadline. It is "
                "what he DID. It is not evidence about what he knew, and it "
                "may be scored against the frozen model only because his "
                "choices do not change the football"),
        }


def action_from_picks(
    before: CAND.BeforeState, picks: Mapping[str, Any], *,
    free_transfers: int | None = None,
) -> CAND.Candidate:
    """Rebuild the action from `entry/{id}/event/{gw}/picks/`.

    Refuses rather than guesses. FPL publishes the resulting fifteen and the
    bench order but not the transfers, so those are derived by differencing the
    before-state — which is exact when the before-state is the previous
    gameweek's squad and impossible otherwise.
    """
    rows = picks.get("picks") or []
    if len(rows) != config.SQUAD_SIZE:
        raise InsufficientState(
            f"picks carry {len(rows)} players, not {config.SQUAD_SIZE}; the "
            "action cannot be reconstructed from them")
    after = tuple(sorted(int(r["element"]) for r in rows))
    if not before.known:
        raise InsufficientState(
            "no before-state is known, so a transfer cannot be derived by "
            "differencing; the action is not reconstructable")

    held = set(before.squad)
    out = tuple(sorted(held - set(after)))
    inn = tuple(sorted(set(after) - held))

    hist = picks.get("entry_history") or {}
    chip = picks.get("active_chip") or None
    published_cost = int(hist.get("event_transfers_cost") or 0)
    ft = free_transfers if free_transfers is not None else before.free_transfers
    inferred_ft = False
    if ft is None and out:
        # The hit is published; invert it rather than inventing a free-transfer
        # count, so the derived cost is FPL's own number.
        ft = max(0, len(inn) - published_cost // config.HIT_COST)
        inferred_ft = True

    kind = (CAND.KIND_CHIP if chip else
            CAND.KIND_TRANSFER if inn else CAND.KIND_HOLD)
    action = CAND.Action(kind=kind, transfers_out=out, transfers_in=inn,
                         chip=chip)

    xi = tuple(int(r["element"]) for r in rows if int(r["position"]) <= 11)
    bench = tuple(int(r["element"]) for r in sorted(
        (r for r in rows if int(r["position"]) > 11),
        key=lambda r: int(r["position"])))
    cap = next((int(r["element"]) for r in rows if r.get("is_captain")), None)
    vice = next((int(r["element"]) for r in rows if r.get("is_vice_captain")), None)
    if cap is None:
        raise InsufficientState("these picks name no captain")

    rebuilt = CAND.BeforeState(
        known=before.known, squad=before.squad, bank=before.bank,
        free_transfers=ft, chips_available=before.chips_available,
        source=before.source, source_event=before.source_event,
        unresolved=before.unresolved)
    cand = CAND.Candidate.create(
        candidate_id=CAND.candidate_id_for(action, before),
        before=rebuilt, action=action, xi=xi, bench=bench, captain=cap,
        vice=vice, label="Actual", basis="fpl_picks")

    # THE integrity check, and it has to be this one. Both squads are fifteen,
    # so counting transfers in and out can never disagree -- that comparison is
    # dead code dressed as a guard. What can be checked is the money: FPL
    # publishes what the week's transfers actually cost, and if the hit derived
    # from this before-state does not match it, then the before-state is not
    # the squad these picks were made from. Fifteen changes is a legitimate
    # wildcard and an illegitimate two-gameweek gap, and only the cost tells
    # them apart.
    #
    # Skipped when the free-transfer count was inferred FROM that same cost,
    # where the two agree by construction and the check would prove nothing.
    if not inferred_ft and cand.hit != published_cost:
        raise InsufficientState(
            f"these picks cost {published_cost} points and this before-state "
            f"implies {cand.hit} ({action.n_transfers} transfers on {ft} free"
            f"{', chip ' + chip if chip else ''}); the before-state and these "
            "picks are not one gameweek apart")
    return cand


def paired_comparison(
    season: str, gameweek: int, snapshot_payload: Mapping[str, Any],
    data_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Gaffer's selected candidate against the action actually taken.

    Returns None when the evidence for scoring both does not exist, and says
    which half is missing rather than filling it in. The block is deliberately
    all quantities and no grade: `gaffer.review` suspended its categorical
    verdict because the expectation described Gaffer's action and the realised
    total described a different one, and being able to score both does not by
    itself repair that. What these numbers say is what the model implied. The
    model may be wrong -- its known defects are in `limitations` -- and no
    arrangement of them settles whether a decision was good.
    """
    ev = load_for(season, gameweek, data_dir)
    if ev is None:
        return {"available": False,
                "why": "no frozen pre-deadline evidence for this gameweek; "
                       "evidence is prospective from A-C6 and earlier "
                       "gameweeks have none"}
    cs_payload = (snapshot_payload or {}).get("candidate_set") or {}
    if not cs_payload or cs_payload.get("error"):
        return {"available": False, "evidence_id": ev.evidence_id,
                "why": "the snapshot carries no coherent candidate set"}
    actions = actions_for(season, gameweek, data_dir)
    if not actions:
        return {"available": False, "evidence_id": ev.evidence_id,
                "why": "no action has been recorded for this gameweek, so "
                       "there is nothing to compare the selection against"}

    cset = candidate_set_from_payload(cs_payload)

    # The freeze happens at ONE run inside the window; the snapshot being
    # reviewed is the LAST run before the deadline. Usually the same run, but
    # not necessarily: the scheduler's observed gap between refreshes has a
    # median of 140 minutes and a measured maximum of 51 hours, so a later run
    # can change the decision after the football was frozen. When it does, the
    # frozen matrix still describes a real belief -- but it is not the belief
    # this snapshot recommends, and comparing them would quietly mix two.
    if ev.before_state_fingerprint != cset.before.fingerprint:
        return {"available": False, "evidence_id": ev.evidence_id,
                "why": ("the frozen evidence was taken against a different "
                        f"before-state ({ev.before_state_fingerprint}) from "
                        f"the snapshot being reviewed ({cset.before.fingerprint}); "
                        "the squad or the transfer resources moved between the "
                        "freeze and the deadline, so these are two worlds and "
                        "not one comparison")}
    if cset.selected_id not in ev.candidate_ids:
        return {"available": False, "evidence_id": ev.evidence_id,
                "why": (f"the snapshot selects {cset.selected_id}, which was "
                        "not among the candidates frozen before the deadline; "
                        "the recommendation changed after the freeze")}

    rec = actions[-1]
    try:
        human = candidate_from_record(rec, cset.before)
        out = ev.compare(cset.selected, human)
    except (CAND.CandidateError, EvidenceError) as exc:
        return {"available": False, "evidence_id": ev.evidence_id,
                "why": f"the recorded action could not be scored: {exc}"}

    out.update({
        "available": True,
        "a_is": "gaffer_selected_candidate",
        "b_is": "action_actually_taken",
        "action_provenance": rec["provenance"],
        "action_recorded_at": rec["recorded_at"],
        "information_cutoff": ev.information_cutoff,
        "model_version": ev.model_version,
        "limitations": list(ev.limitations),
        "same_action": cset.selected.candidate_id == human.candidate_id,
    })
    return out


def actions_path(data_dir: Path | None = None) -> Path:
    return Path(data_dir or config.DATA_DIR) / "state" / "actions.ndjson"


def record_action(rec: RecordedAction, data_dir: Path | None = None) -> Path:
    """Append-only. A second recording for one gameweek is a new line.

    Not an overwrite: a pre-deadline intention and a post-deadline
    reconstruction of the same gameweek are two different facts and both are
    worth keeping, precisely so a later reader can see whether they agreed.
    """
    p = actions_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec.as_dict(), sort_keys=True, separators=(",", ":"))
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return p


def actions_for(season: str, gameweek: int,
                data_dir: Path | None = None) -> list[dict[str, Any]]:
    p = actions_path(data_dir)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if r.get("season") == season and int(r.get("gameweek", -1)) == gameweek:
            out.append(r)
    return out


def candidate_from_record(rec: Mapping[str, Any],
                          before: CAND.BeforeState) -> CAND.Candidate:
    """Rebuild the stored candidate, re-validating it against the before-state."""
    c = rec["candidate"]
    a = c["action"]
    action = CAND.Action(
        kind=a["kind"], transfers_out=tuple(a["transfers_out"]),
        transfers_in=tuple(a["transfers_in"]), chip=a.get("chip"))
    return CAND.Candidate.create(
        candidate_id=c["candidate_id"], before=before, action=action,
        xi=tuple(c["xi"]), bench=tuple(c["bench"]),
        captain=c.get("captain"), vice=c.get("vice"),
        label=c.get("label", ""), basis=c.get("basis", ""))
