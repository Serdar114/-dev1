"""
Fee engine for polybot_v2.

Computes taker and maker fee/rebate for after-fee entry economics.
This module only calculates — it never makes trade decisions.

Fee model (Polymarket, Phase 1):
  Taker: entry_price * taker_fee_rate on the notional bought
  Maker: post-only; rebate applied on fill (Phase 1 rebate = 0 by default)

All values are per-unit (per share / per $1 notional).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TakerFeeEstimate:
    entry_price: float        # price paid per share (0-1 range)
    taker_fee_rate: float     # e.g. 0.02 for 2%
    fee_per_share: float      # cost in USDC per share
    effective_cost: float     # entry_price + fee_per_share
    breakeven_price: float    # price at which trade is flat (incl. fee)


@dataclass
class MakerFeeEstimate:
    quote_price: float        # posted price
    rebate_rate: float        # e.g. 0.0 in Phase 1
    rebate_per_share: float   # credit in USDC per share on fill
    effective_proceeds: float # quote_price + rebate_per_share


class FeeEngine:
    """
    Stateless fee calculator.
    All rates come from config at construction time.
    """

    def __init__(self, taker_fee_rate: float, maker_rebate_rate: float) -> None:
        if taker_fee_rate < 0 or taker_fee_rate >= 1:
            raise ValueError(f"taker_fee_rate must be in [0, 1), got {taker_fee_rate}")
        if maker_rebate_rate < 0:
            raise ValueError(f"maker_rebate_rate must be >= 0, got {maker_rebate_rate}")
        self.taker_fee_rate = taker_fee_rate
        self.maker_rebate_rate = maker_rebate_rate

    # ------------------------------------------------------------------ #
    # Taker
    # ------------------------------------------------------------------ #

    def taker_estimate(self, entry_price: float) -> TakerFeeEstimate:
        """
        Estimate taker cost for buying 1 share at entry_price.

        Polymarket charges taker_fee_rate * entry_price on the entry side.
        Selling (when market resolves) is also subject to fee on the payout.
        Here we model the round-trip cost as 2× one-side fee for conservatism,
        since the payout is 1.0 (win) or 0.0 (loss).

        For Phase 1 we use single-side entry fee only (entry side),
        because resolution payouts are net of no additional fee on Polymarket.
        """
        fee_per_share = entry_price * self.taker_fee_rate
        effective_cost = entry_price + fee_per_share
        # breakeven: need price to reach effective_cost to be flat
        breakeven_price = effective_cost
        return TakerFeeEstimate(
            entry_price=entry_price,
            taker_fee_rate=self.taker_fee_rate,
            fee_per_share=fee_per_share,
            effective_cost=effective_cost,
            breakeven_price=breakeven_price,
        )

    def taker_after_fee_pnl_per_share(
        self, entry_price: float, outcome: float
    ) -> float:
        """
        PnL per share given outcome (1.0 = win, 0.0 = loss).
        outcome is the resolution value (YES=1, NO=0).
        """
        fee = entry_price * self.taker_fee_rate
        return outcome - entry_price - fee

    # ------------------------------------------------------------------ #
    # Maker / shadow
    # ------------------------------------------------------------------ #

    def maker_estimate(self, quote_price: float) -> MakerFeeEstimate:
        """
        Estimate maker economics for a theoretical post-only quote.
        Phase 1: rebate = 0, so effective_proceeds == quote_price.
        """
        rebate_per_share = quote_price * self.maker_rebate_rate
        return MakerFeeEstimate(
            quote_price=quote_price,
            rebate_rate=self.maker_rebate_rate,
            rebate_per_share=rebate_per_share,
            effective_proceeds=quote_price + rebate_per_share,
        )

    # ------------------------------------------------------------------ #
    # Edge computation helpers (used by edge_engine)
    # ------------------------------------------------------------------ #

    def taker_edge_yes(self, fair_yes_prob: float, ask_yes: float) -> float:
        """
        Raw after-fee expected value of buying YES at ask_yes.
        EV = fair_yes_prob * 1.0 + (1 - fair_yes_prob) * 0.0 - effective_cost
           = fair_yes_prob - (ask_yes + ask_yes * fee_rate)
        """
        est = self.taker_estimate(ask_yes)
        return fair_yes_prob - est.effective_cost

    def taker_edge_no(self, fair_no_prob: float, ask_no: float) -> float:
        """After-fee EV of buying NO at ask_no."""
        est = self.taker_estimate(ask_no)
        return fair_no_prob - est.effective_cost
