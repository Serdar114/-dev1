"""
feeds/binance_client.py — Binance BTC/USDT bookTicker WebSocket feed.

AUXILIARY ONLY. Never canonical settlement truth.
Provides real-time best bid/ask for basis monitoring.
Reconnects automatically on disconnect.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

import requests

from loggingx.schemas import BinanceConnectedEvent, BinanceDisconnectedEvent

log = logging.getLogger(__name__)


class BinanceClient:
    """
    Connects to Binance bookTicker stream for BTCUSDT.
    Falls back to REST poll if WebSocket fails to connect initially.
    """

    def __init__(self, config: dict, event_logger=None) -> None:
        bn = config["binance"]
        self._ws_url: str = bn["ws_url"]
        self._rest_url: str = bn["rest_fallback_url"]
        self._max_age: float = float(bn.get("max_age_seconds", 30))
        self._logger = event_logger

        self._lock = threading.Lock()
        self._bid: Optional[float] = None
        self._ask: Optional[float] = None
        self._updated_at: Optional[float] = None
        self._last_error: Optional[str] = None

        self._stop_event = threading.Event()
        self._ws = None

    def _log(self, event) -> None:
        if self._logger:
            self._logger.log(event)

    def _on_message(self, ws, message: str) -> None:
        try:
            data = json.loads(message)
            bid = float(data["b"])
            ask = float(data["a"])
            now = time.time()
            with self._lock:
                self._bid = bid
                self._ask = ask
                self._updated_at = now
                self._last_error = None
        except Exception as exc:
            log.debug("Binance message parse error: %s", exc)

    def _on_open(self, ws) -> None:
        log.info("Binance WebSocket connected")
        self._log(BinanceConnectedEvent())

    def _on_error(self, ws, error) -> None:
        err = str(error)
        log.warning("Binance WebSocket error: %s", err)
        with self._lock:
            self._last_error = err

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        err = f"code={close_status_code} msg={close_msg}"
        log.info("Binance WebSocket closed: %s", err)
        self._log(BinanceDisconnectedEvent(error=err))

    def run(self) -> None:
        """Background loop: connect WebSocket, reconnect on disconnect."""
        import websocket

        while not self._stop_event.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    self._ws_url,
                    on_message=self._on_message,
                    on_open=self._on_open,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                log.error("Binance WS run_forever exception: %s", exc)
                with self._lock:
                    self._last_error = str(exc)
            if not self._stop_event.is_set():
                log.info("Binance WS reconnecting in 5s...")
                self._stop_event.wait(timeout=5)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="binance-ws")
        t.start()
        return t

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def rest_fetch_once(self) -> bool:
        """Single REST poll. Used for initial bootstrap or if WS unavailable."""
        try:
            resp = requests.get(self._rest_url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            bid = float(data["bidPrice"])
            ask = float(data["askPrice"])
            now = time.time()
            with self._lock:
                self._bid = bid
                self._ask = ask
                self._updated_at = now
                self._last_error = None
            return True
        except Exception as exc:
            log.warning("Binance REST fetch failed: %s", exc)
            with self._lock:
                self._last_error = str(exc)
            return False

    def inject_state(self, state) -> None:
        """Write current reading into SystemState under its lock."""
        with self._lock:
            bid, ask, updated = self._bid, self._ask, self._updated_at
        with state._lock:
            state.binance.bid = bid
            state.binance.ask = ask
            state.binance.updated_at = updated
