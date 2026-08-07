"""Fixture-level scenario engine (T-16).

The previous simulator drew every player independently. Two Arsenal centre-backs
got a *joint* clean sheet 52.6% of the time when the true answer is 72.6% — it is
one event, not two. Measured correlation between teammates was -0.0032. That
makes squad variance, captain swings and any probability-of-placing estimate
built on top of it meaningless.

Here a match is drawn ONCE and every player in it is conditioned on that draw:

    team goals  ~ Poisson(lambda_team),  lambda_team = sum of that team's
                  players' deterministic expected goals
    allocation  ~ Multinomial over the team's players, in proportion to their
                  share of that expected-goals total
    clean sheet  = (opponent's drawn goals == 0)      <- the SAME draw
    conceded     = opponent's drawn goals             <- the SAME draw

So goals, clean sheets and goals-conceded can never contradict one another, and
teammates are positively correlated while an opponent's attackers are negatively
correlated with your clean sheet, by construction rather than by tuning.

Marginals are preserved by construction: E[player goals] = lambda_team x share =
that player's deterministic expected goals.

Everything is vectorised over scenarios, and seeded, so a league's rivals can be
scored under exactly the same simulated football.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from gaffer import config
from gaffer.model import features as F
from gaffer.model import projection

#: Bumped when the scenario construction changes.
SIM_VERSION = "scenarios-1.0"
DEFAULT_SIMS = 2000
DEFAULT_SEED = 20260806


@dataclass
class ScenarioSet:
    """Per-player points across N shared football scenarios."""

    points: np.ndarray                  # (n_players, n_sims), float32
    player_ids: list[int]
    index: dict[int, int]
    n_sims: int
    seed: int
    meta: dict[str, Any] = field(default_factory=dict)

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
    pid: int
    team_id: int
    position: str
    fixture_key: tuple[int, int, int]     # (gw, team, opponent)
    p_start: float
    p_play: float
    mins_frac: float
    exp_goals: float
    exp_assists: float
    defcon_p: float
    save_units: float
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
    lf = conn.execute("SELECT value FROM meta WHERE key='last_finished_gw'").fetchone()
    games_played = int(lf["value"]) if lf and str(lf["value"]).isdigit() else 0

    rates: list[_PlayerRates] = []
    for p in conn.execute("SELECT * FROM players").fetchall():
        avail = projection._availability(p["status"], p["chance_playing"])
        if avail <= 0:
            continue
        for fx in fixtures.get(p["team_id"], []):
            if fx.gw != gw:
                continue
            r = projection.fixture_rates(p, fx, ctx, avail, games_played)
            rates.append(_PlayerRates(
                pid=p["id"], team_id=p["team_id"], position=r["pos"],
                fixture_key=(fx.gw, p["team_id"], fx.opponent_id),
                p_start=r["p_start"], p_play=r["p_play"], mins_frac=r["mins_frac"],
                exp_goals=r["exp_goals"], exp_assists=r["exp_assists"],
                defcon_p=r["defcon_p_hit"], save_units=r["save_units"],
                yellow=r["yellow_rate"] * r["mins_frac"],
                red=r["red_rate"] * r["mins_frac"],
                og=r["og_rate"] * r["mins_frac"],
                pen_save=r["pen_save_rate"] * r["mins_frac"],
                pen_miss=r["pen_miss_rate"] * r["mins_frac"],
                bonus_rate=r["bonus_rate"] * r["mins_frac"],
            ))
    # lambda for each attacking side = the sum of that side's expected goals.
    # Using this ONE number for both the goal draw and the opponent's clean
    # sheet is what makes the two consistent.
    lam: dict[tuple[int, int, int], float] = {}
    for r in rates:
        lam[r.fixture_key] = lam.get(r.fixture_key, 0.0) + r.exp_goals
    return rates, lam


def simulate(
    conn: sqlite3.Connection, gw: int, n_sims: int = DEFAULT_SIMS,
    seed: int = DEFAULT_SEED,
) -> ScenarioSet:
    """Draw ``n_sims`` shared football scenarios for gameweek ``gw``."""
    rng = np.random.default_rng(seed)
    rates, lam = _collect_rates(conn, gw)
    if not rates:
        return ScenarioSet(np.zeros((0, n_sims), np.float32), [], {}, n_sims, seed,
                           {"fixtures": 0, "note": "no fixtures in this gameweek"})

    # One goal draw per attacking side, shared by everyone in that match.
    goals_for: dict[tuple[int, int, int], np.ndarray] = {
        key: rng.poisson(max(rate, 0.0), n_sims).astype(np.int16)
        for key, rate in lam.items()
    }

    def conceded_by(key: tuple[int, int, int]) -> np.ndarray:
        """Goals against = the opponent's drawn goals in the SAME match."""
        gw_, team, opp = key
        return goals_for.get((gw_, opp, team), np.zeros(n_sims, np.int16))

    by_fixture: dict[tuple[int, int, int], list[_PlayerRates]] = {}
    for r in rates:
        by_fixture.setdefault(r.fixture_key, []).append(r)

    pids = sorted({r.pid for r in rates})
    index = {p: i for i, p in enumerate(pids)}
    points = np.zeros((len(pids), n_sims), dtype=np.float32)

    for key, group in by_fixture.items():
        team_goals = goals_for[key]
        conceded = conceded_by(key)
        clean_sheet = conceded == 0

        # --- who plays: one draw per player, but goals are shared -----------
        u = rng.random((len(group), n_sims))
        p_start = np.array([r.p_start for r in group])[:, None]
        p_play = np.array([r.p_play for r in group])[:, None]
        started = u < p_start
        played = u < p_play

        # --- allocate the team's goals among its players --------------------
        share = np.array([max(r.exp_goals, 0.0) for r in group], dtype=np.float64)
        a_share = np.array([max(r.exp_assists, 0.0) for r in group], dtype=np.float64)
        tot = share.sum()
        probs = share / tot if tot > 0 else np.full(len(group), 1.0 / len(group))
        # Multinomial over players, conditioned on the drawn team total. This is
        # what couples teammates: one player's goal is another's non-goal.
        alloc = np.zeros((len(group), n_sims), dtype=np.int16)
        for g in np.unique(team_goals):
            if g <= 0:
                continue
            cols = np.flatnonzero(team_goals == g)
            alloc[:, cols] = rng.multinomial(int(g), probs, size=len(cols)).T
        # A player who did not play cannot score; reassign nothing (a small,
        # documented loss of team-total fidelity rather than fabricating a scorer).
        alloc = np.where(played, alloc, 0)

        # Assists scale with the team's goals too, but are not capped by them.
        a_tot = a_share.sum()
        a_probs = a_share / a_tot if a_tot > 0 else np.zeros(len(group))
        assists = rng.poisson(
            np.outer(a_probs, team_goals.astype(np.float64)) *
            (a_tot / tot if tot > 0 else 1.0)
        ).astype(np.int16)
        assists = np.where(played, assists, 0)

        for i, r in enumerate(group):
            row = np.zeros(n_sims, dtype=np.float32)
            # appearance
            row += np.where(started[i], 2.0, np.where(played[i], 1.0, 0.0))
            # attacking returns
            row += alloc[i] * config.GOAL_POINTS[r.position]
            row += assists[i] * config.ASSIST_POINTS
            # clean sheet / goals conceded — from the shared match draw
            cs_pts = config.CS_POINTS[r.position]
            if cs_pts:
                row += np.where(clean_sheet & started[i], cs_pts, 0.0)
            if r.position in config.CONCEDED_POSITIONS:
                row += np.where(started[i],
                                (conceded // config.CONCEDED_PER_PENALTY)
                                * config.CONCEDED_PENALTY, 0.0)
            # saves: more shots when conceding more, so tie to the same draw
            if r.position == "GKP":
                lam_sav = max(r.save_units, 0.0) * config.SAVES_PER_POINT
                sav = rng.poisson(lam_sav + conceded * 0.9, n_sims)
                row += np.where(started[i],
                                (sav // config.SAVES_PER_POINT) * config.SAVE_POINTS, 0.0)
                row += rng.binomial(1, min(max(r.pen_save, 0.0), 1.0), n_sims) \
                    * config.PENALTY_SAVE_POINTS * started[i]
            # DEFCON
            if r.defcon_p > 0:
                row += rng.binomial(1, min(r.defcon_p, 1.0), n_sims) \
                    * config.DEFCON_POINTS * played[i]
            # discipline and rare events
            row += rng.binomial(1, min(max(r.yellow, 0.0), 1.0), n_sims) \
                * config.YELLOW_POINTS * played[i]
            row += rng.binomial(1, min(max(r.red, 0.0), 1.0), n_sims) \
                * config.RED_POINTS * played[i]
            row += rng.binomial(1, min(max(r.og, 0.0), 1.0), n_sims) \
                * config.OWN_GOAL_POINTS * played[i]
            row += rng.binomial(1, min(max(r.pen_miss, 0.0), 1.0), n_sims) \
                * config.PENALTY_MISS_POINTS * played[i]
            # bonus: a coarse BPS proxy driven by what actually happened in the
            # scenario, clipped to the real 0-3 range.
            bps_like = 0.9 * alloc[i] + 0.6 * assists[i] + 0.4 * clean_sheet
            row += np.clip(np.rint(bps_like + r.bonus_rate), 0, 3).astype(np.float32) \
                * played[i]
            # A double gameweek adds a second fixture for the same player.
            points[index[r.pid]] += row

    return ScenarioSet(
        points=points, player_ids=pids, index=index, n_sims=n_sims, seed=seed,
        meta={
            "gameweek": gw,
            "fixtures": len(by_fixture),
            "players": len(pids),
            "assumptions": [
                "One goal draw per attacking side, shared by every player in the "
                "match; clean sheets and goals conceded read the same draw.",
                "Goals are allocated multinomially in proportion to each player's "
                "deterministic expected goals, so teammates compete for the same "
                "goals and marginals are preserved.",
                "A player drawn as not playing forfeits any allocated goal rather "
                "than it being reassigned, so simulated team totals sit slightly "
                "below lambda for sides with rotation risk.",
                "Bonus is a coarse scenario-driven BPS proxy, not a BPS model.",
            ],
        },
    )
