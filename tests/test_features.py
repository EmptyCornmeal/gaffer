"""Unit tests for the projection math primitives."""

from gaffer.model import features as F


def test_shrink_pulls_small_samples_to_prior():
    # 3.6 xGI/90 over 2 minutes must be pulled almost entirely to the prior.
    v = F.shrink(3.6, minutes=2, prior=0.2)
    assert v < 0.25
    # a full season is trusted almost entirely
    v2 = F.shrink(0.8, minutes=3000, prior=0.2)
    assert v2 > 0.7


def test_poisson_clean_sheet_monotonic():
    # more expected goals conceded -> lower clean-sheet probability
    assert F.poisson_p0(0.5) > F.poisson_p0(1.5) > F.poisson_p0(3.0)
    assert 0 < F.poisson_p0(1.2) < 1


def test_poisson_sf_threshold():
    # P(N>=thr) rises with the rate
    low = F.poisson_sf(10, mu=8)
    high = F.poisson_sf(10, mu=13)
    assert 0 <= low < high <= 1


def test_clamp():
    assert F.clamp(5, 0, 1) == 1
    assert F.clamp(-1, 0, 1) == 0
    assert F.clamp(0.5, 0, 1) == 0.5


# --------------------------------------------------------------------------
# G-L / G-M — the DEFCON constants
# --------------------------------------------------------------------------

def test_the_defcon_prior_is_positional_and_measured():
    """Measured on 2025-26 over players with 900+ minutes, which is the only
    season carrying a `defensive_contribution` column at all. Midfielders make
    more defensive contributions per 90 than defenders (8.6 vs 7.7) because the
    threshold that pays them is higher and the role covers more ground;
    forwards make far fewer. Goalkeepers are a MEASURED zero — not a
    placeholder — across 68,395 keeper minutes."""
    p = F.DEFCON_PRIOR
    assert set(p) == {"GKP", "DEF", "MID", "FWD"}
    assert p["GKP"] == 0.0
    assert p["FWD"] < p["DEF"] < p["MID"]
    # Sanity: a full 90 at the prior must not read as a likely hit for anyone.
    assert F.nbinom_sf(10, p["DEF"], F.DEFCON_NB_DISPERSION) < 0.35
    assert F.nbinom_sf(12, p["MID"], F.DEFCON_NB_DISPERSION) < 0.30


def test_defcon_shrinks_faster_than_xgi_because_the_count_arrives_faster():
    """Defensive contributions arrive about ten a match where xG arrives a third
    of one, so the per-90 rate converges far quicker and half-trusting it at the
    xGI constant would discard real evidence."""
    assert F.DEFCON_SHRINK_K < F.XGI_SHRINK_K


def test_one_contribution_in_one_minute_is_pulled_to_the_prior():
    """The live defect, at the level of the primitive. 90.0 per 90 is one
    defensive contribution in one minute of football."""
    v = F.shrink(90.0, minutes=1, prior=F.DEFCON_PRIOR["DEF"],
                 k=F.DEFCON_SHRINK_K)
    assert v < 8.0
    # ...and a full season is still trusted almost entirely.
    v2 = F.shrink(13.9, minutes=3332, prior=F.DEFCON_PRIOR["MID"],
                  k=F.DEFCON_SHRINK_K)
    assert v2 > 13.0


def test_the_fitted_dispersion_is_the_one_the_threshold_model_uses():
    """`nbinom_sf` defaults to the module constant, so refitting the constant is
    what changes every DEFCON probability in the product. Pinned because a
    default that silently stopped tracking it would be invisible."""
    assert F.DEFCON_NB_DISPERSION == 20.0
    assert F.nbinom_sf(10, 8.0) == F.nbinom_sf(10, 8.0, F.DEFCON_NB_DISPERSION)


def test_a_larger_dispersion_thins_the_tail():
    """r is a size, so a bigger r means LESS over-dispersion and a thinner tail.
    The refit from 6.0 to 20.0 therefore lowers every below-threshold
    probability, which is exactly the over-prediction it was fitted to remove:
    on held-out GW20-38 of 2025-26 the 0.05-0.10 band predicted 2.14x its actual
    rate at 6.0 and 1.54x at 20.0."""
    below = [F.nbinom_sf(12, 6.0, r) for r in (2.0, 6.0, 20.0, 60.0)]
    assert below == sorted(below, reverse=True)
    # Above the mean the ordering reverses: a thin tail concentrates mass.
    above = [F.nbinom_sf(12, 16.0, r) for r in (2.0, 6.0, 20.0, 60.0)]
    assert above == sorted(above)


def test_attack_multiplier_bounds(conn):
    ctx = F.TeamContext.build(conn)
    m = ctx.attack_multiplier(opponent_id=2, at_home=True)
    assert 0.6 <= m <= 1.7
