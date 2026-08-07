"""The leakage contract: which columns may inform a pre-deadline decision.

The audited backtest filtered its evaluation population on ``minutes > 0`` —
i.e. it knew who had played before choosing a team — and reported the result as
model skill. This module makes the boundary explicit and machine-checkable so
that class of mistake fails a test instead of shipping a number.

Rule: a feature computed for gameweek G may use post-match data from gameweeks
< G, but never from G itself. The denylist below names the fields that are only
knowable after G kicks off.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Post-match fields. Legal as evaluation TARGETS, never as same-gameweek features.
POST_MATCH_FIELDS = frozenset({
    # `xP` is INADMISSIBLE — on provenance, not on a correlation.
    #
    # The upstream data dictionary states that `xP` is FPL's `ep_this` field,
    # scraped AFTER each gameweek has ended, that FPL's update cadence for that
    # field is undocumented, and that it "may reflect post-match information
    # rather than the pre-match prediction managers actually saw". Its own advice
    # is to shift(1) it within each element or drop it.
    #
    # So: the archive cannot certify this value as the pre-deadline forecast
    # managers saw, and the upstream dataset explicitly warns that it may contain
    # post-match information. It is therefore inadmissible here.
    #
    # Corroborating, not proof: measured on 2022-23, 2023-24 and 2024-25,
    # restricted to single-fixture gameweeks in which the player completed 60+
    # minutes, a player's week-to-week deviation in `xP` has sd ~1.75 points and
    # correlates +0.40..+0.47 with the deviation in what he then scored. Two
    # quantities that ARE pre-deadline by construction move by sd 0.65-0.87 and
    # correlate +0.09 and -0.13 on the same rows. That is consistent with the
    # warning; it does not by itself establish the timing, and no claim here
    # rests on it alone. See docs/MODEL-EVALUATION.md.
    "xP",
    "total_points", "minutes", "starts",
    "goals_scored", "goals", "assists", "clean_sheets", "clean_sheet",
    "goals_conceded", "own_goals", "penalties_saved", "penalties_missed",
    "yellow_cards", "red_cards", "saves", "bonus", "bps",
    "influence", "creativity", "threat", "ict_index",
    "expected_goals", "expected_assists", "expected_goal_involvements",
    "expected_goals_conceded", "xg", "xa", "xgi", "xgc",
    "defensive_contribution", "defcon",
    "team_h_score", "team_a_score", "result",
})

#: Known-before-kickoff fields. Safe as features.
PRE_DEADLINE_FIELDS = frozenset({
    "element", "player_id", "id", "name", "position", "team", "team_id",
    "opponent_team", "was_home", "home", "fixture", "round", "GW", "gw",
    "kickoff_time", "season", "value", "price", "selected",
    "transfers_balance", "transfers_in", "transfers_out",
    "ep_next", "ep_this", "chance_of_playing_next_round", "status",
})

#: Fields that are genuinely pre-deadline in the LIVE API but have no faithful
#: historical counterpart, so they can be shipped but never measured here.
#: `ep_next` is read from `bootstrap-static` before the deadline and is a real
#: forecast; the archive's `xP` column is not the same number (see above), so
#: anything trained or validated against `xP` says nothing about `ep_next`.
LIVE_ONLY_FIELDS = frozenset({"ep_next", "ep_this", "status",
                              "chance_of_playing_next_round"})

#: Rolling/aggregate features derived from strictly prior gameweeks. The `_td`
#: (to-date) and `r_` (rolling) prefixes are shift(1)-based by construction.
DERIVED_PREFIXES = ("r_", "roll_")
DERIVED_SUFFIXES = ("_td", "_prior", "_lag")


class LeakageError(AssertionError):
    """Raised when a post-match field is used as a pre-deadline feature."""


def is_post_match(column: str) -> bool:
    """True when ``column`` is only knowable after the gameweek has been played."""
    if column in PRE_DEADLINE_FIELDS:
        return False
    if column.startswith(DERIVED_PREFIXES) or column.endswith(DERIVED_SUFFIXES):
        return False  # derived from prior gameweeks only
    return column in POST_MATCH_FIELDS


def check_features(columns: Iterable[str], *, context: str = "features") -> list[str]:
    """Return the offending column names (empty when clean)."""
    return sorted(c for c in columns if is_post_match(c))


def assert_no_leakage(columns: Iterable[str], *, context: str = "features") -> None:
    """Raise :class:`LeakageError` if any post-match column is present."""
    bad = check_features(columns, context=context)
    if bad:
        raise LeakageError(
            f"post-match fields used as pre-deadline {context}: {bad}. "
            "These are only knowable after kickoff; they may be evaluation "
            "targets but never model inputs."
        )
