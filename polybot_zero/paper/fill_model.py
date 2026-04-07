"""
fill_model.py — Explicit fill model for hypothetical and paper trades.

Design:
  The fill model defines WHAT price we assume a taker trade fills at.
  It must be explicit about:
    - what assumption is used
    - where it is optimistic
    - where it is conservative
    - how it should be stress-tested later

  Current model: TAKER_BEST_ASK
    Assumption: fill at the current best ask price.
    Conservative vs reality: assumes full liquidity at best ask.
    Optimistic vs reality: real fills may slip beyond best ask under volume.
    For small wallet ($5 stake), slippage is minimal on liquid markets.
    For very thin books, best ask fill is unrealistically optimistic.

  Stress test plan (for later):
    - Record actual book depth at hypothetical entry time
    - Compute hypothetical slippage if order was larger
    - Compare hypothetical fill to post-entry price movement

  This module does NOT decide WHEN to fill — that is no_trade_rules.
  This module does NOT decide IF to fill — that is paper_executor.
  This module only defines the fill price and quantity math.
"""

from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger("polybot.fill_model")

FILL_MODEL_NAME = "taker_best_ask"
FILL_IS_OPTIMISTIC = True   # True = may be unrealistically good in thin books
FILL_OPTIMISM_NOTE = (
    "Assumes full fill at best ask. Optimistic for thin books. "
    "Conservative vs larger orders. Realistic for small wallet on liquid markets."
)


def compute_fill(
    best_ask: Optional[float],
    stake_usdc: float,
    fee_rate: Optional[float],
) -> dict:
    """
    Compute hypothetical fill details for a taker entry.

    Args:
        best_ask:   Best ask price on the outcome token (0.0–1.0 range)
        stake_usdc: USDC to deploy (NOT including fee)
        fee_rate:   Fee rate from API (e.g. 0.02 for 2%). None = fee unknown.

    Returns:
        dict with all fill details explicitly set. None where computation impossible.

    Fill math:
        quantity = stake_usdc / best_ask  (number of tokens bought)
        fee_usdc = fee_rate * stake_usdc  (fee on stake)
        effective_cost = stake_usdc + fee_usdc  (total USDC spent)
        max_payout = quantity * 1.0  (if outcome is correct)
        min_net_pnl = max_payout - effective_cost  (if correct)
        loss_net_pnl = -effective_cost  (if incorrect)
    """
    if best_ask is None or best_ask <= 0.0 or best_ask >= 1.0:
        return {
            "fill_model":         FILL_MODEL_NAME,
            "fill_is_optimistic": FILL_IS_OPTIMISTIC,
            "fill_valid":         False,
            "fill_error":         f"invalid_best_ask:{best_ask}",
            "entry_price":        best_ask,
            "stake_usdc":         stake_usdc,
            "quantity":           None,
            "fee_rate":           fee_rate,
            "fee_usdc":           None,
            "effective_cost":     None,
            "max_payout":         None,
            "net_pnl_if_correct": None,
            "net_pnl_if_wrong":   None,
        }

    quantity = stake_usdc / best_ask

    if fee_rate is not None:
        fee_usdc = fee_rate * stake_usdc
        effective_cost = stake_usdc + fee_usdc
    else:
        fee_usdc = None
        effective_cost = None

    max_payout = quantity * 1.0

    net_pnl_if_correct = (max_payout - effective_cost) if effective_cost is not None else None
    net_pnl_if_wrong   = (-effective_cost) if effective_cost is not None else None

    return {
        "fill_model":         FILL_MODEL_NAME,
        "fill_is_optimistic": FILL_IS_OPTIMISTIC,
        "fill_optimism_note": FILL_OPTIMISM_NOTE,
        "fill_valid":         True,
        "fill_error":         None,
        "entry_price":        best_ask,
        "stake_usdc":         stake_usdc,
        "quantity":           quantity,
        "fee_rate":           fee_rate,
        "fee_usdc":           fee_usdc,
        "effective_cost":     effective_cost,
        "max_payout":         max_payout,
        "net_pnl_if_correct": net_pnl_if_correct,
        "net_pnl_if_wrong":   net_pnl_if_wrong,
    }
