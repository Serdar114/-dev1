"""
Stake policy for polybot_v2.

Bankroll-linked, conservative sizing.
Anti-martingale: cut size on drawdown.
Scale tiers: grow fraction as bankroll grows.
No aggressive compounding in Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class StakeDecision:
    suggested_notional: float    # USDC to risk
    suggested_shares: float      # shares to buy (notional / entry_price)
    scale_tier: int              # which tier applied (0-indexed)
    fraction_used: float         # bankroll fraction used
    capped: bool                 # True if min/max cap applied


class StakePolicy:
    def __init__(
        self,
        min_notional: float,
        max_risk_fraction: float,
        drawdown_reduce_factor: float,
        scale_tiers: list[dict],
    ) -> None:
        self.min_notional = min_notional
        self.max_risk_fraction = max_risk_fraction
        self.drawdown_reduce_factor = drawdown_reduce_factor
        # sort tiers by min_bankroll
        self._tiers = sorted(scale_tiers, key=lambda t: t["min_bankroll"])

    def compute(
        self,
        bankroll: float,
        entry_price: float,
        last_drawdown: float = 0.0,
        lane: str = "selective_taker",
    ) -> StakeDecision:
        """
        Determine position size for one trade.

        Args:
            bankroll: current USDC bankroll
            entry_price: price per share (used to convert notional → shares)
            last_drawdown: current drawdown fraction (0.0–1.0)
            lane: "selective_taker" or "maker_shadow" (shadow has smaller size)
        """
        if bankroll <= 0 or entry_price <= 0:
            return StakeDecision(0.0, 0.0, 0, 0.0, True)

        base_fraction = self._tier_fraction(bankroll)
        tier_idx = self._tier_index(bankroll)

        # Drawdown penalty: halve size if in drawdown
        if last_drawdown > 0.0:
            base_fraction *= (1.0 - last_drawdown * self.drawdown_reduce_factor)
            base_fraction = max(base_fraction, 0.005)  # floor at 0.5%

        # Shadow lane: use half the taker size (observation only)
        if lane == "maker_shadow":
            base_fraction *= 0.5

        # Hard cap – mark capped if tier fraction exceeded max_risk_fraction
        capped = False
        if base_fraction > self.max_risk_fraction:
            base_fraction = self.max_risk_fraction
            capped = True

        notional = bankroll * base_fraction

        if notional < self.min_notional:
            notional = self.min_notional
            capped = True

        max_notional = bankroll * self.max_risk_fraction
        if notional > max_notional:
            notional = max_notional
            capped = True

        shares = notional / entry_price if entry_price > 0 else 0.0

        return StakeDecision(
            suggested_notional=round(notional, 4),
            suggested_shares=round(shares, 4),
            scale_tier=tier_idx,
            fraction_used=base_fraction,
            capped=capped,
        )

    def _tier_fraction(self, bankroll: float) -> float:
        for tier in reversed(self._tiers):
            if bankroll >= tier["min_bankroll"]:
                return float(tier["fraction"])
        # below all tiers: use smallest tier fraction
        if self._tiers:
            return float(self._tiers[0]["fraction"])
        return 0.02  # fallback

    def _tier_index(self, bankroll: float) -> int:
        for i, tier in enumerate(reversed(self._tiers)):
            if bankroll >= tier["min_bankroll"]:
                return len(self._tiers) - 1 - i
        return 0
