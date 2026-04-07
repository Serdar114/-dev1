"""
binance_aux.py — Binance BTCUSDT auxiliary price feed.

Design:
  Binance is AUXILIARY. It is NOT canonical settlement truth.
  It is used for signal observation only — never for resolution determination.
  If Binance is stale/missing, it is logged but does NOT block trading decisions alone.
  The no_trade_rules module determines whether Binance staleness causes no-trade.

  Feed: Binance WebSocket bookTicker stream
  URL: wss://stream.binance.com:9443/ws/btcusdt@bookTicker
  Message: {"u": update_id, "s": "BTCUSDT", "b": bid, "B": bid_qty, "a": ask, "A": ask_qty}

  Reconnection: automatic on disconnect, with delay.
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Optional

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

from loggingx.schemas import BinancePrice, FreshnessState
from truth.freshness import check_freshness

logger = logging.getLogger("polybot.binance_aux")

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"


class BinanceAuxClient:
    """
    Auxiliary Binance BTCUSDT price feed via WebSocket.

    Job: Maintain latest bid/ask from Binance for signal observation.
    Input: WebSocket bookTicker stream
    Output: latest BinancePrice snapshot (NEVER used for settlement)
    Failure:
      - Disconnect: reconnect with delay, log gap duration
      - Parse error: skip message, log error
      - websockets not installed: logs warning, returns None always
    """

    def __init__(
        self,
        ws_url: str = BINANCE_WS_URL,
        staleness_threshold_secs: float = 20.0,
        reconnect_delay_secs: float = 5.0,
    ):
        self._ws_url = ws_url
        self._staleness_threshold = staleness_threshold_secs
        self._reconnect_delay = reconnect_delay_secs

        self._latest: Optional[BinancePrice] = None
        self._running = False
        self._messages_received = 0
        self._last_disconnect_at: Optional[float] = None

        if not WEBSOCKETS_AVAILABLE:
            logger.warning(
                "websockets not installed — Binance auxiliary feed UNAVAILABLE. "
                "Binance data will be MISSING (non-canonical, OK for measurement mode)."
            )

    async def start(self) -> None:
        """Start WebSocket listener with auto-reconnect. Run as asyncio task."""
        if not WEBSOCKETS_AVAILABLE:
            return

        self._running = True
        logger.info("Binance auxiliary client starting: %s", self._ws_url)

        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._last_disconnect_at = time.time()
                logger.warning("Binance WS disconnected: %s — reconnecting in %.1fs", exc, self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)

    async def stop(self) -> None:
        self._running = False

    async def _connect_and_listen(self) -> None:
        async with websockets.connect(
            self._ws_url,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            if self._last_disconnect_at:
                gap = time.time() - self._last_disconnect_at
                logger.info("Binance WS reconnected (gap=%.1fs)", gap)
                self._last_disconnect_at = None
            else:
                logger.info("Binance WS connected")

            async for raw_msg in ws:
                if not self._running:
                    break
                self._handle_message(raw_msg)

    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            bid = float(msg["b"])
            ask = float(msg["a"])
            now = time.time()

            self._latest = BinancePrice(
                bid=bid,
                ask=ask,
                fetched_at=now,
                freshness=FreshnessState.FRESH,  # just received, always fresh at creation
            )
            self._messages_received += 1

        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Binance message parse error: %s | raw=%r", exc, raw[:100])

    def latest(self) -> Optional[BinancePrice]:
        """
        Return latest BinancePrice with current freshness recomputed.
        Returns None if never received.
        """
        if self._latest is None:
            return None

        freshness = check_freshness(
            last_updated_ts=self._latest.fetched_at,
            max_age_secs=self._staleness_threshold,
        )

        return BinancePrice(
            bid=self._latest.bid,
            ask=self._latest.ask,
            fetched_at=self._latest.fetched_at,
            freshness=freshness,
        )

    def is_fresh(self) -> bool:
        p = self.latest()
        return p is not None and p.freshness == FreshnessState.FRESH

    def mid_or_none(self) -> Optional[float]:
        p = self.latest()
        if p and p.freshness == FreshnessState.FRESH:
            return p.mid()
        return None

    def messages_received(self) -> int:
        return self._messages_received
