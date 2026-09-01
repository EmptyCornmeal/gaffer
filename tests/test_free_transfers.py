"""1.15 -- free transfers are derived, not defaulted.

This was a configuration constant defaulting to 1, published as
`free_transfers_source: "default"`. It decides how many of a move's transfers
are PAID: three transfers on 1 FT is -8 and on 2 FT is -4, so the single most
important state variable in a transfer decision was a guess that happened to
be right.
"""
from __future__ import annotations

from gaffer import teamstate as T


def _hist(rows, chips=None):
    return {"current": [{"event": g, "event_transfers": n} for g, n in rows],
            "chips": chips or []}


def test_gw1_is_pre_season_and_consumes_nothing():
    """Unlimited free transfers before the first deadline. A 15-transfer
    pre-season build must not read as fifteen spent."""
    ft, src, _ = T.derive_free_transfers(_hist([(1, 15)]))
    assert ft == 1
    assert src == "derived_from_entry_history"


def test_a_spent_transfer_leaves_one():
    # The live entry on 2026-09-01: 0 in GW1, 1 in GW2 -> 1 for GW3.
    ft, _, _ = T.derive_free_transfers(_hist([(1, 0), (2, 1)]))
    assert ft == 1


def test_a_rolled_transfer_accumulates():
    """Where the old constant becomes WRONG. Roll in GW2 and GW3 has two."""
    ft, _, _ = T.derive_free_transfers(_hist([(1, 0), (2, 0)]))
    assert ft == 2


def test_accumulation_is_capped():
    rows = [(1, 0)] + [(g, 0) for g in range(2, 12)]
    ft, _, _ = T.derive_free_transfers(_hist(rows), cap_extra=4)
    assert ft == 5, "1 + max_extra_free_transfers, and no further"


def test_taking_hits_never_drives_it_below_one():
    ft, _, _ = T.derive_free_transfers(_hist([(1, 0), (2, 6)]))
    assert ft == 1


def test_a_wildcard_week_spends_no_free_transfer():
    """A wildcard's transfers are free. Counting them would report 1 where the
    manager actually has 2, and understate every subsequent hit."""
    ft, _, notes = T.derive_free_transfers(
        _hist([(1, 0), (2, 0), (3, 11)], chips=[{"event": 3, "name": "wildcard"}]))
    assert ft == 3, "two rolled, plus one granted; the eleven were free"
    assert any("wildcard" in n for n in notes)


def test_a_free_hit_week_likewise():
    ft, _, _ = T.derive_free_transfers(
        _hist([(1, 0), (2, 8)], chips=[{"event": 2, "name": "freehit"}]))
    assert ft == 2


def test_an_underivable_history_returns_none_not_a_guess():
    """An absent answer, never a fabricated one."""
    for bad in (None, {}, {"current": []},
                {"current": [{"event": 2, "event_transfers": None}]},
                {"current": [{"event": "x", "event_transfers": 1}]}):
        ft, src, notes = T.derive_free_transfers(bad)
        assert ft is None
        assert src == "unavailable"
        assert notes


def test_an_explicit_configuration_value_still_wins():
    """Derivation replaces a GUESS, not knowledge. If the operator has said
    what it is, that is a source, and ingest must not overwrite it."""
    import inspect

    from gaffer import ingest
    src = inspect.getsource(ingest.ingest_my_squad)
    assert 'if ft_source == "default":' in src, (
        "the derivation must be gated on the source being a default")
