"""
execution/taker_lane.py — Taker paper execution lane.

The taker lane simulates market-order fills on the YES side.
It is the BENCHMARK / CONTROL lane — used to compare against the maker lane
using the same signal, providing an apples-to-apples fee-impact analysis.

Sizing: FIXED 5 SHARES (v1 hard constraint).

Fill simulation
---------------
A taker fill is assumed to execute at the window OPEN price.  This is an
optimistic assumption for the open-price benchmark.  Actual taker fills
during the window may be at worse prices.  See README for execution realism
risks.

Fee computation
---------------
Taker fee is computed using the formula from execution/fees.py:
    fee = C * 0.25 * (p * (1 - p))^2

This is deducted from gross P&L to produce net P&L.

Execution log fields (per window):
    filled                  : bool  (always True for taker lane if signal exists)
    fill_price              : float (window open price)
    shares                  : int   (always 5 in v1)
    fee_per_share           : float
    total_fee               : float
    gross_pnl               : float | None
    net_pnl                 : float | None
    bankroll_fraction        : float
    break_even_wr_estimate  : float
    win_if_correct          : float
    loss_if_wrong           : float
    outcome_correct         : bool | None
    fee_result              : TakerFeeResult  (full fee breakdown for audit)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .fees import compute_taker_fee, break_even_win_rate, TakerFeeResult

logger = logging.getLogger(__name__)

FIXED_SHARES_V1 = 5


@dataclass
class TakerResult:
    """Full record of a single taker-lane paper evaluation for one window."""
    window_open_ts: int
    slug: str
    signal_direction: str
    intended_price: Optional[float]
    shares: int = FIXED_SHARES_V1
    filled: bool = False
    fill_price: Optional[float] = None
    fee_per_share: float = 0.0
    total_fee: float = 0.0
    gross_pnl: Optional[float] = None
    net_pnl: Optional[float] = None
    bankroll_fraction: Optional[float] = None
    break_even_wr_estimate: Optional[float] = None
    win_if_correct: Optional[float] = None
    loss_if_wrong: Optional[float] = None
    outcome_correct: Optional[bool] = None
    fee_result: Optional[TakerFeeResult] = None
    rejection_reason: Optional[str] = None
    extra: dict = field(default_factory=dict)


class TakerLane:
    """
    Taker paper execution lane (benchmark / control).

    Same signal as the maker lane, different execution model:
    - Always fills at open price (market order assumption).
    - Deducts taker fee per the auditable formula in execution/fees.py.
    """

    def __init__(self, config: dict) -> None:
        self._fee_C = config.get("fees", {}).get("taker_fee_C", 0.02)
        self._shares = FIXED_SHARES_V1

    def evaluate(
        self,
        window_open_ts: int,
        slug: str,
        signal_direction: str,
        open_price: Optional[float],
        bankroll: float,
    ) -> TakerResult:
        """
        Simulate a taker fill at the window open price.

        Parameters
        ----------
        window_open_ts  : Unix timestamp of window open.
        slug            : Market slug.
        signal_direction: "YES" | "NO" | "NONE"
        open_price      : Window open price (YES side). None → no fill.
        bankroll        : Current paper bankroll (USD).
        """
        result = TakerResult(
            window_open_ts=window_open_ts,
            slug=slug,
            signal_direction=signal_direction,
            intended_price=open_price,
        )

        if signal_direction == "NONE" or open_price is None:
            result.rejection_reason = "no_signal"
            return result

        fee_result = compute_taker_fee(open_price, self._shares, self._fee_C)
        result.fee_result = fee_result
        result.fee_per_share = fee_result.fee_per_share
        result.total_fee = fee_result.total_fee
        result.filled = True
        result.fill_price = open_price
        result.bankroll_fraction = (open_price * self._shares) / bankroll if bankroll > 0 else 0.0
        result.break_even_wr_estimate = break_even_win_rate(open_price, fee_result.fee_per_share)
        result.win_if_correct = (1.0 - open_price) * self._shares - fee_result.total_fee
        result.loss_if_wrong = open_price * self._shares + fee_result.total_fee

        logger.info(
            "[taker] window=%d slug=%s price=%.4f fee=%.6f/share total_fee=%.6f "
            "bankroll_frac=%.4f be_wr=%.4f",
            window_open_ts, slug, open_price,
            fee_result.fee_per_share, fee_result.total_fee,
            result.bankroll_fraction or 0.0,
            result.break_even_wr_estimate or 0.0,
        )
        logger.debug("[taker] %s", fee_result.formula_str)
        return result

    def settle(self, result: TakerResult, actual_outcome: str) -> TakerResult:
        """Apply settlement outcome to a TakerResult."""
        if not result.filled or result.fill_price is None:
            result.outcome_correct = None
            return result

        result.outcome_correct = (result.signal_direction == actual_outcome)

        if result.outcome_correct:
            result.gross_pnl = (1.0 - result.fill_price) * result.shares
        else:
            result.gross_pnl = -result.fill_price * result.shares

        result.net_pnl = (result.gross_pnl or 0.0) - result.total_fee

        logger.info(
            "[taker] settle window=%d outcome=%s correct=%s gross=%.4f net=%.4f fee=%.6f",
            result.window_open_ts, actual_outcome, result.outcome_correct,
            result.gross_pnl or 0.0, result.net_pnl or 0.0, result.total_fee,
        )
        return result
