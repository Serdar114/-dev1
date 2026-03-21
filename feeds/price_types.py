"""
feeds/price_types.py — Typed price-space containers with validation guards.

Two distinct price spaces are used in this codebase and must NEVER be mixed:

1. BTC/USD spot prices (from RTDS feeds)
   - Always >> 1 (e.g. 94000.0)
   - Used for: direction computation, basis mismatch, settlement
   - Container: BTCSpotSnapshot

2. Polymarket YES probability prices (from CLOB)
   - Always in (0, 1) (e.g. 0.87)
   - Used for: extreme zone gate, spread quality, taker fee, maker quote
   - Container: YesPriceSnapshot

Both containers validate their price invariants in __post_init__ and raise
ValueError if a price from the wrong space is detected.  This catches bugs
like passing BTC/USD (94000.0) where YES probability (0.87) is expected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class BTCSpotSnapshot:
    """
    A single BTC/USD spot price observation from an RTDS feed.

    price_usd MUST be > 1.0.  Values in (0, 1) indicate a YES probability
    has been incorrectly passed as a BTC/USD price — raises ValueError.

    Attributes
    ----------
    price_usd   : BTC/USD spot price (e.g. 94000.0). Must be > 1.0.
    timestamp   : Unix epoch seconds of the observation.
    feed_name   : "fast" | "chainlink" — which RTDS channel this came from.
    is_stale    : True if the feed has not updated within the stale threshold.
    gap_seconds : Seconds since the last feed tick (None if no prior tick).
    """
    price_usd: float
    timestamp: float
    feed_name: str
    is_stale: bool = False
    gap_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if self.price_usd <= 1.0:
            raise ValueError(
                f"BTCSpotSnapshot.price_usd={self.price_usd!r} is <= 1.0. "
                "This looks like a YES probability (0-1 range) passed as a "
                "BTC/USD price. BTC/USD prices are always >> 1 (e.g. 94000.0). "
                "Check the caller — you may have mixed up price spaces."
            )


@dataclass(frozen=True)
class YesPriceSnapshot:
    """
    A single Polymarket YES-side probability price observation from the CLOB.

    probability MUST be in (0, 1) exclusive.  Values > 1.0 indicate a BTC/USD
    spot price has been incorrectly passed as a YES probability — raises ValueError.

    Attributes
    ----------
    probability     : YES-side mid/ask price as a probability in (0, 1).
                      e.g. 0.87 means "87% chance YES resolves 1".
    timestamp       : Unix epoch seconds of the observation.
    source          : "clob_midpoint" | "clob_ask" | "clob_book" — CLOB source.
    is_provisional  : True if this is a REST-polled estimate (not live WebSocket).
                      Provisional prices should be flagged in fill grading.
    token_id        : Polymarket CLOB token_id for the YES side (for audit).
    """
    probability: float
    timestamp: float
    source: str
    is_provisional: bool = True     # REST polling is provisional by default
    token_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not (0.0 < self.probability < 1.0):
            raise ValueError(
                f"YesPriceSnapshot.probability={self.probability!r} is outside (0, 1). "
                "YES probability must be strictly between 0 and 1. "
                "If this is a BTC/USD price (e.g. 94000.0), you have mixed up "
                "price spaces. Check the caller."
            )
