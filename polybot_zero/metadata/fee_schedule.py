"""
fee_schedule.py — Fee computation using Polymarket fee curve formula.

Design:
  Polymarket Crypto category fee formula (exponent = 1):
    fee = C × feeRate × p × (1 − p)
  where:
    C          = stake in USDC
    feeRate    = rate from /fee-rate?token_id endpoint (in basis points → decimal)
    p          = entry price (probability, 0 < p < 1)

  This is NOT a flat fee on stake. The fee depends on entry price p.
  At p=0.5: fee is maximised (0.25 × C × feeRate)
  At p→0 or p→1: fee → 0

  feeRate source: GET /fee-rate?token_id={token_id} → {"feeRateBps": <int>}
  feeRate decimal = feeRateBps / 10000

  FeeProvenance:
    CONFIRMED  — feeRateBps received from /fee-rate endpoint, non-zero
    ZERO       — feeRateBps received but equals 0 (suspicious, logged, accepted)
    UNRESOLVED — endpoint failed, no data, or parse error → no-trade required

  pair_sum is explicitly NOT a fee source.
"""

from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger("polybot.fee_schedule")


class FeeProvenance:
    CONFIRMED  = "CONFIRMED"   # fee rate received from API, non-zero
    ZERO       = "ZERO"        # fee rate is 0.0 from API (suspicious but accepted)
    UNRESOLVED = "UNRESOLVED"  # no fee data — no-trade required


class FeeSchedule:
    """
    Fee schedule for one token.

    Job: compute fee-adjusted costs using the Polymarket Crypto fee curve.
    Input: fee_rate_bps (int from /fee-rate endpoint), provenance.
    Output: fee_usdc, net_pnl, etc.
    Failure: if provenance is UNRESOLVED, all computed values return None.

    This class never guesses a fee rate.
    """

    def __init__(
        self,
        fee_rate_bps: Optional[int],
        provenance: str,
        token_id: Optional[str] = None,
    ):
        self.token_id = token_id
        self.fee_rate_bps = fee_rate_bps
        self.provenance = provenance

        if fee_rate_bps is None:
            self.fee_rate = None
        else:
            self.fee_rate = fee_rate_bps / 10000.0

        if provenance == FeeProvenance.UNRESOLVED:
            logger.warning(
                "FeeSchedule[%s]: UNRESOLVED — no-trade required",
                token_id or "unknown",
            )
        elif provenance == FeeProvenance.ZERO:
            logger.warning(
                "FeeSchedule[%s]: fee_rate=0.0 from API — verify this is correct",
                token_id or "unknown",
            )

    @classmethod
    def from_bps(cls, fee_rate_bps: Optional[int], token_id: Optional[str] = None) -> "FeeSchedule":
        """Build FeeSchedule from raw bps value from /fee-rate endpoint."""
        if fee_rate_bps is None:
            return cls(fee_rate_bps=None, provenance=FeeProvenance.UNRESOLVED, token_id=token_id)
        if fee_rate_bps == 0:
            return cls(fee_rate_bps=0, provenance=FeeProvenance.ZERO, token_id=token_id)
        return cls(fee_rate_bps=fee_rate_bps, provenance=FeeProvenance.CONFIRMED, token_id=token_id)

    def is_known(self) -> bool:
        """True only if fee rate is available (CONFIRMED or ZERO)."""
        return self.provenance in (FeeProvenance.CONFIRMED, FeeProvenance.ZERO)

    def fee_usdc(self, stake_usdc: float, entry_price: float) -> Optional[float]:
        """
        Compute fee in USDC for a given stake and entry price.

        Formula: fee = stake × feeRate × entry_price × (1 − entry_price)

        Returns None if fee is UNRESOLVED.
        """
        if self.fee_rate is None:
            return None
        if not (0.0 < entry_price < 1.0):
            logger.warning(
                "fee_usdc: entry_price=%.4f is outside (0, 1) — fee may be zero",
                entry_price,
            )
        return stake_usdc * self.fee_rate * entry_price * (1.0 - entry_price)

    def total_cost_usdc(self, stake_usdc: float, entry_price: float) -> Optional[float]:
        """
        Total cost = stake + fee.
        Returns None if fee is UNRESOLVED.
        """
        fee = self.fee_usdc(stake_usdc, entry_price)
        if fee is None:
            return None
        return stake_usdc + fee

    def quantity_from_stake(self, entry_price: float, stake_usdc: float) -> Optional[float]:
        """
        Tokens purchased = stake / entry_price.
        (Fee is charged separately, not deducted from tokens.)
        Returns None if entry_price is zero or fee is UNRESOLVED.
        """
        if self.fee_rate is None:
            return None
        if entry_price <= 0:
            return None
        return stake_usdc / entry_price

    def net_pnl(
        self,
        stake_usdc: float,
        entry_price: float,
        correct: bool,
    ) -> Optional[float]:
        """
        Net PnL for a resolved position.
          quantity = stake / entry_price
          payout (correct)   = quantity * 1.0 = stake / entry_price
          payout (incorrect) = 0
          fee = stake × feeRate × p × (1 − p)
          net = payout − stake − fee

        Returns None if fee is UNRESOLVED.
        """
        fee = self.fee_usdc(stake_usdc, entry_price)
        if fee is None:
            return None
        if correct:
            quantity = stake_usdc / entry_price if entry_price > 0 else 0.0
            payout = quantity * 1.0
            return payout - stake_usdc - fee
        else:
            return -stake_usdc - fee

    def describe(self) -> str:
        return (
            f"FeeSchedule(token={self.token_id} "
            f"fee_rate_bps={self.fee_rate_bps} "
            f"provenance={self.provenance})"
        )
