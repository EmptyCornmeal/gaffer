"""1.6 -- the XI-selection change was measured and refused.

The shipped objective picks the eleven on a horizon-decayed six-gameweek sum
while picking the captain on the imminent gameweek, and the comment above the
captain term states exactly why one week is right for something re-chosen every
week. The identical argument was never applied to the eleven beside it, and the
roadmap carried it as a Scope defect worth "~2 pts/wk".

It was blocked on the harness -- `_decision_metrics` runs only at h=1 and picks
squad AND XI on the same one-week column, so the backtest could not model the
shipped behaviour at all. The harness gap is closed and the change was measured
before it was written.

It lost on the held-out season, so it did not ship. This pins the refusal so it
cannot be quietly re-derived from the intuition.
"""
from __future__ import annotations

from gaffer import backtest as BT


def test_the_refusal_is_recorded_with_its_numbers():
    r = BT.XI_SELECTION_REFUSED
    assert r["decision"] == "measured, REFUSED"
    seasons = r["xi_points_per_gw"]
    assert set(seasons) == {"2023-24", "2024-25", "2025-26"}
    assert seasons["2025-26"]["role"] == "test"


def test_it_lost_on_the_held_out_season():
    """The whole reason it did not ship. If this ever reads positive, someone
    has edited the record rather than re-run the measurement."""
    test = BT.XI_SELECTION_REFUSED["xi_points_per_gw"]["2025-26"]
    assert test["delta"] < 0
    assert test["proposed"] < test["shipped"]


def test_the_two_seasons_that_liked_it_are_kept_too():
    """A refusal that hides the evidence FOR the idea is not a record, it is an
    argument. Both winning seasons stay on the page."""
    s = BT.XI_SELECTION_REFUSED["xi_points_per_gw"]
    assert s["2023-24"]["delta"] > 0
    assert s["2024-25"]["delta"] > 0


def test_the_decision_rule_was_pre_registered():
    assert "pre-registered" in BT.XI_SELECTION_REFUSED["rule"]


def test_the_scope_concern_is_resolved_rather_than_dropped():
    """Refusing the change does not make the presentation honest by itself.
    The eleven are still chosen over six gameweeks and shown beside a one-week
    xP, so the domain has to be stated somewhere."""
    r = BT.XI_SELECTION_REFUSED
    assert "disclosure" in r["scope_concern_resolved_by"]

    from gaffer import config
    home = (config.REPO_ROOT / "web" / "src" / "pages" / "Home.svelte")
    text = home.read_text(encoding="utf-8")
    assert "picked over 6 GWs" in text, (
        "the XI card must state the horizon it was selected on")


def test_the_shipped_objective_still_selects_the_xi_on_the_horizon():
    """The refusal means the code does NOT change. If someone ships the
    proposal later, this fails and they must update the record first."""
    import inspect

    from gaffer.solver import optimize
    src = inspect.getsource(optimize)
    assert "obj = pulp.lpSum(start[i] * players[i].value for i in ids)" in src, (
        "the XI term was changed without updating XI_SELECTION_REFUSED")
