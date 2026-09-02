"""2A -- every reading of `fixture_rates` must receive the same inputs.

`projection.fixture_rates` is read in four places: the point estimate
(`_project_one_fixture`), the per-player distribution (`model.simulate`), the
correlated scenario engine (`model.scenarios`), and the backtest. A13 and A17
were both about those readings disagreeing, and the sampling-tolerance
invariant exists because they had.

Adding a new INPUT to that function reopens the same hole: a caller that omits
it computes a different `p_start` and every downstream number drifts. It
happened immediately -- Armstrong published a point estimate of 2.17 above his
own simulated ceiling of 2.0 -- and the artifact contract caught it, which is
what it is for. These tests are the cheaper guard.
"""
from __future__ import annotations

import inspect

from gaffer.model import features, projection, scenarios, simulate


def test_every_reading_of_fixture_rates_passes_the_recency_map():
    """Source-level, deliberately. A behavioural test would need the whole
    pipeline; this catches a new caller the moment it is written."""
    offenders = []
    for mod in (projection, simulate, scenarios):
        src = inspect.getsource(mod)
        for i, line in enumerate(src.splitlines()):
            if "fixture_rates(" not in line or "def fixture_rates" in line:
                continue
            # The call may wrap; take the next three lines as its argument list.
            window = "\n".join(src.splitlines()[i:i + 4])
            if "recency" not in window:
                offenders.append(f"{mod.__name__}:{line.strip()[:70]}")
    assert not offenders, (
        "these readings of fixture_rates omit the recency map and will compute "
        f"a different p_start from the published one: {offenders}")


def test_the_two_recency_implementations_agree_on_shape():
    """One definition, two readers: the live table and the archive frame. They
    cannot share code -- one reads SQLite for a running season, the other a
    DataFrame for a finished one -- so the contract between them is the shape
    of what they return, and it is asserted rather than assumed."""
    from gaffer import backtest

    live = inspect.signature(features.start_recency_by_player)
    archive = inspect.signature(backtest._recency_before)
    assert "last_n" in live.parameters and "last_n" in archive.parameters
    assert live.parameters["last_n"].default == archive.parameters["last_n"].default


def test_recency_is_absent_rather_than_zero_for_a_player_with_no_fixtures(conn):
    """A new signing has not been dropped. If he appeared with
    `started_lag: 0` the model would read "benched last week" from no evidence
    at all, which is the failure mode the price prior already has."""
    out = features.start_recency_by_player(conn)
    for pid, row in out.items():
        assert row["started_lag"] is not None, pid
        assert row["start_rate_r3"] is not None, pid
    # Nobody with no rows may appear.
    have_rows = {
        int(r["player_id"]) for r in conn.execute(
            "SELECT DISTINCT player_id FROM player_gw WHERE starts IS NOT NULL")
    }
    assert set(out) <= have_rows


def test_a_missing_recency_map_still_produces_a_projection():
    """The map is optional by design: the backtest's parity test and any caller
    without a `player_gw` table must still get a number, falling back to
    shrinkage alone rather than raising."""
    rate, branch = projection.base_start_rate(
        starts_td=2, fixtures_played=2, base_starts=19,
        started_lag=None, start_rate_r3=None, position="DEF", price=50)
    assert 0.0 < rate < 1.0
    assert "last_match" not in branch and "last3" not in branch
