"""
Fee engine for polybot_v2.

Computes taker and maker fee/rebate for after-fee entry economics.
This module only calculates — it never makes trade decisions.

Fee model (Polymarket crypto markets):
  fee_per_share = p * fee_rate_base * (p * (1 - p)) ^ fee_exponent

  With fee_rate_base=0.25, fee_exponent=2:
    p=0.50 -> 0.0078125 USDC/share
    p=0.90 -> 0.0018225 USDC/share
    p=0.10 -> 0.0002025 USDC/share

All values are per-unit (per share / per $1 notional).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TakerFeeEstimate:
    entry_price: float        # price paid per share (0-1 range)
    fee_per_share: float      # cost in USDC per share
    effective_cost: float     # entry_price + fee_per_share
    effective_rate: float     # fee_per_share / entry_price (for logging)
    breakeven_price: float    # price at which trade is flat (incl. fee)


@dataclass
class MakerFeeEstimate:
    quote_price: float        # posted price
    maker_rebate_share: float # rebate fraction (informational in Phase 1)
    rebate_per_share: float   # credit in USDC per share on fill (Phase 1 = 0)
    effective_proceeds: float # quote_price + rebate_per_share


class FeeEngine:
    """
    Stateless fee calculator using the Polymarket crypto fee curve.

    fee_per_share = p * fee_rate_base * (p * (1 - p)) ^ fee_exponent
    """

    def __init__(
        self,
        fee_rate_base: float,
        fee_exponent: float,
        maker_rebate_share: float = 0.0,
    ) -> None:
        if fee_rate_base < 0:
            raise ValueError(f"fee_rate_base must be >= 0, got {fee_rate_base}")
        if fee_exponent < 0:
            raise ValueError(f"fee_exponent must be >= 0, got {fee_exponent}")
        if maker_rebate_share < 0:
            raise ValueError(f"maker_rebate_share must be >= 0, got {maker_rebate_share}")
        self.fee_rate_base = fee_rate_base
        self.fee_exponent = fee_exponent
        self.maker_rebate_share = maker_rebate_share

    # ------------------------------------------------------------------ #
    # Taker
    # ------------------------------------------------------------------ #

    def taker_estimate(self, entry_price: float) -> TakerFeeEstimate:
        """
        Estimate taker cost for buying 1 share at entry_price.

        fee_per_share = p * fee_rate_base * (p * (1 - p)) ^ fee_exponent

        This is the fee-equivalent for Phase 1 paper trading.
        No actual share deduction is modelled; but fee-equivalent must be correct
        so that after-fee edge calculations reflect real Polymarket economics.
        """
        p = entry_price
        fee_per_share = p * self.fee_rate_base * (p * (1.0 - p)) ** self.fee_exponent
        effective_cost = p + fee_per_share
        effective_rate = fee_per_share / p if p > 0 else 0.0
        breakeven_price = effective_cost
        return TakerFeeEstimate(
            entry_price=p,
            fee_per_share=fee_per_share,
            effective_cost=effective_cost,
            effective_rate=effective_rate,
            breakeven_price=breakeven_price,
        )

    def taker_after_fee_pnl_per_share(
        self, entry_price: float, outcome: float
    ) -> float:
        """
        PnL per share given outcome (1.0 = win, 0.0 = loss).
        outcome is the resolution value (YES=1, NO=0).
        """
        fee = self.taker_estimate(entry_price).fee_per_share
        return outcome - entry_price - fee

    # ------------------------------------------------------------------ #
    # Maker / shadow
    # ------------------------------------------------------------------ #

    def maker_estimate(self, quote_price: float) -> MakerFeeEstimate:
        """
        Estimate maker economics for a theoretical post-only quote.
        Phase 1: rebate tracked as informational only (rebate_per_share = 0).
        maker_rebate_share is stored for future reference.
        """
        rebate_per_share = 0.0  # Phase 1: no live rebate deduction
        return MakerFeeEstimate(
            quote_price=quote_price,
            maker_rebate_share=self.maker_rebate_share,
            rebate_per_share=rebate_per_share,
            effective_proceeds=quote_price + rebate_per_share,
        )

    # ------------------------------------------------------------------ #
    # Edge computation helpers (used by edge_engine)
    # ------------------------------------------------------------------ #

    def taker_edge_yes(self, fair_yes_prob: float, ask_yes: float) -> float:
        """
        After-fee expected value of buying YES at ask_yes.
        EV = fair_yes_prob - effective_cost
        """
        est = self.taker_estimate(ask_yes)
        return fair_yes_prob - est.effective_cost

    def taker_edge_no(self, fair_no_prob: float, ask_no: float) -> float:
        """After-fee EV of buying NO at ask_no."""
        est = self.taker_estimate(ask_no)
        return fair_no_prob - est.effective_cost
