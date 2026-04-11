"""
truth/chainlink_buffer.py — Rolling buffer of Chainlink oracle observations.

Enables exact window-close capture for resolution.

Problem solved:
  - resolution_truth.py previously used "current live snapshot at rollover time"
  - rollover happens 1-5 seconds AFTER window_end
  - current snapshot may reflect an oracle update that happened AFTER window_end
  - that would resolve using a price the market never saw during the window

Fix:
  - Every Chainlink poll (Polygon RPC or RTDS) records an observation here
  - At resolution time, find the last observation with oracle_updated_at <= window_end
  - Report exact timing metadata: capture_ts, seconds_from_window_end, source
  - If no observation is close enough to window_end: UNRESOLVED (never guess)
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class Observation:
    oracle_updated_at: float  # timestamp from oracle contract (canonical)
    price: float
    fetched_at: float         # when we received this reading
    source: str               # "polygon_rpc" | "rtds"


@dataclass
class CloseCapture:
    oracle_updated_at: float
    price: float
    fetched_at: float
    source: str
    seconds_before_window_end: float  # window_end - oracle_updated_at; >= 0 means before close


class ChainlinkBuffer:
    """
    Thread-safe rolling buffer of Chainlink observations.
    Stores up to max_entries readings (default 180 = 15 min at 5s poll).

    Used exclusively for window-close resolution.
    Not a live price feed — see ChainlinkClient / RtdsChainlinkClient for that.
    """

    def __init__(self, max_entries: int = 180) -> None:
        self._lock = threading.Lock()
        self._obs: List[Observation] = []
        self._max = max_entries

    def record(
        self,
        oracle_updated_at: float,
        price: float,
        fetched_at: float,
        source: str,
    ) -> None:
        """Add an observation. Drops oldest when full. Thread-safe."""
        with self._lock:
            # Deduplicate: don't add if oracle_updated_at already recorded
            if self._obs and self._obs[-1].oracle_updated_at == oracle_updated_at:
                return
            self._obs.append(Observation(
                oracle_updated_at=oracle_updated_at,
                price=price,
                fetched_at=fetched_at,
                source=source,
            ))
            if len(self._obs) > self._max:
                self._obs.pop(0)

    def best_close_capture(
        self,
        window_end: int,
        max_age_before_end: float = 300.0,
    ) -> Optional[CloseCapture]:
        """
        Find the best Chainlink observation for resolving a window close.

        Criteria:
          1. oracle_updated_at <= window_end  (strictly at or before window closed)
          2. oracle_updated_at >= window_end - max_age_before_end  (not too old)
          3. Among candidates: pick the one with the largest oracle_updated_at
             (most recent update before close)

        Returns None if no observation meets criteria.
        Caller must treat None as UNRESOLVED.
        """
        with self._lock:
            candidates = [
                o for o in self._obs
                if o.oracle_updated_at <= window_end
                and o.oracle_updated_at >= window_end - max_age_before_end
            ]
        if not candidates:
            return None
        best = max(candidates, key=lambda o: o.oracle_updated_at)
        return CloseCapture(
            oracle_updated_at=best.oracle_updated_at,
            price=best.price,
            fetched_at=best.fetched_at,
            source=best.source,
            seconds_before_window_end=window_end - best.oracle_updated_at,
        )

    def depth(self) -> int:
        with self._lock:
            return len(self._obs)

    def oldest_ts(self) -> Optional[float]:
        with self._lock:
            return self._obs[0].oracle_updated_at if self._obs else None

    def newest_ts(self) -> Optional[float]:
        with self._lock:
            return self._obs[-1].oracle_updated_at if self._obs else None
