"""
Binance BTCUSDT WebSocket mid-price feed.

Maintains a rolling 60-second mid-price buffer for realized vol calculation.
Reconnects automatically on disconnect.
Thread-safe reads via threading.Lock.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from typing import Optional

import websocket  # websocket-client

log = logging.getLogger(__name__)

_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"
_RECONNECT_DELAY_BASE = 2.0
_MAX_RECONNECT_DELAY = 30.0


class BinanceFeed:
    """
    Subscribes to Binance BTCUSDT best bid/ask stream.
    Computes mid-price and tracks a 60-second price history for realized vol.
    """

    def __init__(self, stale_threshold_ms: int = 3000) -> None:
        self._stale_ms = stale_threshold_ms
        self._lock = threading.Lock()
        self._mid: Optional[float] = None
        self._last_ts: float = 0.0
        # (timestamp, mid) pairs for last 60s
        self._history: deque[tuple[float, float]] = deque()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._reconnect_delay = _RECONNECT_DELAY_BASE
        self._window_open: Optional[float] = None  # price at start of current 5m window

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="BinanceFeed")
        self._thread.start()
        log.info("BinanceFeed started, connecting to Binance WS…")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            self._ws.close()
        log.info("BinanceFeed stopped")

    def get_snapshot(self) -> tuple[Optional[float], float, float]:
        """
        Returns (mid_price, timestamp, realized_vol_60s).
        mid_price is None if no data received yet.
        """
        with self._lock:
            return self._mid, self._last_ts, self._compute_vol_locked()

    def is_stale(self) -> bool:
        with self._lock:
            if self._mid is None:
                return True
            age_ms = (time.time() - self._last_ts) * 1000
            return age_ms > self._stale_ms

    def data_age_ms(self) -> float:
        with self._lock:
            if self._last_ts == 0:
                return float("inf")
            return (time.time() - self._last_ts) * 1000

    def set_window_open(self, price: float) -> None:
        with self._lock:
            self._window_open = price

    def get_window_open(self) -> Optional[float]:
        with self._lock:
            return self._window_open

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._connect()
            except Exception as exc:
                log.warning("BinanceFeed connection error: %s", exc)
            if not self._running:
                break
            log.info("BinanceFeed reconnecting in %.1fs…", self._reconnect_delay)
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, _MAX_RECONNECT_DELAY)

    def _connect(self) -> None:
        ws = websocket.WebSocketApp(
            _WS_URL,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        self._ws = ws
        ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        self._reconnect_delay = _RECONNECT_DELAY_BASE
        log.info("BinanceFeed WebSocket connected")

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            msg = json.loads(raw)
            bid = float(msg["b"])
            ask = float(msg["a"])
            mid = (bid + ask) / 2.0
            now = time.time()
            with self._lock:
                self._mid = mid
                self._last_ts = now
                self._history.append((now, mid))
                self._prune_history_locked(now)
        except (KeyError, ValueError, TypeError) as exc:
            log.debug("BinanceFeed parse error: %s | raw=%s", exc, raw[:80])

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        log.warning("BinanceFeed WS error: %s", error)

    def _on_close(self, ws: websocket.WebSocketApp, code: int, msg: str) -> None:
        log.info("BinanceFeed WS closed (code=%s msg=%s)", code, msg)

    def _prune_history_locked(self, now: float) -> None:
        cutoff = now - 60.0
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    def _compute_vol_locked(self) -> float:
        """
        Compute realized vol from 60s mid-price returns.
        Returns sigma_floor-equivalent 0 if insufficient data; caller applies floor.
        """
        if len(self._history) < 3:
            return 0.0
        prices = [p for _, p in self._history]
        returns = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
            if prices[i - 1] > 0
        ]
        if len(returns) < 2:
            return 0.0
        n = len(returns)
        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
        return math.sqrt(variance)  # per-tick std; caller normalises vs tau
