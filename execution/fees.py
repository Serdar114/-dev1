"""
execution/fees.py — Auditable fee model for paper execution.

============================================================
FEE MODEL SPECIFICATION
============================================================

MAKER LANE
----------
  maker_fee = 0

  Rationale: Polymarket's CLOB has historically offered zero (or near-zero)
  fees for passive liquidity providers (limit orders that rest in the book).
  We model this as a strict zero-cost entry for the maker evaluation lane.

  Implication: Maker P&L is gross of fees.  Any advantage over the taker
  lane is directly attributable to avoiding taker costs.

TAKER LANE
----------
  fee = C * p * 0.25 * (p * (1 - p))^2

  Symbol definitions:
    p   = execution price of the YES share (0 < p < 1)
    C   = fee constant (see below)

  Why this formula?
    The leading p term scales the fee with the price of the position —
    higher-priced YES shares attract a larger absolute fee, reflecting
    that more capital is at risk.
    The trailing 0.25 * (p*(1-p))^2 captures the price-variance shape:
      - At p = 0.5 the variance term peaks at 0.25 * (0.25)^2 = 0.015625.
      - At p near 0 or 1 the term → 0 (thin markets, small absolute value).
    Combining: fee is highest for mid-to-high-probability positions.

  Numeric examples (C=0.02, 5 shares):
    p = 0.50: fee_per_share = 0.02 * 0.50 * 0.25 * (0.50*0.50)^2
                            = 0.02 * 0.50 * 0.25 * 0.0625
                            = 0.000156250
              total_fee (5 shares) = 0.000781
    p = 0.85: fee_per_share = 0.02 * 0.85 * 0.25 * (0.85*0.15)^2
                            = 0.02 * 0.85 * 0.25 * (0.1275)^2
                            = 0.02 * 0.85 * 0.25 * 0.016256
                            = 0.000069
              total_fee (5 shares) = 0.000345

  Derivation of C:
    Polymarket CLOB documented taker fee:
      2% of potential winnings on the filled leg.
    For a YES share bought at price p:
      Potential winnings per share = (1 - p)
      Taker fee per share          = 0.02 * (1 - p)                  ...(A)

    The formula above at C=0.02:
      fee = 0.02 * p * 0.25 * (p*(1-p))^2

    Note: formula (A) and the cubic form are NOT identical.
    We use the cubic form as specified in the spec addendum.
    The leading p term makes it more conservative at high-probability
    positions than the simple linear form (A).

    C = 0.02 is the default, drawn from the Polymarket CLOB documentation.
    Override via config `fees.taker_fee_C` to run sensitivity analysis.

  CAUTION: This formula is not directly derived from Polymarket's
  published fee schedule.  It is a model imposed by the spec addendum.
  Results are only comparable within this framework.  Do not interpret the
  fee output as an exact exchange charge.

AUDITING
--------
  All fee computations are visible here and nowhere else.
  Execution code imports and calls these functions; it does not re-implement
  fee logic.  Any fee change requires only modifying this file.
============================================================
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default taker fee constant C.
# Source: Polymarket CLOB documentation (taker = 2% of potential winnings).
# See derivation above.
DEFAULT_TAKER_C = 0.02


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TakerFeeResult:
    """
    Breakdown of a taker fee computation for a single order.

    Attributes
    ----------
    price           Execution price p (YES side, 0 < p < 1).
    shares          Number of shares.
    C               Fee constant used in the computation.
    fee_per_share   Fee in USD per share:  C * p * 0.25 * (p*(1-p))^2
    total_fee       fee_per_share * shares
    formula_str     Human-readable formula string for logs / audits.
    """
    price: float
    shares: int
    C: float
    fee_per_share: float
    total_fee: float
    formula_str: str


def maker_fee(shares: int) -> float:
    """
    Maker fee for an order of `shares` shares.

    Always returns 0.0.  Maker fee = 0 by spec.
    """
    _ = shares   # explicitly acknowledged — no computation needed
    return 0.0


def compute_taker_fee(price: float, shares: int, C: float = DEFAULT_TAKER_C) -> TakerFeeResult:
    """
    Compute the taker fee for a YES-side fill.

    Formula (from spec addendum):
        fee_per_share = C * p * 0.25 * (p * (1 - p))^2

    The leading p term scales the fee with the price of the position.
    At p=0.50, C=0.02: fee_per_share = 0.02 * 0.50 * 0.25 * 0.0625 = 0.000156
    At p=0.85, C=0.02: fee_per_share = 0.02 * 0.85 * 0.25 * 0.01626 = 0.000069

    Parameters
    ----------
    price  : float — Execution price p of the YES share (0 < p < 1).
    shares : int   — Number of shares.
    C      : float — Fee constant (default = 0.02, see module docstring).

    Returns
    -------
    TakerFeeResult with full breakdown.
    """
    if not (0.0 < price < 1.0):
        raise ValueError(f"price must be in (0, 1), got {price}")
    if shares < 1:
        raise ValueError(f"shares must be >= 1, got {shares}")

    inner = price * (1.0 - price)
    fee_per_share = C * price * 0.25 * (inner ** 2)
    total_fee = fee_per_share * shares

    formula_str = (
        f"fee = {C} * {price:.4f} * 0.25 * ({price:.4f} * (1 - {price:.4f}))^2 "
        f"= {fee_per_share:.8f} per share × {shares} shares = {total_fee:.8f} USD"
    )

    return TakerFeeResult(
        price=price,
        shares=shares,
        C=C,
        fee_per_share=fee_per_share,
        total_fee=total_fee,
        formula_str=formula_str,
    )


def break_even_win_rate(price: float, fee_per_share: float) -> float:
    """
    Compute break-even win rate for a binary YES position.

    At break-even:
        WR * (1 - price) = (1 - WR) * price + fee_per_share

    Solving:
        WR = (price + fee_per_share) / 1.0
           = price + fee_per_share          (since payout is 1 per winning share)

    For maker (fee_per_share = 0):
        break_even_wr = price

    Parameters
    ----------
    price         : float — Entry price of YES share.
    fee_per_share : float — Fee per share (0 for maker, >0 for taker).

    Returns
    -------
    Float in [0, 1] representing the minimum win rate needed to break even.
    """
    return price + fee_per_share
