"""The season's scoring table is read from the API, not assumed.

`GOAL_POINTS["GKP"]` sat at 6 for a whole pre-season of a campaign in which a
goalkeeper's goal is worth 10, because a hard-coded rule agrees with itself and
nothing could see the difference. These tests are the thing that now can.
"""

from __future__ import annotations

import pytest

from gaffer import config, rules

# Shaped exactly like the live 2026/27 `bootstrap.game_config`.
LIVE_SCORING = {
    "long_play": 2, "short_play": 1,
    "goals_scored": {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4},
    "clean_sheets": {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0},
    "goals_conceded": {"GKP": -1, "DEF": -1, "MID": 0, "FWD": 0},
    "defensive_contribution": {"GKP": 0, "DEF": 2, "MID": 2, "FWD": 2},
    "assists": 3, "saves": 1, "bonus": 1,
    "penalties_saved": 5, "penalties_missed": -2,
    "yellow_cards": -1, "red_cards": -3, "own_goals": -2,
    "mng_win": {"GKP": 0, "DEF": 0, "MID": 0, "FWD": 0}, "mng_loss": 0,
}


def bootstrap(scoring=None, rules_block=None, settings=None):
    out = {}
    if settings is not None:
        out["game_settings"] = settings
    gc = {}
    if scoring is not None:
        gc["scoring"] = scoring
    if rules_block is not None:
        gc["rules"] = rules_block
    if gc:
        out["game_config"] = gc
    return out


def test_the_live_table_matches_what_gaffer_models():
    """The regression that matters: config and the API agree, today."""
    assert rules.compare(LIVE_SCORING) == []
    record = rules.verify(bootstrap(LIVE_SCORING))
    assert record["status"] == rules.STATUS_VERIFIED
    assert record["source"] == rules.SOURCE_API


def test_a_goalkeeper_goal_is_worth_ten():
    """The specific constant that was wrong. Pinned so it cannot regress."""
    assert config.GOAL_POINTS["GKP"] == 10
    assert LIVE_SCORING["goals_scored"]["GKP"] == config.GOAL_POINTS["GKP"]


def test_drift_is_fatal_rather_than_silent():
    drifted = {**LIVE_SCORING, "goals_scored": {**LIVE_SCORING["goals_scored"], "MID": 6}}
    with pytest.raises(rules.ScoringRuleDrift) as exc:
        rules.verify(bootstrap(drifted))
    assert any("goal points (MID)" in d for d in exc.value.drift)
    # The message has to be actionable, not just loud.
    assert "gaffer.config" in str(exc.value)
    assert rules.DRIFT_OVERRIDE_ENV in str(exc.value)


def test_every_modelled_rule_is_actually_compared():
    """A check that silently skips a rule is worse than no check."""
    for key, bad in (
        ("assists", 4), ("saves", 2), ("yellow_cards", -2), ("red_cards", -4),
        ("own_goals", -3), ("penalties_saved", 6), ("penalties_missed", -3),
        ("long_play", 3), ("short_play", 2),
    ):
        assert rules.compare({**LIVE_SCORING, key: bad}), f"{key} is unchecked"
    for key in ("goals_scored", "clean_sheets", "goals_conceded",
                "defensive_contribution"):
        broken = {**LIVE_SCORING[key], "DEF": LIVE_SCORING[key]["DEF"] + 1}
        assert rules.compare({**LIVE_SCORING, key: broken}), f"{key} is unchecked"


def test_the_assistant_manager_chip_coming_back_is_drift():
    """All mng_* keys are zero this season and Gaffer models none of them."""
    revived = {**LIVE_SCORING, "mng_win": {"GKP": 0, "DEF": 0, "MID": 0, "FWD": 6}}
    drift = rules.compare(revived)
    assert any("Assistant Manager" in d for d in drift)


def test_the_override_records_the_drift_instead_of_hiding_it(monkeypatch):
    monkeypatch.setenv(rules.DRIFT_OVERRIDE_ENV, "1")
    drifted = {**LIVE_SCORING, "assists": 4}
    record = rules.verify(bootstrap(drifted))
    assert record["status"] == rules.STATUS_DRIFT_ALLOWED
    assert record["drift"], "the drift must still be reported, not swallowed"


def test_an_api_without_game_config_is_unverified_not_fatal():
    record = rules.verify(bootstrap())
    assert record["status"] == rules.STATUS_UNVERIFIED
    assert record["source"] == rules.SOURCE_ABSENT
    assert record["drift"] == []


def test_squad_rules_prefer_game_config_over_the_legacy_block():
    merged = rules.parse_rules(bootstrap(
        rules_block={"squad_total_spend": 1010, "squad_team_limit": 3},
        settings={"squad_total_spend": 1000, "transfers_cap": 20}))
    assert merged["squad_total_spend"] == 1010, "game_config.rules wins"
    assert merged["transfers_cap"] == 20, "legacy-only keys survive"


def test_unpublished_divisors_are_named_rather_than_assumed_verified():
    """Saves-per-3 and conceded-per-2 are not in the payload. Say so."""
    joined = " ".join(rules.UNVERIFIABLE)
    assert "saves" in joined and "conceded" in joined and "DEFCON" in joined
