# binance.py — Binance WebSocket feed, shared BTC price state

import asyncio
import json
import logging
import time

import websockets

from config import BINANCE_WS, BINANCE_BACKOFF_BASE, BINANCE_BACKOFF_MAX

logger = logging.getLogger(__name__)


class BinanceFeed:
    """Maintains a live BTC/USDT price from Binance aggTrade stream."""

    def __init__(self):
        self.btc_price: float | None = None
        self._last_update: float = 0.0
        self._running: bool = False

    @property
    def price(self) -> float | None:
        return self.btc_price

    @property
    def last_update(self) -> float:
        return self._last_update

    def stop(self):
        self._running = False

    async def run(self):
        """Connect and stream forever, reconnecting with exponential backoff."""
        self._running = True
        delay = BINANCE_BACKOFF_BASE

        while self._running:
            try:
                logger.info("Connecting to Binance WS: %s", BINANCE_WS)
                async with websockets.connect(
                    BINANCE_WS,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    delay = BINANCE_BACKOFF_BASE  # reset on successful connect
                    logger.info("Binance WS connected.")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw)
                            self.btc_price = float(data["p"])
                            self._last_update = time.time()
                        except (KeyError, ValueError, json.JSONDecodeError) as exc:
                            logger.warning("Binance parse error: %s", exc)

            except asyncio.CancelledError:
                logger.info("Binance feed cancelled.")
                break
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "Binance WS disconnected (%s). Reconnecting in %ds…", exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, BINANCE_BACKOFF_MAX)
