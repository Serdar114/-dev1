"""
Thread-safe UI state snapshot for polybot_v2.

The main loop writes to this object via update().
The UI dashboard thread reads from it without blocking the main loop.
If the UI thread crashes, the main loop is unaffected.

UILogHandler: a standard logging.Handler that routes log records into the
live-log ring buffer so the dashboard log panel shows real bot output.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Optional


class UILogHandler(logging.Handler):
    """
    Routes Python logging records into UIState's live-log ring buffer.

    Wire up once in main.py after UIState is created:
        handler = UILogHandler(ui_state)
        handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(handler)
    """

    def __init__(self, ui_state: "UIState") -> None:
        super().__init__()
        self._state = ui_state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._state.push_log(record.levelname, msg)
        except Exception:
            self.handleError(record)


class UIState:
    """
    A thread-safe snapshot of all information the UI needs to render.

    The main loop calls update(**kwargs) on every tick.
    The UI thread calls snapshot() to get a point-in-time copy.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            # Timing
            "ts": 0.0,
            # System
            "mode": "paper",
            "status_msg": "STARTING UP…",
            # BTC feed
            "btc_mid": 0.0,
            "binance_age_ms": 0.0,
            "realized_vol_60s": 0.0,
            # BTC delta (dual units)
            "delta_raw_fraction": 0.0,   # (btc - open) / open
            "delta_pct_display": 0.0,    # delta_raw_fraction * 100
            # Window
            "window_start": 0.0,
            "window_end": 0.0,
            "elapsed_from_window_start": 0.0,
            "window_open_price": 0.0,
            "entry_start_sec": 30,
            "entry_end_sec": 240,
            # Market
            "market_slug": "",
            "best_bid_yes": 0.0,
            "best_ask_yes": 0.0,
            "best_bid_no": 0.0,
            "best_ask_no": 0.0,
            "implied_yes_prob": 0.5,
            # Market microstructure diagnostics
            "yes_mid": 0.5,
            "no_mid": 0.5,
            "spread_yes": 0.0,
            "spread_no": 0.0,
            "midpoint_sum": 1.0,
            "complement_skew": 0.0,
            "sanity_reject": None,         # None | reject-reason string
            # Bankroll / PnL
            "bankroll": 0.0,
            "paper_pnl": 0.0,
            "peak_bankroll": 0.0,
            "drawdown": 0.0,
            # Trades
            "open_trades": [],
            "win_count": 0,
            "loss_count": 0,
            # Risk
            "cooldown_remaining": 0,
            "consecutive_losses": 0,
            # Latest decision
            "last_decision": None,
            # Aggregated metrics
            "metrics": {},
        }
        # Rolling log ring-buffer for live log panel
        self._log_lines: deque[tuple[str, str, str]] = deque(maxlen=200)
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
