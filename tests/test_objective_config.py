"""3.8 -- which competition this is for, as runtime state.

`resolve` has always accepted a weighting across leagues and has always been
called with None, so it published a shortlist and the conflicts and refused to
name a winner. Honest, and unusable as a decision.

The order matters and is the whole lesson of 1.13: the objective mathematics
first, then evidence that it changes an answer, then the control. The Planner's
risk stance was hidden precisely because it offered three settings that solved
to identical squads.
"""
from __future__ import annotations

import pytest

from gaffer import config
from gaffer import multileague as ML


def _opts():
    return [
        ML.Option(key="captain:A", label="A", expected_points=60.0,
                  p_target={"1": 0.60, "2": 0.10}),
        ML.Option(key="captain:B", label="B", expected_points=59.0,
                  p_target={"1": 0.50, "2": 0.40}),
    ]


def test_with_no_weighting_no_winner_is_invented():
    """The default, and it must stay the default. With nothing configured
    there is no principled way to trade one league's probability against
    another's, and inventing one is the same error as an inert control."""
    r = ML.resolve(_opts(), None, ["1", "2"])
    assert r["default"] is None
    assert "no league weights configured" in r["reason"]
    assert len(r["shortlist"]) == 2


def test_a_weighting_picks_a_winner_and_the_weighting_decides_which():
    """The evidence that this is a real objective and not a relabelling: the
    same options with different weights give different answers."""
    first = ML.resolve(_opts(), {"1": 1.0, "2": 0.1}, ["1", "2"])
    second = ML.resolve(_opts(), {"1": 0.1, "2": 1.0}, ["1", "2"])
    assert first["default"] == "captain:A"
    assert second["default"] == "captain:B"


def test_the_weighting_is_parsed_from_the_environment():
    got = config._league_weights_from("271619:1,733241:0.3", "test")
    assert got == {271619: 1.0, 733241: 0.3}


def test_a_malformed_weighting_raises_rather_than_defaulting():
    """A weighting nobody chose is exactly the invented winner the resolution
    layer refuses to produce, and it would be harder to notice here."""
    for bad in ("271619", "271619:abc", "271619:-1", 7):
        with pytest.raises(config.ConfigError):
            config._league_weights_from(bad, "test")


def test_an_empty_weighting_is_not_a_weighting():
    assert config._league_weights_from("", "test") == {}
    assert config._league_weights_from({}, "test") == {}


def test_settings_carry_the_weighting_and_its_source(monkeypatch):
    monkeypatch.setenv("GAFFER_LEAGUE_WEIGHTS", "1:2,2:1")
    st = config.Settings.load()
    assert st.league_weights == {1: 2.0, 2: 1.0}
    assert st.sources["league_weights"] == "env:GAFFER_LEAGUE_WEIGHTS"


def test_unset_leaves_the_old_behaviour_exactly(monkeypatch):
    monkeypatch.delenv("GAFFER_LEAGUE_WEIGHTS", raising=False)
    st = config.Settings.load()
    assert st.league_weights == {}
    assert "league_weights" not in st.sources
