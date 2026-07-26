"""Feature helpers for the projection model.

Everything here is deliberately transparent and cheap to reason about — the
product principle is *trust by transparency*, so no black boxes.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

# Priors for per-90 attacking rate when a player has little/no history, by
# position. Deliberately modest so unknowns don't float to the top.
XGI_PRIOR = {"GKP": 0.02, "DEF": 0.08, "MID": 0.20, "FWD": 0.35}
# Shrinkage constant (minutes): rate is half-trusted at this many minutes.
XGI_SHRINK_K = 600.0


def shrink(observed: float, minutes: float, prior: float, k: float = XGI_SHRINK_K) -> float:
    """Empirical-Bayes shrink a per-90 rate toward a prior by sample size.

    A player with few minutes (e.g. 3.6 xGI/90 over 2 minutes) is pulled hard
    back to the prior; a full-season regular is trusted almost entirely.
    """
    if minutes <= 0:
        return prior
    w = minutes / (minutes + k)
    return w * observed + (1.0 - w) * prior


def poisson_pmf(k: int, mu: float) -> float:
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-mu) * mu**k / math.factorial(k)


def poisson_p0(mu: float) -> float:
    """P(no goals) = clean-sheet probability under a Poisson goals model."""
    return math.exp(-mu) if mu > 0 else 1.0


def poisson_sf(threshold: int, mu: float) -> float:
    """P(N >= threshold) for N ~ Poisson(mu)."""
    if threshold <= 0:
        return 1.0
    cdf = sum(poisson_pmf(i, mu) for i in range(threshold))
    return max(0.0, 1.0 - cdf)


# Negative-binomial dispersion (size r) for defensive-action counts. CBIT/CBIRT
# are over-dispersed (game-to-game variance > mean), so a NegBin threshold model
# fits the "does he hit 10/12?" question better than Poisson. Smaller r = fatter
# tail; ~6 is a mild, defensible over-dispersion.
DEFCON_NB_DISPERSION = 6.0


def nbinom_pmf(k: int, mu: float, r: float) -> float:
    """P(X = k) for X ~ NegBin(mean=mu, size=r). variance = mu + mu^2/r."""
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    p = r / (r + mu)
    # C(k+r-1, k) p^r (1-p)^k  — via lgamma for non-integer r
    log_coef = math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
    return math.exp(log_coef + r * math.log(p) + k * math.log(1 - p))


def nbinom_sf(threshold: float, mu: float, r: float = DEFCON_NB_DISPERSION) -> float:
    """P(X >= threshold) for X ~ NegBin(mean=mu, size=r). Used for DEFCON hits."""
    thr = int(math.ceil(threshold))
    if thr <= 0:
        return 1.0
    if mu <= 0:
        return 0.0
    cdf = sum(nbinom_pmf(i, mu, r) for i in range(thr))
    return max(0.0, min(1.0, 1.0 - cdf))


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass
class TeamContext:
    """Precomputed team-level defensive/attacking context for a season."""

    # team_id -> minutes-weighted goals-conceded-per-90 proxy (from player xGC).
    team_xgc: dict[int, float] = field(default_factory=dict)
    # FPL curated strength ratings, for fixture adjustment.
    att_home: dict[int, float] = field(default_factory=dict)
    att_away: dict[int, float] = field(default_factory=dict)
    def_home: dict[int, float] = field(default_factory=dict)
    def_away: dict[int, float] = field(default_factory=dict)
    league_att: float = 1.0
    league_def: float = 1.0
    league_xgc: float = 1.3

    @classmethod
    def build(cls, conn: sqlite3.Connection) -> TeamContext:
        ctx = cls()
        for t in conn.execute(
            "SELECT id, strength_att_home, strength_att_away, "
            "strength_def_home, strength_def_away FROM teams"
        ):
            ctx.att_home[t["id"]] = t["strength_att_home"] or 1000
            ctx.att_away[t["id"]] = t["strength_att_away"] or 1000
            ctx.def_home[t["id"]] = t["strength_def_home"] or 1000
            ctx.def_away[t["id"]] = t["strength_def_away"] or 1000

        # Team goals-conceded-per-90 proxy from defenders'/keepers' xGC.
        for r in conn.execute(
            "SELECT team_id, "
            "SUM(xgc_per_90 * minutes) AS wsum, SUM(minutes) AS msum "
            "FROM players WHERE position IN ('GKP','DEF') AND minutes>0 "
            "GROUP BY team_id"
        ):
            if r["msum"]:
                ctx.team_xgc[r["team_id"]] = r["wsum"] / r["msum"]

        vals = list(ctx.att_home.values()) + list(ctx.att_away.values())
        ctx.league_att = sum(vals) / len(vals) if vals else 1.0
        dvals = list(ctx.def_home.values()) + list(ctx.def_away.values())
        ctx.league_def = sum(dvals) / len(dvals) if dvals else 1.0
        if ctx.team_xgc:
            ctx.league_xgc = sum(ctx.team_xgc.values()) / len(ctx.team_xgc)
        return ctx

    def attack_multiplier(self, opponent_id: int, at_home: bool) -> float:
        """Boost attacking output vs weak defences, damp vs strong ones.

        The opponent defends at *their* venue: if our player is away, the
        opponent is at home.
        """
        opp_def = self.def_home[opponent_id] if not at_home else self.def_away[opponent_id]
        mult = self.league_def / opp_def if opp_def else 1.0
        # Small home advantage for the attacking side.
        mult *= 1.08 if at_home else 0.94
        return clamp(mult, 0.6, 1.7)

    def expected_conceded(self, team_id: int, opponent_id: int, at_home: bool) -> float:
        """Expected goals conceded by ``team_id`` this fixture (for clean sheets)."""
        base = self.team_xgc.get(team_id, self.league_xgc)
        opp_att = self.att_away[opponent_id] if at_home else self.att_home[opponent_id]
        mult = opp_att / self.league_att if self.league_att else 1.0
        mult *= 0.90 if at_home else 1.12  # harder to keep a CS away
        return max(0.15, base * mult)


@dataclass
class Fixture:
    gw: int
    opponent_id: int
    at_home: bool
    fdr: int


def upcoming_fixtures_by_team(
    conn: sqlite3.Connection, from_gw: int, horizon: int
) -> dict[int, list[Fixture]]:
    """Map team_id -> list of upcoming Fixtures within [from_gw, from_gw+horizon).

    Naturally represents blanks (empty list) and doubles (>1 per gw).
    """
    out: dict[int, list[Fixture]] = {}
    rows = conn.execute(
        "SELECT gw, team_h, team_a, fdr_h, fdr_a FROM fixtures "
        "WHERE gw>=? AND gw<? AND finished=0 ORDER BY gw",
        (from_gw, from_gw + horizon),
    )
    for r in rows:
        out.setdefault(r["team_h"], []).append(
            Fixture(r["gw"], r["team_a"], True, r["fdr_h"] or 3)
        )
        out.setdefault(r["team_a"], []).append(
            Fixture(r["gw"], r["team_h"], False, r["fdr_a"] or 3)
        )
    return out
