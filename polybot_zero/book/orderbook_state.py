"""
orderbook_state.py — Per-token order book state manager.

Design:
  Wraps OrderBookSnapshot with validation helpers.
  Does not fetch data — that is market_ws_client's job.
  Provides validity checks for: min levels, spread, age.
  All accessors return Optional — never raise on missing book.
"""

from __future__ import annotations
import logging
import time
from typing import Optional, Dict

from loggingx.schemas import OrderBookSnapshot, PriceLevel

logger = logging.getLogger("polybot.orderbook_state")


class OrderBookState:
    """
    Manages the current order book for a single token.

    Job: hold latest snapshot, provide validated access to best bid/ask.
    Input: OrderBookSnapshot from market_ws_client
    Output: best_bid, best_ask, spread, validity check
    Failure: missing book → None returned on all accessors
    """

    def __init__(
        self,
        token_id: str,
        staleness_threshold_secs: float = 30.0,
        min_bid_levels: int = 1,
        min_ask_levels: int = 1,
    ):
        self.token_id = token_id
        self._staleness_threshold = staleness_threshold_secs
        self._min_bid_levels = min_bid_levels
        self._min_ask_levels = min_ask_levels
        self._book: Optional[OrderBookSnapshot] = None

    def update(self, book: OrderBookSnapshot) -> None:
        """Accept a new book snapshot."""
        self._book = book

    def has_book(self) -> bool:
        return self._book is not None

    def is_stale(self) -> bool:
        if self._book is None:
            return True
        return (time.time() - self._book.updated_at) > self._staleness_threshold

    def is_valid(self) -> bool:
        """
        True if book exists, is fresh, and has minimum levels on both sides.
        """
        if self._book is None or self.is_stale():
            return False
        return self._book.is_valid(self._min_bid_levels, self._min_ask_levels)

    def best_bid(self) -> Optional[float]:
        if self._book is None:
            return None
        return self._book.best_bid()

    def best_ask(self) -> Optional[float]:
        if self._book is None:
            return None
        return self._book.best_ask()

    def spread(self) -> Optional[float]:
        if self._book is None:
            return None
        return self._book.spread()

    def age_secs(self) -> Optional[float]:
        if self._book is None:
            return None
        return time.time() - self._book.updated_at

    def snapshot(self) -> Optional[OrderBookSnapshot]:
        return self._book
