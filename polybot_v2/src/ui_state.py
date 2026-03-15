"""
Thread-safe UI state snapshot for polybot_v2.

The main loop writes to this object via update().
The UI dashboard thread reads from it without blocking the main loop.
If the UI thread crashes, the main loop is unaffected.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Optional


class UIState:
    """
    A thread-safe snapshot of all information the UI needs to render.

    The main loop calls update(**kwargs) on every tick.
    The UI thread calls snapshot() to get a point-in-time copy.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "ts": 0.0,
            "mode": "paper",
            "btc_mid": 0.0,
            "window_start": 0.0,
            "window_end": 0.0,
            "elapsed_from_window_start": 0.0,
            "bankroll": 0.0,
            "paper_pnl": 0.0,
            "peak_bankroll": 0.0,
            "drawdown": 0.0,
            "best_bid_yes": 0.0,
            "best_ask_yes": 0.0,
            "best_bid_no": 0.0,
            "best_ask_no": 0.0,
            "implied_yes_prob": 0.5,
            "window_open_price": 0.0,
            "last_decision": None,
            "metrics": {},
            "open_trades": [],
            "win_count": 0,
            "loss_count": 0,
            "binance_age_ms": 0.0,
            "cooldown_remaining": 0,
            "consecutive_losses": 0,
        }
        # Rolling log ring-buffer for live log panel
        self._log_lines: deque[tuple[str, str, str]] = deque(maxlen=100)
        # last_shadow_quotes ring for shadow panel
        self._recent_shadow: deque[Any] = deque(maxlen=20)

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            self._data.update(kwargs)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def push_log(self, level: str, message: str) -> None:
        """Add a log line to the live log ring buffer."""
        ts_str = time.strftime("%H:%M:%S")
        with self._lock:
            self._log_lines.append((ts_str, level, message))

    def get_log_lines(self, n: int = 20) -> list[tuple[str, str, str]]:
        with self._lock:
            lines = list(self._log_lines)
        return lines[-n:]

    def push_shadow_quote(self, quote: Any) -> None:
        with self._lock:
            self._recent_shadow.append(quote)

    def get_recent_shadow(self) -> list[Any]:
        with self._lock:
            return list(self._recent_shadow)
