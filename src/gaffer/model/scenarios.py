"""Fixture-level scenario engine (T-16).

The previous simulator drew every player independently. Two Arsenal centre-backs
got a *joint* clean sheet 52.6% of the time when the true answer is 72.6% — it is
one event, not two. Measured correlation between teammates was -0.0032. That
makes squad variance, captain swings and any probability-of-placing estimate
built on top of it meaningless.

Here a match is drawn ONCE and every player in it is conditioned on that draw.
The coupling device is **one uniform per attacking side**, and every quantity
that depends on how many goals that side scores is a MONOTONE function of it:

    U               ~ Uniform(0, 1),  one draw per side per scenario
    team goals      = PoissonInvCDF(U; lambda_lineup)   <- that side's attack
    clean sheet     = U < p_cs                          <- the opposing side
    goals conceded  = PoissonInvCDF(U; lam_conceded x mins_frac)

So goals, clean sheets and goals conceded read the SAME draw and are ordered by
it: teammates share one clean sheet, an opponent's attackers are negatively
correlated with it, and a defender cannot be paid a clean sheet in a scenario
that also charges him for a goal — that is proved below, not tuned.

A17 — the coupling was already here; the ARITHMETIC was not. This module is the
third reading of ``projection.fixture_rates``, after ``_project_one_fixture``
(the point estimate) and ``model.simulate`` (the per-player distribution), and it
disagreed with both by up to 2.54 points on the live 2026/27 GW3 artifact —
Raya published at 5.12 beside a scenario mean of 2.58. 418 of 516 players were
outside the 5-sigma sampling tolerance ``simulate.sampling_tolerance`` allows.
The bias was positional and NEGATIVE: -0.18 a defender, -0.15 a keeper, -0.05 a
midfielder. Six separate mechanisms, all of the same family — a term drawn at a
rate that already carries the minutes, and then gated on the minutes a second
time, or drawn from a second formula for a quantity the projection already names:

  * the **clean sheet** and the **long appearance point** were gated on
    ``started``. The projection pays both on ``p60``, which is not ``p_start``;
  * **goals conceded** was the opponent's whole-match goal count gated on
    ``started``, where the projection integrates ``Poisson(lam x mins_frac)``;
  * **DEFCON** and the four discipline terms were gated on ``played`` while
    drawn at their unconditional per-fixture rates, which already contain
    ``mins_frac`` — the bench universe counted twice;
  * **saves** were drawn at ``save_units x 3 + 0.9 x conceded``. ``save_units``
    is ``E[floor(X/3)]``, not a lambda, so multiplying it by three does not
    recover one; the ``0.9 x conceded`` term then made a busy keeper and a clean
    sheet mutually exclusive;
  * **bonus** was a second BPS proxy (``clip(rint(0.9g + 0.6a + 0.4cs), 0, 3)``)
    — the very formula A13 deleted from ``model.simulate`` for being a different
    answer to the same unmeasurable question;
  * **goals and assists** were zeroed for a player drawn as not playing, while
    ``exp_goals`` already carries ``mins_frac``. That is the same double count,
    and it was the one the module had documented as deliberate.

Every term is now drawn in the form that makes its mean the projection's own term
EXACTLY. The three forms are ``model.simulate``'s, and the reasoning there is not
repeated here:

  * LINEAR IN MINUTES — goals, assists, cards, own goals, penalties and the
    historical half of the bonus blend. Scaled by ``sim_min / exp_minutes``,
    whose mean is 1, so a benched universe scores nothing and the marginal is
    untouched. For goals that scaling has to survive the multinomial: the team's
    lambda is recomputed per scenario from the drawn lineup, so a side missing
    its striker really does threaten less, and each player's allocation is
    ``Poisson(exp_goals x scale)`` by the Poisson-splitting identity.
  * GATED ON AN EVENT THE PROJECTION NAMES — the appearance point and the clean
    sheet, both on ``p60``. A 60-minute event is drawn nested inside the
    start/cameo split with ``features.P60_GIVEN_*`` and reproduces ``p60``
    exactly, rare hour-long substitute included.
  * THRESHOLDED, AND THEREFORE NOT LINEAR IN MINUTES — goals conceded, saves and
    DEFCON, each drawn at the projection's own unconditional per-fixture rate and
    NOT re-gated on the appearance draw.

**Why the clean sheet is a threshold on U rather than ``goals == 0``.** It used
to be the latter, which is the more obviously coherent thing to write and gave
the wrong number. The opposing side's lambda here is the sum of its players'
expected goals; the projection's ``p_cs`` comes from ``ctx.expected_conceded``,
a team-strength estimate of the same quantity. **They are two different numbers**
— on the live GW3 artifact they agree in the mean (1.64 vs 1.61) and disagree by
up to 0.54 in probability on individual sides, which is most of a clean sheet.
No reading can be exact against both, so each term is made exact against its own
projection term and the residual is stated rather than hidden: a clean sheet is
paid in the low-U tail, the opponent's goals are drawn from the same U, and the
band where the two disagree is measured by
``tests/test_scenarios.py::test_a_clean_sheet_rarely_contradicts_the_goal_draw``.
Reconciling the two lambdas is a projection change, not a sampler change.

What IS guaranteed, for every lambda and every ``mins_frac``: a scenario that
pays a clean sheet never also deducts for goals conceded. A clean sheet needs
``U < exp(-lam)``; a deduction needs the drawn conceded count to reach 2, i.e.
``U > exp(-lam m)(1 + lam m)``; and ``exp(-lam m)(1 + lam m)`` is decreasing in
``m`` with minimum ``exp(-lam)(1 + lam) > exp(-lam)`` at ``m = 1``. The two bands
cannot overlap.

Marginals are preserved by construction: ``E[player goals] = exp_goals``, and the
mean of every other term is the matching term in ``_project_one_fixture``.
Everything is vectorised over scenarios, and seeded, so a league's rivals can be
scored under exactly the same simulated football.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from gaffer import config
from gaffer.model import features as F
from gaffer.model import projection

#: Bumped when the scenario construction changes. 1.1 is A17: every term now
#: reproduces its own projection term in expectation.
SIM_VERSION = "scenarios-1.1"

DEFAULT_SIMS = 2000
DEFAULT_SEED = 20260806

_START_MIN = 82.0
_CAMEO_MIN = 20.0


def _poisson_icdf(u: np.ndarray, lam: np.ndarray | float) -> np.ndarray:
    """``F^-1(u)`` for ``Poisson(lam)``, elementwise and vectorised.

    The inverse CDF, not ``rng.poisson``, is what makes the whole module cohere:
    it is MONOTONE in ``u``, so one uniform per attacking side orders that side's
    goals, the opposing side's clean sheet and every defender's goals-conceded
    count on the same axis. Draw them independently and the module is back to the
    independent sampler it exists to replace.

    ``lam`` broadcasts against ``u``, so a whole fixture's players can be drawn
    against one shared uniform in a single call. The series is truncated where
    the remaining tail is below 1e-12, which for the lambdas in football (a team
    total under 5) is unreachable rather than merely unlikely.
    """
    lam_b, u_b = np.broadcast_arrays(
        np.asarray(lam, dtype=np.float64), np.asarray(u, dtype=np.float64))
    m = float(lam_b.max()) if lam_b.size else 0.0
    cap = int(max(12, math.ceil(m + 8.0 * math.sqrt(m) + 8.0)))
    pmf = np.exp(-lam_b)
    cdf = pmf.copy()
    out = (u_b > cdf).astype(np.int16)
    for k in range(1, cap + 1):
        pmf = pmf * lam_b / k
        cdf = cdf + pmf
        out += (u_b > cdf)
    return out


@dataclass
class ScenarioSet:
    """Per-player points across N shared football scenarios."""

    points: np.ndarray                  # (n_players, n_sims), float32
    player_ids: list[int]
    index: dict[int, int]
    n_sims: int
    seed: int
    meta: dict[str, Any] = field(default_factory=dict)
    #: Measurements about the draw itself. Deliberately NOT merged into
    #: ``as_meta``: the meta block is echoed wholesale into a byte-capped MCP
    #: payload, and a diagnostic that pushes a real answer out of a response is
    #: worse than one you have to ask for.
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def row(self, player_id: int) -> np.ndarray:
        i = self.index.get(player_id)
        if i is None:
            return np.zeros(self.n_sims, dtype=np.float32)
        return self.points[i]

    def squad_points(
        self, starting: list[int], captain: int | None = None,
        bench: list[int] | None = None, captain_multiplier: int = 2,
        bench_boost: bool = False,
    ) -> np.ndarray:
        """Total points for a squad in every scenario.

        The same ScenarioSet must be used for the user and every rival, so that
        a comparison reflects one shared reality rather than two draws.
        """
        total = np.zeros(self.n_sims, dtype=np.float32)
        for pid in starting:
            total += self.row(pid)
        if captain is not None:
            total += self.row(captain) * (captain_multiplier - 1)
        if bench_boost and bench:
            for pid in bench:
                total += self.row(pid)
        return total

    def summary(self, player_id: int) -> dict[str, float]:
        r = self.row(player_id)
        return {
            "mean": float(r.mean()),
            "floor": float(np.percentile(r, 25)),
            "ceiling": float(np.percentile(r, 90)),
            "boom": float((r >= 10).mean() * 100),
            "std": float(r.std()),
        }

    def as_meta(self) -> dict[str, Any]:
        return {
            "sim_version": SIM_VERSION,
            "n_sims": self.n_sims,
            "seed": self.seed,
            "model_version": projection.MODEL_VERSION,
            **self.meta,
        }


@dataclass
class _PlayerRates:
    """One player-fixture's slice of ``projection.fixture_rates``.

    A17 — the rates are carried RAW. The previous version pre-multiplied the six
    per-90 rates by ``mins_frac`` here and then gated the draw on ``played`` in
    the sampler, which is the double count this release removes. Whatever scaling
    a term needs is now applied where the term is drawn, once.
    """

    pid: int
    team_id: int
    position: str
    fixture_key: tuple[int, int, int]     # (gw, team, opponent)
    p_start: float
    p_play: float
    exp_minutes: float
    mins_frac: float
    exp_goals: float
    exp_assists: float
    goal_pts_per: float
    assist_pts_per: float
    p_cs: float
    cs_pts_per: float
    defcon_p: float
    defcon_pts: float
    lam_conceded: float
    saves_lam: float
    yellow: float
    red: float
    og: float
    pen_save: float
    pen_miss: float
    bonus_rate: float


def _collect_rates(
    conn: sqlite3.Connection, gw: int
) -> tuple[list[_PlayerRates], dict[tuple[int, int, int], float]]:
    """Run the production rate model once per player-fixture."""
    ctx = F.TeamContext.build(conn)
    fixtures = F.upcoming_fixtures_by_team(conn, gw, 1)
    # A17 — the denominator for a `starts` tally is the team's own completed
    # FIXTURES, which is what `projection.project` passes. This module used to
    # pass `meta.last_finished_gw`, an event count; the two differ from the first
    # double or blank of the season onward, and a different denominator is a
    # different `p_start`, which is a different everything.
    played_by_team = F.played_fixtures_by_team(conn)
    # 2A -- see the note in `model.simulate`: a reading of `fixture_rates` that
    # omits the recency map computes a different p_start from the published one,
    # and this module is the THIRD reading of that function. Three readings of
    # one rulebook that disagree is the defect A13 and A17 were both about.
    recency_by_player = F.start_recency_by_player(conn)
    rates: list[_PlayerRates] = []
    for p in conn.execute("SELECT * FROM players").fetchall():
        avail = projection._availability(p["status"], p["chance_playing"])
        if avail <= 0:
            continue
        for fx in fixtures.get(p["team_id"], []):
            if fx.gw != gw:
                continue
            r = projection.fixture_rates(
                p, fx, ctx, avail, played_by_team.get(p["team_id"], 0),
                recency_by_player.get(p["id"]))
            rates.append(_PlayerRates(
                pid=p["id"], team_id=p["team_id"], position=r["pos"],
                fixture_key=(fx.gw, p["team_id"], fx.opponent_id),
                p_start=r["p_start"], p_play=r["p_play"],
                exp_minutes=r["exp_minutes"], mins_frac=r["mins_frac"],
                exp_goals=r["exp_goals"], exp_assists=r["exp_assists"],
                goal_pts_per=r["goal_pts_per"], assist_pts_per=r["assist_pts_per"],
                p_cs=r["p_cs"], cs_pts_per=r["cs_pts_per"],
                defcon_p=r["defcon_p_hit"], defcon_pts=r["defcon_pts"],
                lam_conceded=r["lam_conceded"], saves_lam=r["saves_lam"],
                yellow=r["yellow_rate"], red=r["red_rate"], og=r["og_rate"],
                pen_save=r["pen_save_rate"], pen_miss=r["pen_miss_rate"],
                bonus_rate=r["bonus_rate"],
            ))
    # The deterministic lambda for each attacking side. The scenario draw uses a
    # per-universe version of this built from the lineup actually drawn; this one
    # is its mean, and is published for diagnostics.
    lam: dict[tuple[int, int, int], float] = {}
    for r in rates:
        lam[r.fixture_key] = lam.get(r.fixture_key, 0.0) + r.exp_goals
    return rates, lam


def _contradiction_rate(
    p_no_goals: dict[tuple[int, int, int], np.ndarray],
    cs_prob: dict[tuple[int, int, int], float],
) -> dict[str, float]:
    """How often a clean sheet lands in a scenario where the attack scored.

    The one residual incoherence, measured instead of asserted away. A clean
    sheet is paid when ``u < p_cs``; the attack facing it draws no goals when
    ``u < exp(-lambda_lineup)``. Both read the same uniform, so the contradiction
    is exactly the band between the two thresholds, ``E[max(0, p_cs - e^-lam)]``,
    and it is non-zero only because ``p_cs`` and ``lambda_lineup`` are two
    different estimates of one quantity (see the module docstring).
    """
    gaps: list[float] = []
    for key in cs_prob:
        gw_, team, opp = key
        opp_p0 = p_no_goals.get((gw_, opp, team))
        if opp_p0 is None:      # nobody available to attack us: no goals, no gap
            gaps.append(0.0)
            continue
        gaps.append(float(np.maximum(cs_prob[key] - opp_p0, 0.0).mean()))
    if not gaps:
        return {"mean": 0.0, "max": 0.0, "sides": 0}
    return {"mean": round(float(np.mean(gaps)), 4),
            "max": round(float(np.max(gaps)), 4), "sides": len(gaps)}


def simulate(
    conn: sqlite3.Connection, gw: int, n_sims: int = DEFAULT_SIMS,
    seed: int = DEFAULT_SEED,
) -> ScenarioSet:
    """Draw ``n_sims`` shared football scenarios for gameweek ``gw``."""
    rng = np.random.default_rng(seed)
    rates, _lam = _collect_rates(conn, gw)
    if not rates:
        return ScenarioSet(np.zeros((0, n_sims), np.float32), [], {}, n_sims, seed,
                           {"fixtures": 0, "note": "no fixtures in this gameweek"})

    by_fixture: dict[tuple[int, int, int], list[_PlayerRates]] = {}
    for r in rates:
        by_fixture.setdefault(r.fixture_key, []).append(r)

    # One uniform per attacking side, shared by everyone in that match. Both
    # directions of every fixture get one — a side whose players are all
    # unavailable still has to supply its opponent with an attack to keep out.
    keys = set(by_fixture)
    for gw_, team, opp in list(keys):
        keys.add((gw_, opp, team))
    shared_u: dict[tuple[int, int, int], np.ndarray] = {
        key: rng.random(n_sims) for key in sorted(keys)
    }

    pids = sorted({r.pid for r in rates})
    index = {p: i for i, p in enumerate(pids)}
    points = np.zeros((len(pids), n_sims), dtype=np.float32)
    # P(this side draws no goals) and the clean-sheet probability the projection
    # gives the side facing it — the two estimates of one quantity, kept so the
    # gap between them is published rather than assumed away.
    p_no_goals: dict[tuple[int, int, int], np.ndarray] = {}
    cs_prob: dict[tuple[int, int, int], float] = {}

    for key in sorted(by_fixture):
        group = by_fixture[key]
        gw_, team, opp = key
        u_att = shared_u[key]                       # this side's attack
        u_opp = shared_u[(gw_, opp, team)]          # the attack we face
        k = len(group)

        # --- who plays: one draw per player, but the football is shared -------
        u = rng.random((k, n_sims))
        p_start = np.array([r.p_start for r in group])[:, None]
        p_play = np.array([r.p_play for r in group])[:, None]
        started = u < p_start
        cameo = (~started) & (u < p_play)
        played = started | cameo
        sim_min = np.where(started, _START_MIN, np.where(cameo, _CAMEO_MIN, 0.0))
        exp_min = np.array([r.exp_minutes or 1.0 for r in group])[:, None]
        scale = sim_min / exp_min      # mean 1 by construction
        mins_90 = sim_min / 90.0       # mean == mins_frac

        # --- the hour --------------------------------------------------------
        # The projection pays the long appearance point and the clean sheet on
        # `p60`, and `p60` is NOT `p_start`. Nesting the draw inside the
        # start/cameo split with the same `features.P60_GIVEN_*` rates
        # reproduces `p60` to the digit.
        v = rng.random((k, n_sims))
        p60_start = np.array(
            [F.P60_GIVEN_START.get(r.position, 1.0) for r in group])[:, None]
        p60_sub = np.array(
            [F.P60_GIVEN_SUB.get(r.position, 0.0) for r in group])[:, None]
        hour = np.where(started, v < p60_start, np.where(cameo, v < p60_sub, False))

        # --- the team's goals, from the lineup actually drawn -----------------
        # `w` is each player's expected goals scaled by the minutes he drew, so
        # the side's lambda falls when its forwards are benched. Splitting a
        # Poisson(sum w) multinomially by w/sum(w) makes each player's own count
        # Poisson(w_i) — his projected expected goals in the mean, and zero in a
        # universe he sat out. That identity is the whole reason the team total
        # can be one shared number without biasing anybody's marginal.
        w = np.maximum(np.array([r.exp_goals for r in group])[:, None] * scale, 0.0)
        lam_u = w.sum(axis=0)
        team_goals = _poisson_icdf(u_att, lam_u)
        p_no_goals[key] = np.exp(-lam_u)

        alloc = np.zeros((k, n_sims), dtype=np.int16)
        remaining_n = team_goals.copy()
        remaining_w = lam_u.copy()
        for i in range(k):
            if i == k - 1:
                alloc[i] = remaining_n
                break
            p_i = np.clip(
                np.divide(w[i], remaining_w, out=np.zeros(n_sims),
                          where=remaining_w > 0), 0.0, 1.0)
            drawn = rng.binomial(remaining_n, p_i).astype(np.int16)
            alloc[i] = drawn
            remaining_n = remaining_n - drawn
            remaining_w = np.maximum(remaining_w - w[i], 0.0)

        # Assists ride the same drawn team total — they are not capped by it, but
        # a side that failed to score did not create many. `team_goals / lam_u`
        # has mean 1 given the lineup, so the marginal is again exact.
        ratio = np.divide(team_goals, lam_u, out=np.zeros(n_sims), where=lam_u > 0)
        a_lam = np.maximum(
            np.array([r.exp_assists for r in group])[:, None] * scale * ratio, 0.0)
        assists = rng.poisson(a_lam).astype(np.int16)

        # --- the opposing attack, read once for the whole group ---------------
        # Clean sheet on `p60` at the projection's own `p_cs`; goals conceded from
        # the projection's own `lam_conceded x mins_frac`. Both are monotone in
        # the SAME uniform, so they cannot contradict each other (see module
        # docstring) and every teammate shares one clean sheet.
        p_cs = np.array([r.p_cs for r in group])[:, None]
        cs_prob[key] = float(p_cs.max())
        cs_pts_per = np.array([r.cs_pts_per for r in group])[:, None]
        cs_flag = hour & (u_opp[None, :] < p_cs)
        cs_pts = cs_flag * cs_pts_per

        conceded_lam = np.array([
            r.lam_conceded * r.mins_frac if r.position in config.CONCEDED_POSITIONS
            else 0.0 for r in group])[:, None]
        conceded = _poisson_icdf(u_opp[None, :], conceded_lam)
        conceded_pts = ((conceded // config.CONCEDED_PER_PENALTY)
                        * config.CONCEDED_PENALTY)

        # --- appearance -------------------------------------------------------
        appearance = np.where(
            hour, float(config.APPEARANCE_LONG),
            np.where(played, float(config.APPEARANCE_SHORT), 0.0))

        for i, r in enumerate(group):
            row = appearance[i].astype(np.float32)
            row = row + alloc[i] * r.goal_pts_per
            row = row + assists[i] * r.assist_pts_per
            row = row + cs_pts[i]
            row = row + conceded_pts[i]

            # --- saves --------------------------------------------------------
            # Drawn at `saves_lam`, the Poisson `features.expected_floor_div`
            # integrates, so the mean is `save_units` exactly. Independent of the
            # goals draw: the old `save_units x 3 + 0.9 x conceded` both misread
            # `save_units` as a lambda and made a clean sheet and a busy keeper
            # mutually exclusive, which is the opposite of a goalkeeper's job.
            if r.saves_lam > 0:
                saves = rng.poisson(r.saves_lam, n_sims)
                row = row + (saves // config.SAVES_PER_POINT) * config.SAVE_POINTS

            # --- DEFCON -------------------------------------------------------
            # No `played` gate: `defcon_p_hit` is already an unconditional
            # per-fixture probability built on minutes-scaled volume.
            dc_pts: np.ndarray | float = 0.0
            if r.defcon_pts and r.defcon_p > 0:
                dc_pts = (rng.random(n_sims) < r.defcon_p) * r.defcon_pts
                row = row + dc_pts

            # --- discipline and rare events -----------------------------------
            # Linear in minutes, so a per-90 rate times the minutes actually
            # played has the projection's `rate * mins_frac` as its mean. Drawn
            # as counts rather than a clipped Bernoulli — clipping a rate above 1
            # would silently shave the mean.
            def _rare(rate: float, m90: np.ndarray = mins_90[i]) -> np.ndarray | float:
                if rate <= 0:
                    return 0.0
                return rng.poisson(np.maximum(rate * m90, 0.0))

            row = row + _rare(r.yellow) * config.YELLOW_POINTS
            row = row + _rare(r.red) * config.RED_POINTS
            row = row + _rare(r.og) * config.OWN_GOAL_POINTS
            row = row + _rare(r.pen_miss) * config.PENALTY_MISS_POINTS
            if r.position == "GKP":
                row = row + _rare(r.pen_save) * config.PENALTY_SAVE_POINTS

            # --- bonus ---------------------------------------------------------
            # `projection.bonus_points` is the projection's OWN formula, evaluated
            # on the drawn returns instead of on their expectations. It is linear
            # in all four inputs, so the mean of what comes back is the published
            # bonus. Then randomised-rounded, because FPL bonus is a whole number
            # and the artifact's floor/ceiling read as whole points: floor(b) plus
            # a Bernoulli on the fraction is integer-valued AND mean-preserving,
            # where a plain round is neither, and the old 0-3 clip was neither
            # twice over.
            bonus_lam = projection.bonus_points(
                r.position, alloc[i], assists[i], dc_pts, cs_pts[i], r.cs_pts_per,
                r.bonus_rate * mins_90[i],
                # Switched on the per-PLAYER quantity the projection switches on,
                # not on the per-universe draw, which would flip the branch for a
                # benched universe and lose the mean.
                bool(r.bonus_rate * r.mins_frac > 0),
            )
            lo = np.floor(bonus_lam)
            row = row + lo + (rng.random(n_sims) < (bonus_lam - lo))

            # A double gameweek adds a second fixture for the same player.
            points[index[r.pid]] += row.astype(np.float32)

    return ScenarioSet(
        points=points, player_ids=pids, index=index, n_sims=n_sims, seed=seed,
        meta={
            "gameweek": gw,
            "fixtures": len(by_fixture),
            "players": len(pids),
            "assumptions": [
                "One uniform per attacking side: its goals, the opposing clean "
                "sheet and every conceded count are monotone reads of one draw.",
                "Every term's mean is the projection's own term, so the scenario "
                "mean IS the published expected points up to sampling error.",
                "The goal lambda is rebuilt each scenario from the lineup drawn, "
                "so a benched player scores none.",
                "A clean sheet is never also charged for goals conceded, though "
                "p_cs and the opposing lambda are two different estimates of one "
                "quantity, so it can coincide with a non-zero drawn total.",
                "Bonus is the projection's own proxy on the drawn returns, "
                "randomised-rounded; not a BPS model.",
            ],
        },
        diagnostics={
            "clean_sheet_contradiction": _contradiction_rate(p_no_goals, cs_prob),
        },
    )
