"""
execution/taker_lane.py — Taker paper execution lane.

The taker lane simulates market-order fills on the YES side.
It is the BENCHMARK / CONTROL lane — used to compare against the maker lane
using the same signal, providing an apples-to-apples fee-impact analysis.

Sizing: FIXED 5 SHARES (v1 hard constraint).

Execution timing
----------------
The taker fill is simulated at the DECISION price, not the window-open price.
Decision price is the fast feed YES probability at the moment the signal was
evaluated (i.e., after the signal engine ran, before waiting for close).

execution_source documents where the decision price came from:
    "FAST_FEED_AT_SIGNAL"   — fast feed snapshot taken at signal evaluation time
    "CLOB_ASK_AT_SIGNAL"    — best CLOB ask at signal evaluation time (preferred)
    "UNAVAILABLE"           — price was not available; fill not simulated

assumed_slippage_bps is logged but NOT applied to P&L in v1.
It is a documented placeholder for realistic slippage modelling in v2.
Configure in settings.yaml → fees.assumed_slippage_bps.

Fee computation
---------------
Taker fee is computed using the formula from execution/fees.py:
    fee = C * p * 0.25 * (p * (1 - p))^2

This is deducted from gross P&L to produce net P&L.

Execution log fields (per window):
    filled                  : bool
    fill_price              : float (decision price)
    decision_ts             : float (unix epoch seconds at fill decision)
    execution_source        : str
    assumed_slippage_bps    : float
    shares                  : int   (always 5 in v1)
    fee_per_share           : float
    total_fee               : float
    gross_pnl               : float | None
    net_pnl                 : float | None
    bankroll_fraction       : float
    break_even_wr_estimate  : float
    win_if_correct          : float
    loss_if_wrong           : float
    outcome_correct         : bool | None
    fee_result              : TakerFeeResult  (full fee breakdown for audit)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .fees import compute_taker_fee, break_even_win_rate, TakerFeeResult

logger = logging.getLogger(__name__)

FIXED_SHARES_V1 = 5

# Execution source constants
SRC_FAST_FEED_AT_SIGNAL = "FAST_FEED_AT_SIGNAL"
SRC_CLOB_ASK_AT_SIGNAL = "CLOB_ASK_AT_SIGNAL"
SRC_UNAVAILABLE = "UNAVAILABLE"


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
    decision_ts: Optional[float] = None        # Unix epoch seconds of fill decision
    execution_source: str = SRC_UNAVAILABLE    # Documents where price came from
    assumed_slippage_bps: float = 0.0          # Documented; not applied to P&L in v1
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
    zone_eligible: bool = True   # False if decision_price is outside taker_trade_zone
    rejection_reason: Optional[str] = None
    extra: dict = field(default_factory=dict)


class TakerLane:
    """
    Taker paper execution lane (benchmark / control).

    Same signal as the maker lane, different execution model:
    - Fills at decision price (fast feed YES probability at signal time).
    - Deducts taker fee per the auditable formula in execution/fees.py.
    - Logs decision_ts and execution_source for audit.
    """

    def __init__(self, config: dict) -> None:
        self._fee_C = config.get("fees", {}).get("taker_fee_C", 0.02)
        self._assumed_slippage_bps = float(
            config.get("fees", {}).get("assumed_slippage_bps", 0)
        )
        self._shares = FIXED_SHARES_V1
        # Trade zone: taker fills only within [zone_low, zone_high].
        # Prices inside the research zone (extreme_zone gate) but outside this
        # band are logged as "inside research zone but outside taker trade zone".
        zone_cfg = config.get("trade_zones", {})
        self._zone_low = float(zone_cfg.get("taker_trade_zone_low", 0.80))
        self._zone_high = float(zone_cfg.get("taker_trade_zone_high", 0.92))

    def evaluate(
        self,
        window_open_ts: int,
        slug: str,
        signal_direction: str,
        decision_price: Optional[float],
        bankroll: float,
        decision_ts: Optional[float] = None,
        execution_source: str = SRC_FAST_FEED_AT_SIGNAL,
    ) -> TakerResult:
        """
        Simulate a taker fill at the decision price.

        Parameters
        ----------
        window_open_ts   : Unix timestamp of window open.
        slug             : Market slug.
        signal_direction : "YES" | "NO" | "NONE"
        decision_price   : YES probability price at signal evaluation time.
                           This is the fast feed snapshot (or CLOB ask if available)
                           at the moment we decided to take liquidity.
                           NOT the window-open price — these can differ if signal
                           evaluation is delayed.
        bankroll         : Current paper bankroll (USD).
        decision_ts      : Unix epoch seconds of the fill decision.
                           None → auto-filled with time.time() at call time.
        execution_source : Documents where decision_price came from.
                           Use SRC_FAST_FEED_AT_SIGNAL, SRC_CLOB_ASK_AT_SIGNAL,
                           or SRC_UNAVAILABLE.
        """
        captured_ts = decision_ts if decision_ts is not None else time.time()

        result = TakerResult(
            window_open_ts=window_open_ts,
            slug=slug,
            signal_direction=signal_direction,
            intended_price=decision_price,
            decision_ts=captured_ts,
            execution_source=execution_source,
            assumed_slippage_bps=self._assumed_slippage_bps,
        )

        if signal_direction == "NONE" or decision_price is None:
            result.rejection_reason = "no_signal_or_price_unavailable"
            result.execution_source = SRC_UNAVAILABLE
            return result

        # Price-space guard: decision_price must be a YES probability in (0, 1).
        # BTC/USD prices (e.g. 94000.0) must NEVER reach here.
        # Passing BTC/USD would corrupt the fee formula: p*(1-p) ≈ 94000*(-93999).
        if not (0.0 < decision_price < 1.0):
            raise ValueError(
                f"TakerLane.evaluate: decision_price={decision_price!r} is outside (0, 1). "
                "decision_price must be a Polymarket YES probability (e.g. 0.87). "
                "BTC/USD prices must NEVER be passed here. "
                "Use YesPriceSnapshot from feeds/yes_price_adapter.py to obtain the "
                "YES probability before calling this method."
            )

        # Trade zone gate: reject fills outside configured taker trade zone.
        # This is narrower than the research zone (extreme_zone signal gate).
        # A window can be a signal candidate while still being outside trade zone.
        if not (self._zone_low <= decision_price <= self._zone_high):
            result.zone_eligible = False
            result.rejection_reason = (
                f"taker_zone:{decision_price:.4f}_outside_"
                f"[{self._zone_low:.2f},{self._zone_high:.2f}]"
            )
            logger.debug(
                "[taker] zone rejection window=%d price=%.4f zone=[%.2f, %.2f]",
                window_open_ts, decision_price, self._zone_low, self._zone_high,
            )
            return result

        fee_result = compute_taker_fee(decision_price, self._shares, self._fee_C)
        result.fee_result = fee_result
        result.fee_per_share = fee_result.fee_per_share
        result.total_fee = fee_result.total_fee
        result.filled = True
        result.fill_price = decision_price
        result.bankroll_fraction = (decision_price * self._shares) / bankroll if bankroll > 0 else 0.0
        result.break_even_wr_estimate = break_even_win_rate(decision_price, fee_result.fee_per_share)
        result.win_if_correct = (1.0 - decision_price) * self._shares - fee_result.total_fee
        result.loss_if_wrong = decision_price * self._shares + fee_result.total_fee

        logger.info(
            "[taker] window=%d slug=%s price=%.4f src=%s decision_ts=%.3f "
            "slippage=%dbps fee=%.6f/share total_fee=%.6f bankroll_frac=%.4f be_wr=%.4f",
            window_open_ts, slug, decision_price, execution_source, captured_ts,
            int(self._assumed_slippage_bps),
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
            "[taker] settle window=%d outcome=%s correct=%s "
            "gross=%.4f net=%.4f fee=%.6f src=%s",
            result.window_open_ts, actual_outcome, result.outcome_correct,
            result.gross_pnl or 0.0, result.net_pnl or 0.0, result.total_fee,
            result.execution_source,
        )
        return result
