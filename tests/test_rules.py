"""The season's scoring table is read from the API, not assumed.

`GOAL_POINTS["GKP"]` sat at 6 for a whole pre-season of a campaign in which a
goalkeeper's goal is worth 10, because a hard-coded rule agrees with itself and
nothing could see the difference. These tests are the thing that now can.
"""

from __future__ import annotations

import re

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


# --------------------------------------------------------------------------
# C5 — "verified" must mean checked, not merely un-contradicted
# --------------------------------------------------------------------------

def test_an_empty_scoring_table_is_not_stamped_verified():
    """C5. `scoring: {}` compares nothing, so it produces no drift — and the run
    was stamped with maximum confidence at the one moment it had no evidence at
    all. Absent data has to be distinguishable from agreeing data."""
    record = rules.verify(bootstrap({}))
    assert record["status"] != rules.STATUS_VERIFIED
    assert record["status"] == rules.STATUS_UNVERIFIED
    assert record["unchecked"], "the rules nobody checked have to be named"


def test_a_truncated_scoring_table_names_what_it_could_not_check():
    """The Friday-afternoon shape: the payload arrives with half the table. The
    half that is there agrees; the half that is missing is not evidence."""
    half = {"long_play": 2, "short_play": 1, "assists": 3}
    record = rules.verify(bootstrap(half))
    assert record["status"] == rules.STATUS_UNVERIFIED
    assert record["drift"] == []
    joined = " ".join(record["unchecked"])
    assert "goals_scored" in joined and "clean_sheets" in joined
    assert "assists" not in joined, "the keys that were there are checked"


def test_a_positional_table_missing_a_position_is_not_fully_checked():
    """Per-position, not per-key: a `goals_scored` block carrying only GKP and
    DEF leaves two of Gaffer's four positions unverified."""
    partial = {**LIVE_SCORING, "goals_scored": {"GKP": 10, "DEF": 6}}
    record = rules.verify(bootstrap(partial))
    assert record["status"] == rules.STATUS_UNVERIFIED
    gap = " ".join(record["unchecked"])
    assert "MID" in gap and "FWD" in gap and "GKP" not in gap


def test_drift_outranks_incompleteness():
    """A payload that is both wrong and incomplete is drift. Evidence of a
    changed rule beats absence of evidence about the others."""
    with pytest.raises(rules.ScoringRuleDrift):
        rules.verify(bootstrap({"goals_scored": {"MID": 6}}))


def test_a_full_table_is_still_verified_with_nothing_outstanding():
    record = rules.verify(bootstrap(LIVE_SCORING))
    assert record["status"] == rules.STATUS_VERIFIED
    assert record["unchecked"] == []


# --------------------------------------------------------------------------
# C16 — the override is consent, and consent has to be explicit
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "False", "false", "FALSE", "0", "no", "off", "OFF", "n", "", "   ",
    "nope", "yess", "true-ish", "2",
])
def test_only_a_recognised_yes_silences_the_drift_check(monkeypatch, value):
    """C16. The old test was `value not in ("", "0", "false", "no")`, so the
    literal string `False` — what `str(bool)` writes, and what a templating
    layer therefore hands you — read as consent to silence a safety check. So
    did `off`, and so did every typo."""
    monkeypatch.setenv(rules.DRIFT_OVERRIDE_ENV, value)
    with pytest.raises(rules.ScoringRuleDrift):
        rules.verify(bootstrap({**LIVE_SCORING, "assists": 4}))


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on", "y"])
def test_the_recognised_yeses_are_still_honoured(monkeypatch, value):
    monkeypatch.setenv(rules.DRIFT_OVERRIDE_ENV, value)
    record = rules.verify(bootstrap({**LIVE_SCORING, "assists": 4}))
    assert record["status"] == rules.STATUS_DRIFT_ALLOWED


def test_an_unrecognised_override_says_it_was_ignored(monkeypatch):
    """Refusing consent silently would leave an operator staring at a drift
    report they thought they had already answered."""
    monkeypatch.setenv(rules.DRIFT_OVERRIDE_ENV, "yess")
    with pytest.raises(rules.ScoringRuleDrift) as exc:
        rules.verify(bootstrap({**LIVE_SCORING, "assists": 4}))
    message = str(exc.value)
    assert "yess" in message
    assert "not" in message.lower() and "consent" in message.lower()


# --- G21: computed, persisted, and actually published ------------------------

def test_verify_always_reports_which_rules_went_unchecked():
    """Its docstring promises the record carries them, in every branch."""
    from gaffer import rules

    for bootstrap in (None,
                      {},
                      {"game_config": {"scoring": {}}}):
        rec = rules.verify(bootstrap)
        assert "unchecked" in rec, f"{bootstrap!r} returned no `unchecked` key"
        assert isinstance(rec["unchecked"], list)

    # A payload covering nothing must name gaps rather than report none.
    rec = rules.verify({"game_config": {"scoring": {}}})
    assert rec["unchecked"], "an empty scoring table left nothing unchecked?"


def test_every_rule_scoring_meta_key_reaches_the_artifact():
    """G21 was a whole class of bug, not one missing line.

    `rules.verify` computed `unchecked`, `ingest` did not persist it, and
    `artifacts` did not export it — so the count of unverified scoring rules
    shipped while the list of *which* ones never did. This asserts the three
    stay in step: anything ingest writes as `rule_scoring_*` must be exported.
    """
    from pathlib import Path

    import gaffer

    root = Path(gaffer.__file__).parent
    ingest_src = (root / "ingest.py").read_text(encoding="utf-8")
    artifact_src = (root / "export" / "artifacts.py").read_text(encoding="utf-8")

    written = set(re.findall(r'set_meta\(\s*conn\s*,\s*"(rule_scoring_[a-z_]+)"',
                             ingest_src))
    assert written, "no rule_scoring_* meta writes found — did ingest move?"

    missing = sorted(k for k in written if f'"{k}"' not in artifact_src)
    assert missing == [], (
        f"ingest persists {missing} but the artifact never exports them")
