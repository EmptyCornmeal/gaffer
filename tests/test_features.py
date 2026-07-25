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


def test_attack_multiplier_bounds(conn):
    ctx = F.TeamContext.build(conn)
    m = ctx.attack_multiplier(opponent_id=2, at_home=True)
    assert 0.6 <= m <= 1.7
