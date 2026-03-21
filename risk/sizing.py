"""
risk/sizing.py — Position sizing for paper execution.

v1 HARD CONSTRAINT: All positions are 5 shares (fixed).
No 1-share or 2-share examples appear anywhere in this codebase.

The SizeResult explicitly reports the bankroll fraction consumed so that
risk outputs are always auditable and comparable across runs.

Bankroll tracking
-----------------
The PositionSizer maintains a running paper bankroll.  Callers update it
after each settled trade via record_trade().

Tighten mode
------------
When the kill condition validator returns action=TIGHTEN, the caller
should set sizer.tightened = True.  In tighten mode, the sizer still
uses FIXED_SHARES_V1 (5 shares) in v1 — no fractional shares exist.
A TODO is left for v2 to halve sizing on tighten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# v1 hard constraint: minimum and only allowed order size.
FIXED_SHARES_V1 = 5
MIN_SHARES = 5


@dataclass(frozen=True)
class SizeResult:
    """
    Sizing decision for one trade.

    Attributes
    ----------
    shares              Number of shares (always 5 in v1).
    price               Entry price.
    cost_usd            Gross cash outlay (price * shares).
    bankroll_before     Bankroll before entry.
    bankroll_fraction   cost_usd / bankroll_before.
    tightened           Whether tighten mode was active.
    """
    shares: int
    price: float
    cost_usd: float
    bankroll_before: float
    bankroll_fraction: float
    tightened: bool


class PositionSizer:
    """
    Determines position size and tracks running paper bankroll.
    """

    def __init__(self, initial_bankroll: float) -> None:
        if initial_bankroll <= 0:
            raise ValueError(f"initial_bankroll must be > 0, got {initial_bankroll}")
        self._bankroll = initial_bankroll
        self.tightened: bool = False

    @property
    def bankroll(self) -> float:
        return self._bankroll

    def size(self, price: float) -> SizeResult:
        """
        Return a SizeResult for an entry at `price`.

        v1: always 5 shares.
        TODO v2: if tightened, consider halving (2 or 3 shares minimum).
        """
        if not (0.0 < price < 1.0):
            raise ValueError(f"price must be in (0, 1), got {price}")

        shares = FIXED_SHARES_V1   # v1: no other option
        cost = price * shares
        fraction = cost / self._bankroll if self._bankroll > 0 else 0.0

        sr = SizeResult(
            shares=shares,
            price=price,
            cost_usd=cost,
            bankroll_before=self._bankroll,
            bankroll_fraction=fraction,
            tightened=self.tightened,
        )
        logger.debug(
            "[sizer] shares=%d price=%.4f cost=%.4f bankroll=%.4f frac=%.4f tightened=%s",
            shares, price, cost, self._bankroll, fraction, self.tightened
        )
        return sr

    def record_trade(self, net_pnl: float) -> None:
        """
        Update the running bankroll with the net P&L of a settled trade.

        net_pnl is positive for wins, negative for losses.
        Fee costs are already reflected in net_pnl by the execution lanes.
        """
        self._bankroll += net_pnl
        logger.info(
            "[sizer] trade settled pnl=%.4f new_bankroll=%.4f",
            net_pnl, self._bankroll
        )
