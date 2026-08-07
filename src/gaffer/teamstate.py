"""Executable team state: what you own, what it sells for, what you can spend.

The solver previously valued every held player at their *current market price*,
because ``purchase_price`` and ``selling_price`` were hard-coded ``None``. FPL
pays purchase + half of any rise (rounded down), so that overstated the budget
by roughly ``ceil(rise/2)`` per risen player and produced transfer plans that
could not be executed.

Everything here comes from public endpoints. No authentication, no cookies.

Reconstruction, in order of confidence:

  ``transfer_in``    the player was bought; ``element_in_cost`` is the exact price
  ``season_start``   never transferred in, so held since GW1: the exact start
                     price, recovered as ``now_cost - cost_change_start``
  ``manual``         a user-supplied override
  ``conservative``   the transfer history could not be read; a deliberate
                     lower bound, and the squad is marked not-executable

Units are tenths of a million throughout — the same unit the FPL API uses.
Conversion happens only at the display boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from gaffer import config

SOURCE_TRANSFER = "transfer_in"
SOURCE_SEASON_START = "season_start"
SOURCE_MANUAL = "manual"
SOURCE_CONSERVATIVE = "conservative"


@dataclass(frozen=True)
class HeldPrice:
    player_id: int
    purchase: int          # tenths of a million
    now: int               # tenths
    selling: int           # tenths, per config.fpl_selling_price
    source: str
    exact: bool

    @property
    def locked_in(self) -> int:
        """Money you lose by selling: the gap between market and selling price."""
        return self.now - self.selling


@dataclass
class SquadPrices:
    prices: dict[int, HeldPrice]
    complete: bool          # every held player has an exact purchase price
    reason: str

    @property
    def confidence(self) -> str:
        if self.complete:
            return "exact"
        return "partial" if any(p.exact for p in self.prices.values()) else "unknown"

    def total_selling(self) -> int:
        return sum(p.selling for p in self.prices.values())

    def total_market(self) -> int:
        return sum(p.now for p in self.prices.values())


def season_start_price(now_cost: int, cost_change_start: int) -> int:
    """The price this player started the season at.

    ``cost_change_start`` is the cumulative change since the season opened, so
    the start price is exactly recoverable. This is what a player held since GW1
    was bought for.
    """
    return int(now_cost) - int(cost_change_start)


def _last_purchase_from_transfers(
    transfers: Iterable[dict[str, Any]], ignore_events: frozenset[int]
) -> dict[int, int]:
    """Map player -> the cost of the most recent transfer that bought them.

    Processed chronologically so a sell-then-rebuy resets the acquisition price
    to the *later* purchase, which is what FPL charges against.

    Free Hit transfers are excluded: that squad is reverted after the gameweek,
    so its prices never become your holdings.
    """
    rows = [t for t in transfers if isinstance(t, dict)]
    rows.sort(key=lambda t: (t.get("event") or 0, str(t.get("time") or "")))
    out: dict[int, int] = {}
    for t in rows:
        if (t.get("event") or 0) in ignore_events:
            continue
        pid, cost = t.get("element_in"), t.get("element_in_cost")
        if isinstance(pid, int) and isinstance(cost, int):
            out[pid] = cost          # later transfers overwrite earlier ones
        sold = t.get("element_out")
        if isinstance(sold, int):
            out.pop(sold, None)      # no longer held via that acquisition
    return out


def free_hit_events(chips: Iterable[dict[str, Any]] | None) -> frozenset[int]:
    """Gameweeks where a Free Hit was active (its transfers are reverted)."""
    out = set()
    for c in chips or []:
        if isinstance(c, dict) and str(c.get("name", "")).lower() in ("freehit", "free_hit"):
            ev = c.get("event")
            if isinstance(ev, int):
                out.add(ev)
    return frozenset(out)


def reconstruct(
    squad_ids: Iterable[int],
    market_prices: dict[int, int],
    cost_change_start: dict[int, int],
    transfers: list[dict[str, Any]] | None,
    chips: list[dict[str, Any]] | None = None,
    overrides: dict[int, int] | None = None,
) -> SquadPrices:
    """Reconstruct purchase and selling prices for the currently held squad.

    ``transfers=None`` means the history could not be read — every price then
    falls back to a deliberate lower bound and the result is marked incomplete,
    so nothing downstream can claim the plan is executable.
    """
    overrides = overrides or {}
    squad_ids = list(squad_ids)

    if transfers is None:
        bought: dict[int, int] = {}
        history_ok = False
        reason = "transfer history unavailable; purchase prices are a lower bound"
    else:
        bought = _last_purchase_from_transfers(transfers, free_hit_events(chips))
        history_ok = True
        reason = "reconstructed from public transfer history"

    prices: dict[int, HeldPrice] = {}
    for pid in squad_ids:
        now = int(market_prices.get(pid, 0))
        start = season_start_price(now, cost_change_start.get(pid, 0))

        if pid in overrides:
            purchase, source, exact = int(overrides[pid]), SOURCE_MANUAL, True
        elif pid in bought:
            purchase, source, exact = int(bought[pid]), SOURCE_TRANSFER, True
        elif history_ok:
            # Never bought during the season, so held since the opening squad.
            purchase, source, exact = start, SOURCE_SEASON_START, True
        else:
            # Unknown. Take the smaller of the start price and the current price
            # so the selling estimate can only understate what FPL will pay.
            purchase, source, exact = min(start, now), SOURCE_CONSERVATIVE, False

        prices[pid] = HeldPrice(
            player_id=pid, purchase=purchase, now=now,
            selling=config.fpl_selling_price(purchase, now),
            source=source, exact=exact,
        )

    complete = bool(prices) and all(p.exact for p in prices.values())
    if complete:
        reason = "every held player has an exact purchase price"
    return SquadPrices(prices=prices, complete=complete, reason=reason)


# ---------------------------------------------------------------------------
# Bank
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BankState:
    value: int | None      # tenths
    source: str
    exact: bool


def resolve_bank(
    configured: int | None,
    from_picks: int | None = None,
    from_entry: int | None = None,
) -> BankState:
    """Resolve in-the-bank money. Configuration wins over derived values.

    Returns ``value=None`` when nothing is known — which must be treated as
    "unknown", never as zero. Zero is a real, different answer.
    """
    if configured is not None:
        return BankState(int(configured), "config", True)
    if from_picks is not None:
        return BankState(int(from_picks), "entry_picks", True)
    if from_entry is not None:
        return BankState(int(from_entry), "entry_last_deadline", True)
    return BankState(None, "unknown", False)


@dataclass(frozen=True)
class TeamStateSummary:
    """What the pipeline records, and what the UI must be able to explain."""

    bank: int | None
    bank_source: str
    bank_exact: bool
    free_transfers: int
    free_transfers_source: str
    selling_price_confidence: str
    selling_prices_exact: int
    selling_prices_total: int
    executable: bool
    reason: str

    def as_meta(self) -> dict[str, Any]:
        return {
            "bank": self.bank,
            "bank_source": self.bank_source,
            "bank_exact": self.bank_exact,
            "free_transfers": self.free_transfers,
            "free_transfers_source": self.free_transfers_source,
            "selling_price_confidence": self.selling_price_confidence,
            "selling_prices_exact": self.selling_prices_exact,
            "selling_prices_total": self.selling_prices_total,
            "recommendation_executable": self.executable,
            "team_state_reason": self.reason,
        }


def summarise(
    prices: SquadPrices, bank: BankState, free_transfers: int, ft_source: str
) -> TeamStateSummary:
    """Decide whether a transfer recommendation may be called executable.

    Executable requires: a squad, exact selling prices for all of it, and a
    known bank. Anything less is still usable for ranking, but must not be
    presented as a plan you can carry out.
    """
    n = len(prices.prices)
    exact = sum(1 for p in prices.prices.values() if p.exact)
    if n == 0:
        executable, reason = False, "no squad is known, so there is nothing to execute"
    elif not prices.complete:
        executable, reason = False, f"selling prices incomplete: {prices.reason}"
    elif bank.value is None:
        executable, reason = False, (
            "bank is unknown; set GAFFER_BANK or [fpl].bank so transfers are costed "
            "against real money"
        )
    else:
        executable, reason = True, (
            "selling prices reconstructed exactly and bank is known"
        )
    return TeamStateSummary(
        bank=bank.value, bank_source=bank.source, bank_exact=bank.exact,
        free_transfers=free_transfers, free_transfers_source=ft_source,
        selling_price_confidence=prices.confidence,
        selling_prices_exact=exact, selling_prices_total=n,
        executable=executable, reason=reason,
    )
