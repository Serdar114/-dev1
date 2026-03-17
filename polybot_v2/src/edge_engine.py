"""
Edge engine for polybot_v2.

Takes fair probabilities, implied market prices, and fee estimates
to compute after-fee edge for each side.

This module only calculates — it never makes trade decisions.
"""

from __future__ import annotations

from typing import Optional

from fee_engine import FeeEngine
from models import EdgeResult


class EdgeEngine:
    def __init__(self, fee_engine: FeeEngine, min_after_fee_edge: float) -> None:
        self._fee = fee_engine
        self._min_edge = min_after_fee_edge

    def compute(
        self,
        fair_yes_prob: float,
        fair_no_prob: float,
        best_ask_yes: float,
        best_ask_no: float,
    ) -> tuple[EdgeResult, EdgeResult]:
        """
        Compute after-fee edge for both YES and NO sides.

        Args:
            fair_yes_prob: our model's probability that YES resolves 1
            fair_no_prob: 1 - fair_yes_prob
            best_ask_yes: cheapest YES ask (what a taker pays)
            best_ask_no: cheapest NO ask

        Returns:
            (yes_result, no_result) EdgeResult pair
        """
        yes_result = self._compute_side(
            side="yes",
            fair_prob=fair_yes_prob,
            ask_price=best_ask_yes,
        )
        no_result = self._compute_side(
            side="no",
            fair_prob=fair_no_prob,
            ask_price=best_ask_no,
        )
        return yes_result, no_result

    def _compute_side(
        self,
        side: str,
        fair_prob: float,
        ask_price: float,
    ) -> EdgeResult:
        # raw_edge = fair probability - ask price (before fee)
        raw_edge = fair_prob - ask_price

        # after-fee edge: subtract taker fee cost from expected value
        # EV of buying 1 share at ask_price:
        #   = fair_prob * 1.0 + (1-fair_prob) * 0.0 - ask_price - fee
        fee_est = self._fee.taker_estimate(ask_price)
        after_fee_edge = fair_prob - fee_est.effective_cost

        tradable = after_fee_edge >= self._min_edge
        reject_reason: Optional[str] = None
        if not tradable:
            if raw_edge <= 0:
                reject_reason = "negative_raw_edge"
            elif after_fee_edge < 0:
                reject_reason = "fee_exceeds_edge"
            else:
                reject_reason = f"edge_below_threshold({self._min_edge:.3f})"

        return EdgeResult(
            side=side,
            raw_edge=raw_edge,
            after_fee_edge=after_fee_edge,
            tradable=tradable,
            reject_reason=reject_reason,
        )

    def best_side(
        self, yes_result: EdgeResult, no_result: EdgeResult
    ) -> Optional[EdgeResult]:
        """Return the side with higher after-fee edge, if any is tradable."""
        candidates = [r for r in (yes_result, no_result) if r.tradable]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.after_fee_edge)
