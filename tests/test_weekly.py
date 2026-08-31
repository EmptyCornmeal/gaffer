"""G2 — a hold we cannot price is not a hold worth zero.

`hold_baseline` returns `legal: False`, an empty XI and `horizon_value: 0.0`
when fewer than eleven owned players can be projected. Nothing checked the
flag, so that 0.0 reached `decision.compare` as a real score and every move
beat it by the value of a whole squad -- a confident transfer recommendation
manufactured out of a missing projection.
"""
from __future__ import annotations

from gaffer import config, decision, weekly


class _Sol:
    starting = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    bench = [12, 13, 14, 15]
    captain = 1
    vice = 2
    hits = 0


def _build(monkeypatch, *, legal: bool, starting: list[int] | None = None):
    monkeypatch.setattr(weekly, "held_squad",
                        lambda conn: {"squad": list(range(1, 16)),
                                      "starting": list(range(1, 12))})
    monkeypatch.setattr(
        weekly, "hold_baseline",
        lambda conn, squad, from_gw, horizon: {
            "starting": starting if starting is not None else [],
            "bench": [], "captain": None, "vice": None,
            "horizon_value": 0.0, "legal": legal})

    def _boom(*a, **k):
        raise AssertionError(
            "decision.compare was reached with an unpriceable hold")

    if not legal:
        monkeypatch.setattr(decision, "compare", _boom)

    return weekly.build(
        None, sol=_Sol(), from_gw=1, horizon=6, scen=None,
        settings=config.Settings.load())


def test_an_unpriceable_hold_does_not_become_a_transfer(monkeypatch):
    d = _build(monkeypatch, legal=False)
    assert d.action == decision.ACTION_UNAVAILABLE
    assert d.confidence == "unknown"


def test_it_says_how_many_players_were_missing(monkeypatch):
    d = _build(monkeypatch, legal=False, starting=[1, 2, 3, 4])
    assert "7 of your eleven" in d.reason


def test_the_suggested_squad_still_ships(monkeypatch):
    """Refusing to advise a transfer is not refusing to show anything."""
    d = _build(monkeypatch, legal=False)
    assert d.starting == _Sol.starting
    assert d.captain == _Sol.captain



class _HorizonSol:
    starting = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 16]
    bench = [12, 13, 14, 15]
    captain = 16
    vice = 2
    hits = 4
    transfers_in = [16]
    transfers_out = [11]


def test_a_rejected_horizon_plan_is_separate_from_the_roll(monkeypatch):
    held = {
        "source_event": 2, "squad": list(range(1, 16)),
        "starting": list(range(1, 12)), "bench": [12, 13, 14, 15],
        "captain": 1, "vice": 2,
    }
    hold = {
        "starting": list(range(1, 12)), "bench": [12, 13, 14, 15],
        "captain": 1, "vice": 2, "horizon_value": 100.0, "legal": True,
    }
    cmp_ = decision.Comparison(
        move_expected=43.76, hold_expected=48.34, delta=-4.58,
        delta_ci95=(-4.99, -4.17), p_move_beats_hold=0.2865, n_sims=2000,
        short_term_delta=-4.58, horizon_delta=16.67, hit_cost=16,
    )
    exe = decision.Executability(True, 0, 0, 50, 50, 1, 1, 4)

    monkeypatch.setattr(weekly, "held_squad", lambda conn: held)
    monkeypatch.setattr(weekly, "hold_baseline", lambda *a, **k: hold)
    monkeypatch.setattr(weekly, "move_horizon_value", lambda *a, **k: 116.67)
    monkeypatch.setattr(decision, "compare", lambda *a, **k: cmp_)
    monkeypatch.setattr(decision, "executability", lambda *a, **k: exe)
    monkeypatch.setattr(weekly, "_int_meta", lambda *a, **k: 0)
    monkeypatch.setattr(weekly, "_meta", lambda *a, **k: None)
    monkeypatch.setattr(weekly, "_name", lambda conn, pid: f"P{pid}")

    d = weekly.build(
        None, sol=_HorizonSol(), from_gw=3, horizon=6, scen=None,
        settings=config.Settings.load(),
    )

    assert d.action == decision.ACTION_ROLL
    assert d.headline == "Roll your transfer — captain P1"
    assert d.captain == 1 and d.vice == 2
    assert d.starting == hold["starting"]
    assert d.transfers_in == [] and d.transfers_out == []
    assert d.executability is None
    assert d.candidate_move is not None
    assert d.candidate_move.status == decision.CANDIDATE_STATUS_EVIDENCE_ONLY
    assert d.candidate_move.transfers_in == [16]
    assert d.candidate_move.executability.paid_transfers == 4
