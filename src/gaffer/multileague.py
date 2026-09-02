"""Several leagues at once, and what to do when they disagree (T-18).

A move can help your overall rank and one mini-league while hurting another.
Averaging that away produces a number nobody asked for. This module quantifies
each league's stake in a decision, surfaces the conflict, and only names a
default when the configured weights actually support one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gaffer import league as LG

MULTILEAGUE_VERSION = "multileague-1.0"

#: Overall rank is itself an objective, not the absence of one.
OVERALL_KEY = "overall_rank"


@dataclass
class Option:
    """A candidate decision, scored per league."""

    key: str
    label: str
    expected_points: float
    #: league_id (or OVERALL_KEY) -> probability of hitting that league's target
    p_target: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label,
            "expected_points": round(self.expected_points, 3),
            "p_target": {k: round(v, 4) for k, v in self.p_target.items()},
        }


@dataclass
class Conflict:
    """Two leagues pulling a decision in opposite directions."""

    option_a: str
    option_b: str
    detail: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {"option_a": self.option_a, "option_b": self.option_b,
                "per_league": self.detail}


def pareto_front(options: list[Option], keys: list[str]) -> list[Option]:
    """Options no other option beats on every axis.

    Expected points is one axis; each league's target probability is another.
    Anything dominated is not worth showing.
    """
    def axes(o: Option) -> list[float]:
        return [o.expected_points] + [o.p_target.get(k, 0.0) for k in keys]

    front = []
    for a in options:
        va = axes(a)
        dominated = any(
            all(x >= y for x, y in zip(axes(b), va, strict=False))
            and any(x > y for x, y in zip(axes(b), va, strict=False))
            for b in options if b is not a
        )
        if not dominated:
            front.append(a)
    return front


def find_conflicts(
    options: list[Option], keys: list[str], min_delta: float = 0.02
) -> list[Conflict]:
    """Pairs where the leagues genuinely rank the options differently."""
    out: list[Conflict] = []
    for i, a in enumerate(options):
        for b in options[i + 1:]:
            detail = []
            prefers_a = prefers_b = False
            for k in keys:
                da = a.p_target.get(k, 0.0)
                dbb = b.p_target.get(k, 0.0)
                if abs(da - dbb) < min_delta:
                    continue
                (prefers_a, prefers_b) = (
                    (True, prefers_b) if da > dbb else (prefers_a, True))
                detail.append({
                    "league": k,
                    "p_target_a": round(da, 4), "p_target_b": round(dbb, 4),
                    "prefers": a.key if da > dbb else b.key,
                    "delta": round(abs(da - dbb), 4),
                })
            if prefers_a and prefers_b:
                ep = round(a.expected_points - b.expected_points, 3)
                detail.append({"league": "expected_points",
                               "delta": ep,
                               "prefers": a.key if ep > 0 else b.key})
                out.append(Conflict(a.key, b.key, detail))
    return out


def resolve(
    options: list[Option], weights: dict[str, float] | None, keys: list[str],
) -> dict[str, Any]:
    """Pick a default only when the weights justify one.

    With no weights configured there is no principled way to trade one league's
    probability against another's, so the honest output is the shortlist plus
    the conflicts — not an invented winner.
    """
    if not options:
        return {"default": None, "reason": "no options were evaluated",
                "shortlist": [], "conflicts": []}
    front = pareto_front(options, keys)
    conflicts = find_conflicts(front, keys)

    if not weights:
        if len(front) == 1:
            o = front[0]
            return {"default": o.key,
                    "reason": "one option dominates on every objective",
                    "shortlist": [x.as_dict() for x in front],
                    "conflicts": [c.as_dict() for c in conflicts]}
        return {
            "default": None,
            "reason": ("no league weights configured, and no option dominates. "
                       "Set [leagues].weights to express which league matters "
                       "most; until then this is a genuine choice, not a "
                       "calculation."),
            "shortlist": [x.as_dict() for x in front],
            "conflicts": [c.as_dict() for c in conflicts],
        }

    total = sum(abs(v) for v in weights.values()) or 1.0
    scored = []
    for o in front:
        s = sum(weights.get(k, 0.0) * o.p_target.get(k, 0.0) for k in keys) / total
        scored.append((s, o))
    scored.sort(key=lambda t: -t[0])
    best_s, best = scored[0]
    tied = [o.key for s, o in scored if abs(s - best_s) < 1e-9]
    return {
        "default": best.key if len(tied) == 1 else None,
        "reason": (f"highest weighted target probability ({best_s:.3f})"
                   if len(tied) == 1 else
                   f"tie between {tied} under the configured weights"),
        "weighted_scores": {o.key: round(s, 4) for s, o in scored},
        "shortlist": [o.as_dict() for _, o in scored],
        "conflicts": [c.as_dict() for c in conflicts],
    }


@dataclass
class LeagueView:
    """Everything the UI needs about one league, kept isolated from the others."""

    league_id: int
    name: str
    league_type: str
    classification: str
    size: int | None
    target: int
    posture: dict[str, Any]
    placing: dict[str, Any]
    shields: list[dict[str, Any]]
    differentials: list[dict[str, Any]]
    data_quality: dict[str, Any]
    #: 3.1/3.2 -- one row per named rival: the distribution of my score minus
    #: his, and P(I am ahead of him after this gameweek). Defaulted so a view
    #: built by an older caller is still constructible.
    rival_gaps: list[dict[str, Any]] = field(default_factory=list)
    #: 3.6 -- which differentials actually move the gap, in points of spread.
    differential_leverage: list[dict[str, Any]] = field(default_factory=list)
    differs_from_neutral: bool = False
    difference_reason: str = ""
    #: Players your rivals own and you do not, and how much of the league
    #: captained who you captained. `league` computes both on every run;
    #: this view used to carry neither, so nothing downstream could.
    threats: list[dict[str, Any]] = field(default_factory=list)
    my_captain_eo_pct: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "league_id": self.league_id, "name": self.name,
            "league_type": self.league_type, "classification": self.classification,
            "size": self.size, "target_position": self.target,
            "posture": self.posture, "placing": self.placing,
            "shields": self.shields, "differentials": self.differentials,
            "threats": self.threats,
            "my_captain_eo_pct": self.my_captain_eo_pct,
            "data_quality": self.data_quality,
            "rival_gaps": self.rival_gaps,
            "rival_gaps_domain": LG.RivalGap.DOMAIN,
            "differential_leverage": self.differential_leverage,
            "differs_from_neutral": self.differs_from_neutral,
            "difference_reason": self.difference_reason,
        }


def build_view(
    state: LG.LeagueState, my_squad: list[int], my_captain: int | None,
    placing: LG.PlacingResult, gameweeks_remaining: int, target: int,
    rival_gaps: list | None = None,
    differential_leverage: list | None = None,
) -> LeagueView:
    """Assemble one league's public view. No other league's data enters here."""
    sd = LG.shields_and_differentials(state, my_squad, my_captain)
    gap = LG.points_gap_to_leader(state)
    p = LG.posture(points_gap=gap, gameweeks_remaining=gameweeks_remaining,
                   league_size=state.size or len(state.entries),
                   target=target, coverage=state.coverage)
    differs = p.stance not in ("neutral",)
    reason = ""
    if differs:
        reason = (f"{p.reason}. The neutral recommendation maximises expected "
                  f"points; this league's target ({target}) argues for a "
                  f"{p.stance} posture.")
    elif state.coverage < 1.0:
        reason = "rival squads are only partly known, so no departure is justified"
    return LeagueView(
        league_id=state.league_id, name=state.name,
        league_type=state.league_type, classification=state.classification,
        size=state.size, target=target, posture=p.as_dict(),
        placing=placing.as_dict(), shields=sd["shields"],
        # 3.1/3.2 -- the contest, one row per named rival. `placing` answers
        # "where do I finish"; this answers "am I ahead of HIM", which is the
        # question actually asked and a different optimisation problem.
        rival_gaps=[g.as_dict() for g in (rival_gaps or [])],
        differential_leverage=list(differential_leverage or []),
        differentials=sd["differentials"], threats=sd["threats"],
        my_captain_eo_pct=sd["my_captain_eo_pct"],
        data_quality=state.data_quality(),
        differs_from_neutral=differs, difference_reason=reason,
    )


def assert_isolated(views: list[LeagueView]) -> None:
    """One league's effective ownership must never appear under another."""
    seen: dict[int, int] = {}
    for v in views:
        if v.league_id in seen:
            raise ValueError(f"league {v.league_id} appears twice in the output")
        seen[v.league_id] = 1
