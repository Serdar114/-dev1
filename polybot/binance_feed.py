"""
binance_feed.py — Binance WebSocket BTC/USDT bookTicker feed.

Connects to wss://stream.binance.com:9443/ws/btcusdt@bookTicker
and maintains a live mid_price from best bid/ask.
Reconnects automatically on failure (up to max attempts).
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional

import websockets

logger = logging.getLogger(__name__)


class BinanceFeed:
    def __init__(self, config: dict):
        self._ws_url: str = config["binance"]["ws_url"]
        self._stale_threshold: float = config["binance"]["stale_threshold_sec"]
        self._max_reconnects: int = config["binance"]["reconnect_attempts"]
        self._reconnect_delay: float = config["binance"]["reconnect_delay_sec"]

        self.mid_price: Optional[float] = None
        self.last_update_ts: float = 0.0
        self._callbacks: list[Callable] = []
        self._running: bool = False
        self._ws_task: Optional[asyncio.Task] = None

    @property
    def is_stale(self) -> bool:
        if self.mid_price is None:
            return True
        return (time.time() - self.last_update_ts) > self._stale_threshold

    def add_callback(self, cb: Callable) -> None:
        """Register a callback invoked on every price update."""
        self._callbacks.append(cb)

    async def connect(self) -> None:
        """Start the WebSocket listener task in the background."""
        self._running = True
        self._ws_task = asyncio.create_task(self._listen_loop())
        logger.info("BinanceFeed background task started.")

    async def disconnect(self) -> None:
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        logger.info("BinanceFeed disconnected.")

    async def _listen_loop(self) -> None:
        attempt = 0
        while self._running:
            try:
                logger.info(
                    "Connecting to Binance WebSocket (attempt %d)…", attempt + 1
                )
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    attempt = 0  # reset on successful connect
                    logger.info("Binance WebSocket connected: %s", self._ws_url)
                    async for raw in ws:
                        if not self._running:
                            break
                        self._handle_message(raw)
            except asyncio.CancelledError:
                logger.info("BinanceFeed task cancelled.")
                break
            except Exception as exc:
                attempt += 1
                if attempt > self._max_reconnects:
                    logger.error(
                        "Binance WebSocket max reconnect attempts (%d) exceeded. "
                        "Feed halted.",
                        self._max_reconnects,
                    )
                    break
                delay = self._reconnect_delay * attempt
                logger.warning(
                    "Binance WebSocket error (%s). Reconnecting in %.1fs "
                    "(attempt %d/%d)…",
                    exc,
                    delay,
                    attempt,
                    self._max_reconnects,
                )
                await asyncio.sleep(delay)

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
            # bookTicker payload: {"b": best_bid, "a": best_ask, ...}
            best_bid = float(data["b"])
            best_ask = float(data["a"])
            self.mid_price = (best_bid + best_ask) / 2.0
            self.last_update_ts = time.time()
            for cb in self._callbacks:
                try:
                    cb(self.mid_price, self.last_update_ts)
                except Exception as cb_exc:
                    logger.warning("Callback error: %s", cb_exc)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("BinanceFeed message parse error: %s | raw=%s", exc, raw[:120])
