"""
discovery/market_registry.py — Active market registry.

Holds the current active MarketRecord.
Detects window rollovers and triggers rediscovery.
Thread-safe.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from state import MarketRecord
from truth.window_clock import current_window_start


class MarketRegistry:
    """
    Stores the currently active market.
    Provides rollover detection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._market: Optional[MarketRecord] = None
        self._known_window_start: int = 0

    def set(self, market: Optional[MarketRecord]) -> None:
        with self._lock:
            self._market = market
            self._known_window_start = market.window_start if market else 0

    def get(self) -> Optional[MarketRecord]:
        with self._lock:
            return self._market

    def needs_rediscovery(self) -> bool:
        """
        Returns True if:
          - No market is registered, OR
          - Current window start has advanced past the registered market's window.
        """
        with self._lock:
            if self._market is None:
                return True
            cur = current_window_start()
            return cur > self._known_window_start

    def clear(self) -> None:
        with self._lock:
            self._market = None
            self._known_window_start = 0
