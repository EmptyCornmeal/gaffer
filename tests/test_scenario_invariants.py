"""What a scenario is not allowed to contain, and what it currently does.

`gaffer.model.scenarios` describes itself as one shared football: a match is
drawn once and every player in it is conditioned on that draw. Three of its
terms are not conditioned on the appearance draw at all, so a player who did not
play can still be charged for goals his team conceded, paid for saves he did not
make, and paid a DEFCON bonus for defensive actions he had no minutes to perform.

External review, 2026-09-06 (GPT-6 Astra) measured it; this module pins it.

Two kinds of test live here and the distinction matters:

* **xfail(strict)** — the invariant we want. It fails today and the suite says
  so out loud. When somebody fixes the generator these flip to XPASS and the
  strict flag turns that into a failure, which is the notification that the
  pin below needs retiring rather than a silent green.
* **the bound** — the measured violation rate, pinned so it cannot quietly get
  worse while the invariant remains unmet.

Nothing here proposes a fix. The smallest correction is named in the module
docstring of the fix, not in its test.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from gaffer import config
from gaffer.model import scenarios

GW = 4
SEED_NOTE = "the shipped default seed, so this is the draw production makes"


@pytest.fixture(scope="module")
def drawn():
    """One real draw from the live database, read-only.

    Skips rather than fails where there is no database: this is a property of
    the generator, and a machine without data has nothing to say about it.
    """
    path = config.DATA_DIR / "gaffer.db"
    if not path.exists():
        pytest.skip("no local database")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ss = scenarios.simulate(conn, GW)
    if not ss.player_ids:
        pytest.skip(f"no fixtures in GW{GW}")
    rates, _ = scenarios._collect_rates(conn, GW)
    return ss, {r.pid: r for r in rates}


def _absent(ss):
    pts = np.asarray(ss.points)
    app = np.asarray(ss.appeared)
    return pts[~app]


# --- the invariants --------------------------------------------------------

@pytest.mark.xfail(strict=True, reason=(
    "conceded points, saves and DEFCON are not gated on the appearance draw; "
    "measured 13,346 non-zero cells in 479,016 no-appearance player-scenarios"))
def test_a_player_who_did_not_appear_scores_nothing(drawn):
    ss, _ = drawn
    absent = _absent(ss)
    assert int((absent != 0).sum()) == 0


@pytest.mark.xfail(strict=True, reason=(
    "goalkeepers and defenders are charged for goals conceded on "
    "`lam_conceded * mins_frac`, the EXPECTED minutes share, in scenarios "
    "where the drawn minutes are zero"))
def test_a_player_who_did_not_appear_is_not_charged_for_conceded_goals(drawn):
    ss, _ = drawn
    absent = _absent(ss)
    assert int((absent < 0).sum()) == 0


@pytest.mark.xfail(strict=True, reason=(
    "`defcon_p_hit` is applied unconditionally, and saves are drawn at "
    "`saves_lam` with no minutes gate at all"))
def test_a_player_who_did_not_appear_earns_no_positive_points(drawn):
    ss, _ = drawn
    absent = _absent(ss)
    assert int((absent > 0).sum()) == 0


@pytest.mark.xfail(strict=True, reason=(
    "`p_cs` and the opposing lineup lambda are two estimates of one quantity; "
    "the band between them is paid as a clean sheet in scenarios where the "
    "attack facing it drew goals"))
def test_a_clean_sheet_cannot_coincide_with_goals_conceded(drawn):
    ss, _ = drawn
    contradiction = (ss.diagnostics or {}).get("clean_sheet_contradiction") or {}
    assert float(contradiction.get("mean", 0.0)) == 0.0


# --- what is already true, and must stay true ------------------------------

def test_appearance_is_recorded_separately_from_points(drawn):
    """A zero in the points matrix cannot say whether he played."""
    ss, _ = drawn
    app = np.asarray(ss.appeared)
    pts = np.asarray(ss.points)
    assert app.shape == pts.shape
    assert app.dtype == bool
    # Both states must actually occur, or the mask is not carrying information.
    assert app.any() and (~app).any()


def test_the_appearance_draw_reproduces_the_projections_own_p_play(drawn):
    """The marginal the generator claims to preserve, on the term that gates
    everything else.

    If the appearance rate drifts from `p_play`, every downstream gate is
    conditioning on a different player from the one the projection published.
    """
    ss, rates = drawn
    diffs = []
    for pid, r in rates.items():
        i = ss.index.get(pid)
        if i is None:
            continue
        drawn_rate = float(np.asarray(ss.appeared)[i].mean())
        diffs.append(abs(drawn_rate - float(r.p_play)))
    assert diffs, "no players to compare"
    assert float(np.mean(diffs)) < 0.02, (
        f"mean |p_play - drawn appearance rate| {np.mean(diffs):.4f}, "
        f"worst {max(diffs):.4f}")


def test_captain_doubling_reads_the_same_row_as_the_squad(drawn):
    """Captain, autosubs and squad scoring must share one appearance state."""
    ss, _ = drawn
    xi = ss.player_ids[:11]
    plain = ss.squad_points(xi)
    doubled = ss.squad_points(xi, captain=xi[0])
    assert np.allclose(doubled - plain, ss.row(xi[0]))


# --- the bound, so it cannot silently worsen -------------------------------

def test_the_measured_violation_rate_has_not_grown(drawn):
    """Pinned 2026-09-06 at 13,346 / 479,016 = 2.79% of no-appearance cells.

    A ceiling, not a target. If a change to the generator pushes it up, that is
    a regression in a property the product already claims to have; if it falls
    to zero the xfail tests above turn XPASS and this pin should be deleted with
    them.
    """
    ss, _ = drawn
    absent = _absent(ss)
    if absent.size == 0:
        pytest.skip("every player appeared in every scenario")
    rate = float((absent != 0).mean())
    assert rate <= 0.035, (
        f"{rate:.4%} of no-appearance player-scenarios score points, above the "
        "2.79% measured on 2026-09-06")


def test_the_clean_sheet_contradiction_has_not_grown(drawn):
    """Pinned 2026-09-06 at mean 0.0555, max 0.2727 over 20 fixture-sides."""
    ss, _ = drawn
    c = (ss.diagnostics or {}).get("clean_sheet_contradiction") or {}
    assert float(c.get("mean", 1.0)) <= 0.075, c
    assert float(c.get("max", 1.0)) <= 0.35, c
