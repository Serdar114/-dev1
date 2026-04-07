"""
price_views.py — Derived price views from order book state.

Design:
  Compute spread, pair_sum, and structural validity views.
  pair_sum = up_best_ask + down_best_ask.
  Expected range: [0.90, 1.20] on a binary market.
  Outside this range → structural anomaly → no-trade signal.
  All functions return Optional — never crash on missing data.
"""

from __future__ import annotations
import logging
from typing import Optional, Tuple

logger = logging.getLogger("polybot.price_views")


def compute_pair_sum(up_best_ask: Optional[float], down_best_ask: Optional[float]) -> Optional[float]:
    """
    pair_sum = up_best_ask + down_best_ask.
    On a fair binary market with no fees, this should be 1.0.
    With fees, expect slightly above 1.0.
    Returns None if either input is None.
    """
    if up_best_ask is None or down_best_ask is None:
        return None
    return up_best_ask + down_best_ask


def is_pair_sum_valid(pair_sum: Optional[float], min_val: float = 0.90, max_val: float = 1.20) -> bool:
    """
    Check if pair_sum is within expected bounds.
    Outside bounds indicates: arbitrage opportunity (rare), bad data, or manipulation.
    In any case, do not trade on structurally suspicious pair sums.
    """
    if pair_sum is None:
        return False
    return min_val <= pair_sum <= max_val


def is_spread_acceptable(spread: Optional[float], max_spread: float = 0.10) -> bool:
    """
    Check if spread is within acceptable bounds.
    spread > max_spread means the cost of entry is too high relative to potential payout.
    """
    if spread is None:
        return False
    return 0.0 <= spread <= max_spread


def implied_probability(best_ask: Optional[float]) -> Optional[float]:
    """
    Implied probability of an outcome from the best ask price.
    On a $0-$1 binary, best_ask ≈ implied probability.
    Returns None if best_ask is None or invalid.
    """
    if best_ask is None or best_ask <= 0.0 or best_ask >= 1.0:
        return None
    return best_ask


def book_state_summary(
    up_best_bid: Optional[float],
    up_best_ask: Optional[float],
    down_best_bid: Optional[float],
    down_best_ask: Optional[float],
    pair_sum_min: float = 0.90,
    pair_sum_max: float = 1.20,
    max_spread: float = 0.10,
) -> dict:
    """
    Compute all derived views in one call. Returns a summary dict.
    All fields explicitly present, None where unavailable.
    """
    up_spread   = None if (up_best_bid is None or up_best_ask is None)   else (up_best_ask - up_best_bid)
    down_spread = None if (down_best_bid is None or down_best_ask is None) else (down_best_ask - down_best_bid)
    pair_sum    = compute_pair_sum(up_best_ask, down_best_ask)

    return {
        "up_best_bid":     up_best_bid,
        "up_best_ask":     up_best_ask,
        "up_spread":       up_spread,
        "down_best_bid":   down_best_bid,
        "down_best_ask":   down_best_ask,
        "down_spread":     down_spread,
        "pair_sum_best_ask": pair_sum,
        "pair_sum_valid":  is_pair_sum_valid(pair_sum, pair_sum_min, pair_sum_max),
        "up_spread_ok":    is_spread_acceptable(up_spread, max_spread),
        "down_spread_ok":  is_spread_acceptable(down_spread, max_spread),
    }
