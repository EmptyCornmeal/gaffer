"""T-11 — executable team state: selling prices, bank, free transfers.

The solver valued held players at market price, handing itself money FPL will
not pay. A 4-for-4 with every player risen 0.3m produced £0.8m of phantom cash.
"""

from __future__ import annotations

import pytest

from gaffer import config
from gaffer import teamstate as T

# All prices in tenths of a million, the API's unit.


def tr(event, in_id=None, in_cost=None, out_id=None, out_cost=None, time=""):
    return {"event": event, "element_in": in_id, "element_in_cost": in_cost,
            "element_out": out_id, "element_out_cost": out_cost, "time": time}


# --------------------------------------------------------------------------
# The FPL sell-on rule
# --------------------------------------------------------------------------

@pytest.mark.parametrize("purchase,now,expected", [
    (100, 100, 100),   # unchanged
    (100, 103, 101),   # +0.3 rise -> half, rounded DOWN
    (100, 105, 102),   # +0.5 -> +0.2
    (100, 110, 105),   # +1.0 -> +0.5
    (100, 120, 110),   # +2.0 -> +1.0
    (100, 97, 97),     # a fall is taken in full
    (100, 90, 90),
    (100, 101, 100),   # +0.1 rise -> nothing back
])
def test_selling_price_rule(purchase, now, expected):
    assert config.fpl_selling_price(purchase, now) == expected


def test_selling_price_is_never_above_market():
    for p in range(38, 150):
        for n in range(38, 150):
            assert config.fpl_selling_price(p, n) <= max(n, 0) or n < p


# --------------------------------------------------------------------------
# Reconstruction
# --------------------------------------------------------------------------

def test_season_start_price_is_exactly_recoverable():
    # now 105, up 0.5 since the season opened -> bought at 100.
    assert T.season_start_price(105, 5) == 100
    assert T.season_start_price(95, -5) == 100
    assert T.season_start_price(100, 0) == 100


def test_initial_squad_uses_the_exact_start_price():
    """Never transferred in => held since GW1 => start price is exact."""
    r = T.reconstruct([1], {1: 105}, {1: 5}, transfers=[])
    p = r.prices[1]
    assert p.purchase == 100
    assert p.selling == 102          # 100 + (105-100)//2
    assert p.source == T.SOURCE_SEASON_START
    assert p.exact is True
    assert r.complete is True
    assert p.locked_in == 3          # 0.3m you cannot recover


def test_transferred_in_player_uses_the_paid_price():
    r = T.reconstruct([1], {1: 110}, {1: 20}, transfers=[tr(3, in_id=1, in_cost=104)])
    p = r.prices[1]
    assert p.purchase == 104 and p.source == T.SOURCE_TRANSFER
    assert p.selling == 107          # 104 + (110-104)//2


def test_price_fall_is_taken_in_full():
    r = T.reconstruct([1], {1: 95}, {1: -5}, transfers=[tr(3, in_id=1, in_cost=105)])
    p = r.prices[1]
    assert p.selling == 95           # you eat the whole fall
    assert p.locked_in == 0


def test_repurchase_resets_the_acquisition_price():
    """Sold at one price, bought back later at another: the later one counts."""
    transfers = [
        tr(2, in_id=1, in_cost=100, time="2026-08-25"),
        tr(5, out_id=1, out_cost=104, in_id=9, in_cost=90, time="2026-09-15"),
        tr(9, in_id=1, in_cost=112, out_id=9, out_cost=90, time="2026-10-20"),
    ]
    r = T.reconstruct([1], {1: 118}, {1: 18}, transfers=transfers)
    p = r.prices[1]
    assert p.purchase == 112, "the repurchase price must win, not the original"
    assert p.selling == 115          # 112 + (118-112)//2


def test_multiple_transfers_of_the_same_player_take_the_latest():
    transfers = [
        tr(2, in_id=1, in_cost=100, time="a"),
        tr(4, in_id=1, in_cost=106, time="b"),
    ]
    r = T.reconstruct([1], {1: 110}, {1: 10}, transfers=transfers)
    assert r.prices[1].purchase == 106


def test_sold_player_not_in_the_squad_is_ignored():
    transfers = [tr(2, in_id=7, in_cost=100), tr(4, out_id=7, out_cost=102)]
    r = T.reconstruct([1], {1: 100, 7: 102}, {1: 0, 7: 2}, transfers=transfers)
    assert set(r.prices) == {1}


def test_free_hit_transfers_are_excluded():
    """A Free Hit squad reverts, so its prices never become your holdings."""
    transfers = [tr(7, in_id=1, in_cost=130)]        # bought on the Free Hit
    chips = [{"name": "freehit", "event": 7}]
    r = T.reconstruct([1], {1: 105}, {1: 5}, transfers=transfers, chips=chips)
    p = r.prices[1]
    assert p.source == T.SOURCE_SEASON_START
    assert p.purchase == 100, "the Free Hit price must not stick"


def test_wildcard_transfers_do_count():
    """Unlike a Free Hit, a Wildcard squad is permanent."""
    transfers = [tr(7, in_id=1, in_cost=130)]
    chips = [{"name": "wildcard", "event": 7}]
    r = T.reconstruct([1], {1: 135}, {1: 35}, transfers=transfers, chips=chips)
    assert r.prices[1].purchase == 130


def test_manual_override_wins_and_is_exact():
    r = T.reconstruct([1], {1: 110}, {1: 10}, transfers=[tr(3, in_id=1, in_cost=104)],
                      overrides={1: 99})
    assert r.prices[1].purchase == 99
    assert r.prices[1].source == T.SOURCE_MANUAL
    assert r.complete is True


def test_missing_history_is_conservative_and_incomplete():
    """Unknown must never become market value — that is the phantom-cash bug."""
    r = T.reconstruct([1], {1: 110}, {1: 10}, transfers=None)
    p = r.prices[1]
    assert p.source == T.SOURCE_CONSERVATIVE
    assert p.exact is False
    assert r.complete is False
    assert r.confidence == "unknown"
    # Strictly below market: the solver can never gain money from uncertainty.
    assert p.selling < p.now
    assert p.selling == config.fpl_selling_price(100, 110) == 105


def test_conservative_never_exceeds_the_true_selling_price():
    """Whatever the real purchase was, the estimate must not overstate."""
    now, start = 110, 100
    est = T.reconstruct([1], {1: now}, {1: now - start}, transfers=None).prices[1]
    for true_purchase in range(90, 121):
        true_selling = config.fpl_selling_price(true_purchase, now)
        if true_purchase >= start:
            assert est.selling <= true_selling


def test_mid_season_adopter_with_full_history_is_still_exact():
    """Gaffer starting mid-season does not degrade the reconstruction: the
    transfer history comes from FPL, not from us."""
    transfers = [tr(1, in_id=1, in_cost=100), tr(8, in_id=2, in_cost=75)]
    r = T.reconstruct([1, 2, 3], {1: 104, 2: 78, 3: 50}, {1: 4, 2: 3, 3: 0},
                      transfers=transfers)
    assert r.complete is True
    assert r.prices[1].source == T.SOURCE_TRANSFER
    assert r.prices[3].source == T.SOURCE_SEASON_START


def test_partial_confidence_when_some_prices_are_overridden_and_some_unknown():
    r = T.reconstruct([1, 2], {1: 100, 2: 100}, {1: 0, 2: 0},
                      transfers=None, overrides={1: 95})
    assert r.complete is False
    assert r.confidence == "partial"


def test_totals():
    r = T.reconstruct([1, 2], {1: 110, 2: 60}, {1: 10, 2: 0}, transfers=[])
    assert r.total_market() == 170
    assert r.total_selling() == 165 + 0  # 105 + 60


# --------------------------------------------------------------------------
# Bank + summary
# --------------------------------------------------------------------------

def test_bank_precedence():
    assert T.resolve_bank(12, from_picks=5).value == 12
    assert T.resolve_bank(None, from_picks=5).value == 5
    assert T.resolve_bank(None, None, from_entry=7).value == 7


def test_unknown_bank_is_none_not_zero():
    b = T.resolve_bank(None)
    assert b.value is None and b.exact is False
    assert b.value != 0, "unknown must never collapse to £0.0m"


def test_zero_bank_is_a_real_value():
    b = T.resolve_bank(0)
    assert b.value == 0 and b.exact is True


def test_summary_blocks_executable_when_prices_incomplete():
    r = T.reconstruct([1], {1: 110}, {1: 10}, transfers=None)
    s = T.summarise(r, T.resolve_bank(5), 1, "config")
    assert s.executable is False
    assert "incomplete" in s.reason


def test_summary_blocks_executable_when_bank_unknown():
    r = T.reconstruct([1], {1: 110}, {1: 10}, transfers=[])
    s = T.summarise(r, T.resolve_bank(None), 1, "default")
    assert s.executable is False
    assert "bank is unknown" in s.reason


def test_summary_executable_when_everything_is_known():
    r = T.reconstruct([1], {1: 110}, {1: 10}, transfers=[])
    s = T.summarise(r, T.resolve_bank(5), 2, "config")
    assert s.executable is True
    assert s.selling_price_confidence == "exact"
    assert s.as_meta()["recommendation_executable"] is True


def test_summary_blocks_executable_with_no_squad():
    r = T.reconstruct([], {}, {}, transfers=[])
    s = T.summarise(r, T.resolve_bank(5), 1, "config")
    assert s.executable is False


# --------------------------------------------------------------------------
# Config parsing / units
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (5, 5), (0, 0), (12, 12),        # ints are already tenths
    (0.5, 5), (2.0, 20), (1.3, 13),  # decimals are millions
    ("5", 5), ("0.5", 5), ("£1.5", 15),
])
def test_bank_unit_conversion(raw, expected):
    assert config._bank_tenths(raw, "test") == expected


def test_bank_rejects_nonsense():
    for bad in ("", "abc", True):
        with pytest.raises(config.ConfigError):
            config._bank_tenths(bad, "test")


def test_bank_from_env(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src" / "gaffer").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.setenv("GAFFER_BANK", "0.7")
    config.reload_paths()
    s = config.Settings.load()
    assert s.bank == 7
    assert s.sources["bank"] == "env:GAFFER_BANK"


def test_purchase_price_overrides_parse(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src" / "gaffer").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.setenv("GAFFER_PURCHASE_PRICES", "411:145,233:78")
    config.reload_paths()
    assert config.Settings.load().purchase_prices == {411: 145, 233: 78}


def test_purchase_price_overrides_from_toml(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src" / "gaffer").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / "gaffer.local.toml").write_text(
        "[fpl]\nbank = 0.5\n[fpl.purchase_prices]\n411 = 145\n", encoding="utf-8")
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    for k in ("GAFFER_BANK", "GAFFER_PURCHASE_PRICES"):
        monkeypatch.delenv(k, raising=False)
    config.reload_paths()
    s = config.Settings.load()
    assert s.bank == 5
    assert s.purchase_prices == {411: 145}


def test_malformed_purchase_prices_fail_loudly(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src" / "gaffer").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setenv("GAFFER_REPO_ROOT", str(root))
    monkeypatch.setenv("GAFFER_PURCHASE_PRICES", "411=145")
    config.reload_paths()
    with pytest.raises(config.ConfigError):
        config.Settings.load()
