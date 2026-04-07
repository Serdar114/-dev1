"""
fee_schedule.py — Fee computation from API-sourced data.

Design:
  Fee rate MUST come from the API. It may NOT be hardcoded as canonical truth.
  If fee_rate is missing, effective_fee is None, and no-trade is required.
  We separate fee_rate (input from API) from effective_fee (computed from stake).
  Maker vs taker distinction: at this phase, we are taker-only.
  Fill assumption: taker = pays fee_rate on the stake amount.

  Note on Polymarket fee structure (as of design):
    The fee is typically embedded in the spread or explicit in the API response.
    We read it from the API and do NOT assume any rate.
    If fee_rate is 0.0 from API, we log it as suspicious but accept it.
"""

from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger("polybot.fee_schedule")


class FeeSchedule:
    """
    Holds the fee schedule for one market.

    Job: compute effective fees given entry price and stake.
    Input: fee_rate (from API), provenance string.
    Output: effective_fee_usdc, effective_cost_usdc.
    Failure: if fee_rate is None, all computed values are None.

    This class never guesses a fee rate.
    """

    def __init__(
        self,
        fee_rate: Optional[float],
        fee_source: Optional[str],
        fees_enabled: Optional[bool],
    ):
        self.fee_rate = fee_rate
        self.fee_source = fee_source
        self.fees_enabled = fees_enabled

        if fee_rate is None:
            logger.warning("FeeSchedule: fee_rate is None (source=%s) — no-trade required", fee_source)
        elif fee_rate == 0.0:
            logger.warning(
                "FeeSchedule: fee_rate is 0.0 from source=%s — "
                "verify this is correct before trusting it", fee_source
            )

    def is_known(self) -> bool:
        """True only if fee_rate was received from a known source."""
        return self.fee_rate is not None and self.fee_source is not None

    def effective_fee_usdc(self, stake_usdc: float) -> Optional[float]:
        """
        Compute effective fee for a given stake.
        Returns None if fee_rate is unknown.
        fee = fee_rate * stake_usdc (applied to the amount spent, not the payout)
        """
        if self.fee_rate is None:
            return None
        return self.fee_rate * stake_usdc

    def effective_cost_usdc(self, entry_price: float, quantity: float) -> Optional[float]:
        """
        Total cost = token cost + fee.
        token_cost = entry_price * quantity
        fee = fee_rate * token_cost
        effective_cost = token_cost * (1 + fee_rate)
        Returns None if fee_rate is unknown.
        """
        if self.fee_rate is None:
            return None
        token_cost = entry_price * quantity
        return token_cost * (1.0 + self.fee_rate)

    def quantity_from_stake(self, entry_price: float, stake_usdc: float) -> Optional[float]:
        """
        Given a stake amount (USDC) and entry price, compute how many tokens.
        stake = entry_price * quantity * (1 + fee_rate)
        quantity = stake / (entry_price * (1 + fee_rate))
        Returns None if fee_rate or entry_price is unknown/zero.
        """
        if self.fee_rate is None or entry_price <= 0:
            return None
        return stake_usdc / (entry_price * (1.0 + self.fee_rate))

    def payout_if_correct(self, quantity: float) -> float:
        """Payout is always 1.0 per token on a binary outcome market."""
        return quantity * 1.0

    def net_pnl(self, stake_usdc: float, quantity: float, correct: bool) -> Optional[float]:
        """
        Net PnL for a resolved position.
        correct=True: payout = quantity * 1.0; net = payout - stake - fee
        correct=False: payout = 0; net = -stake - fee
        Returns None if fee_rate is unknown.
        """
        fee = self.effective_fee_usdc(stake_usdc)
        if fee is None:
            return None
        if correct:
            payout = self.payout_if_correct(quantity)
            return payout - stake_usdc - fee
        else:
            return -stake_usdc - fee

    def describe(self) -> str:
        return (
            f"FeeSchedule(fee_rate={self.fee_rate}, "
            f"fees_enabled={self.fees_enabled}, "
            f"source={self.fee_source})"
        )
