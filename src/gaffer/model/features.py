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


# --- DEFCON -----------------------------------------------------------------
# All three constants below were fitted or measured on 2025-26. That is not a
# preference: it is the only season in the archive that carries a
# `defensive_contribution` column at all, and the only season in which DEFCON
# scored points, so it is the only season capable of measuring any of this.

#: Positional prior for defensive contributions per 90, the DEFCON analogue of
#: XGI_PRIOR. Measured as the minutes-weighted league rate over players with at
#: least 900 minutes in 2025-26: DEF 7.66, MID 8.62, FWD 4.73, rounded.
#:
#: Restricting to regulars IS the measurement. Taken over every player
#: regardless of minutes the DEF mean is 8.55, because the under-90-minute band
#: averages 22.5 per 90 — three contributions divided by four minutes, which is
#: precisely the artifact the shrinkage exists to remove. Letting it into the
#: prior would launder the defect into the thing meant to correct it.
#:
#: GKP is a measured zero, not a placeholder: goalkeepers recorded no defensive
#: contributions at all across 68,395 keeper minutes. `DEFCON_THRESHOLD["GKP"]`
#: is 999 so the branch is never entered anyway, but the number is honest.
#:
#: Checked against the case it exists for. Over rows where the player had no
#: season-to-date minutes yet — a genuine unknown — the prior predicts 0.078 for
#: defenders against an actual 0.070, and 0.021 for midfielders against an
#: actual 0.026. The shipped behaviour there was a flat 0.000, which is not
#: caution: it asserts that a player about whom nothing is known is incapable of
#: making a defensive contribution.
DEFCON_PRIOR = {"GKP": 0.0, "DEF": 7.7, "MID": 8.6, "FWD": 4.7}

#: Minutes at which a player's own DEFCON rate is half-trusted against the
#: target above. Deliberately half of XGI_SHRINK_K's 600: defensive
#: contributions arrive about ten a match where xG arrives a third of one, so
#: the per-90 rate converges far faster per minute played and shrinking it as
#: hard as an xG rate would discard real evidence.
#:
#: Fitted jointly with DEFCON_NB_DISPERSION on GW2-19 of 2025-26 and reported on
#: GW20-38 (protocol below). The TRAIN surface is flat — every (k, r) in
#: k in [180,420] x r in [12,30] lands within 0.0006 logloss of the joint
#: minimum at (300, 20) — so this is a basin, not a knife edge, and the third
#: significant figure would be false precision.
DEFCON_SHRINK_K = 300.0

#: Negative-binomial dispersion (size r) for defensive-action counts. CBIT/CBIRT
#: are over-dispersed (game-to-game variance > mean), so a NegBin threshold model
#: fits the "does he hit 10/12?" question better than Poisson. Smaller r = fatter
#: tail.
#:
#: This was 6.0, and the comment that shipped with it said "~6 is a mild,
#: defensible over-dispersion". That was a GUESS, and it had to be: DEFCON did
#: not score before 2025-26, so no held-out sample existed to check it against.
#:
#: One exists now. Held-out protocol, stated because the answer depends on it:
#: features are the leak-free season-to-date aggregates from `histdata`
#: (`shift(1)`, so gameweek G is predicted from GW1..G-1 only); TRAIN is GW2-19
#: (5,126 rows, 13.34% hit rate) and TEST is GW20-38 (5,325 rows, 13.30%); the
#: constants are chosen on TRAIN and every number below is TEST. Scoring uses
#: the player's ACTUAL minutes in the target gameweek, so this measures the rate
#: model and not the minutes model.
#:
#:     variant                    TRAIN ll   TEST ll   TEST Brier   TEST meanP
#:     raw rate, r=6.0 (shipped)   0.31817   0.27106      0.08606       0.1548
#:     shrunk k=300, r=6.0         0.27804   0.26779      0.08573       0.1504
#:     shrunk k=300, r=20.0        0.27425   0.26084      0.08433       0.1374
#:                                                          actual hit: 0.1330
#:
#: The old value over-predicted across the entire low band and was honest only
#: at the top, which is why looking at the ball-winners never caught it. TEST
#: predicted-over-actual by band, shipped then new: 2.25x -> 1.56x below 0.05,
#: 2.14x -> 1.54x in [0.05,0.10), 1.59x -> 1.16x in [0.10,0.20), 1.34x -> 1.07x
#: in [0.20,0.30), 1.06x -> 0.96x in [0.30,0.45), 0.99x -> 0.96x above. The top
#: band now reads about 4% low; that is the price of the corrections below it.
#:
#: ORDER MATTERS, and this is the trap the exercise turns on. Fitting r on the
#: UNSHRUNK rates the model used to feed it picks r=4.0 on TRAIN — the wrong
#: direction entirely — because a fatter tail is the only way one dispersion
#: parameter can absorb rates like 90.0 per 90. That same variant's TEST optimum
#: is r≈20, so the two halves flatly disagree and the in-sample answer is an
#: artifact of the bug rather than a property of the data. r must be fitted
#: AFTER the shrinkage and on shrunk inputs, or it is fitted to compensate for a
#: defect that has since been removed.
#:
#: What did not work: fitting on the whole season at once. Logloss then falls
#: monotonically out to r=120 with no interior minimum, which reads as "these
#: counts are barely over-dispersed" and is simply the model memorising. The
#: train/test split is what makes the question answerable, not a refinement of it.
DEFCON_NB_DISPERSION = 20.0


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


def expected_floor_div(lam: float, divisor: int, max_k: int = 40) -> float:
    """E[floor(X / divisor)] for X ~ Poisson(lam).

    FPL's goals-conceded and saves rules both bucket a count: one point per
    ``divisor`` occurrences. Using ``floor(E[X]/divisor)`` would be wrong — the
    expectation of a floor is not the floor of an expectation — so sum the tail
    probabilities instead: E[floor(X/d)] = sum_{j>=1} P(X >= j*d).
    """
    if lam <= 0 or divisor <= 0:
        return 0.0
    # cdf[k] = P(X <= k)
    cdf, pmf, total = [], math.exp(-lam), 0.0
    for k in range(max_k + 1):
        total += pmf
        cdf.append(min(total, 1.0))
        pmf *= lam / (k + 1)
    out = 0.0
    j = 1
    while j * divisor <= max_k:
        out += 1.0 - cdf[j * divisor - 1]
        j += 1
    return out


# Pre-season, FPL only ships a coarse 1-5 team strength, so fixture ratios cluster
# near 1.0 and every matchup looks average — premiums vs weak sides never get the
# boost they deserve and trap fixtures aren't punished. Raise the coarse ratio to
# this power to de-compress the spread (a soft opener reads soft, a tough one tough).
# Only applied in the coarse regime; the in-season 1000-scale ratings are already
# well-spread and left linear.
STRENGTH_GAMMA = 1.7
STRENGTH_CLAMP = (0.5, 1.85)


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
    coarse: bool = False  # True when using the pre-season 1-5 strength scale

    def _spread(self, ratio: float) -> float:
        """De-compress a strength ratio in the coarse pre-season regime."""
        return ratio**STRENGTH_GAMMA if self.coarse else ratio

    @classmethod
    def from_ratings(
        cls,
        att_home: dict[int, float],
        att_away: dict[int, float],
        def_home: dict[int, float],
        def_away: dict[int, float],
        team_xgc: dict[int, float] | None = None,
        league_xgc: float | None = None,
    ) -> TeamContext:
        """Build a context from explicit ratings instead of the live DB.

        Same class, same ``attack_multiplier`` / ``expected_conceded`` / gamma /
        clamp — only the data source differs. This is what lets the backtest
        score the *shipped* fixture model rather than a reimplementation of it.
        """
        ctx = cls()
        ctx.att_home = dict(att_home)
        ctx.att_away = dict(att_away)
        ctx.def_home = dict(def_home)
        ctx.def_away = dict(def_away)
        ctx.team_xgc = dict(team_xgc or {})
        vals = list(ctx.att_home.values()) + list(ctx.att_away.values())
        ctx.league_att = sum(vals) / len(vals) if vals else 1.0
        dvals = list(ctx.def_home.values()) + list(ctx.def_away.values())
        ctx.league_def = sum(dvals) / len(dvals) if dvals else 1.0
        if league_xgc is not None:
            ctx.league_xgc = league_xgc
        elif ctx.team_xgc:
            ctx.league_xgc = sum(ctx.team_xgc.values()) / len(ctx.team_xgc)
        # Identical regime test to build(): the coarse pre-season scale is small.
        ctx.coarse = ctx.league_att < 100
        return ctx

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
        # Coarse regime = the small 1-5 pre-season scale (fine ratings are ~1000s).
        ctx.coarse = ctx.league_att < 100
        return ctx

    def attack_multiplier(self, opponent_id: int, at_home: bool) -> float:
        """Boost attacking output vs weak defences, damp vs strong ones.

        The opponent defends at *their* venue: if our player is away, the
        opponent is at home.
        """
        opp_def = self.def_home[opponent_id] if not at_home else self.def_away[opponent_id]
        ratio = self.league_def / opp_def if opp_def else 1.0
        mult = self._spread(ratio) * (1.08 if at_home else 0.94)  # small home edge
        return clamp(mult, *STRENGTH_CLAMP)

    def expected_conceded(self, team_id: int, opponent_id: int, at_home: bool) -> float:
        """Expected goals conceded by ``team_id`` this fixture (for clean sheets)."""
        base = self.team_xgc.get(team_id, self.league_xgc)
        opp_att = self.att_away[opponent_id] if at_home else self.att_home[opponent_id]
        ratio = opp_att / self.league_att if self.league_att else 1.0
        mult = self._spread(ratio) * (0.90 if at_home else 1.12)  # harder CS away
        return max(0.12, base * mult)


@dataclass
class Fixture:
    gw: int
    opponent_id: int
    at_home: bool
    fdr: int


def played_fixtures_by_team(conn: sqlite3.Connection) -> dict[int, int]:
    """Map team_id -> how many fixtures that team has actually completed.

    Deliberately not the number of gameweeks elapsed, which is what
    ``meta.last_finished_gw`` counts. The two differ whenever the calendar is not
    one-fixture-per-team-per-event, which is most of a season:

    * a **double** gives a team two fixtures in one event, so a player who
      started four of his team's five fixtures scores 4/4 = 1.00 against an event
      count and 4/5 = 0.80 against a fixture count;
    * a **blank** gives none, so the same player is punished for a match that was
      never played.

    ``starts`` is a fixture-level count, so the denominator must be one too.
    """
    out: dict[int, int] = {}
    for r in conn.execute(
        "SELECT team_h, team_a FROM fixtures WHERE finished=1"
    ):
        out[r["team_h"]] = out.get(r["team_h"], 0) + 1
        out[r["team_a"]] = out.get(r["team_a"], 0) + 1
    return out


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
