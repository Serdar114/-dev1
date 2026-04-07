"""
rtds_client.py — Polymarket RTDS WebSocket client for canonical BTC/USD prices.

Design:
  Connects to wss://ws-live-data.polymarket.com
  Subscribes to TWO topics on the same connection:
    1. crypto_prices_chainlink  → symbol btc/usd   → CANONICAL truth source
    2. crypto_prices            → symbol btcusdt   → Binance auxiliary only

  RTDS subscription format:
    {
      "action": "subscribe",
      "subscriptions": [
        {"topic": "<topic>", "type": "*", "filters": "{\"symbol\":\"<sym>\"}"}
      ]
    }

  RTDS message format:
    {
      "topic": "<topic>",
      "type": "<type>",
      "timestamp": <ms>,
      "payload": {"symbol": "<sym>", "timestamp_ms": <ms>, "value": <float>}
    }

  Gap detection:
    Chainlink RTDS has documented ~8s intermittent gaps (GitHub issue #31).
    If last Chainlink update is older than chainlink_gap_threshold_secs, gap_flag=True.
    Gap at window boundary → outcome UNRESOLVED (handled by resolution_truth.py via
    freshness check).

  Staleness vs gap:
    staleness_secs: threshold for FreshnessState.FRESH/STALE — canonical validity
    The gap_threshold is handled in chainlink_state.py above this layer.
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

from loggingx.schemas import ChainlinkPrice, BinancePrice, FreshnessState
from truth.freshness import check_freshness

logger = logging.getLogger("polybot.rtds_client")

RTDS_URL = "wss://ws-live-data.polymarket.com"
CHAINLINK_TOPIC  = "crypto_prices_chainlink"
CHAINLINK_SYMBOL = "btc/usd"
BINANCE_TOPIC    = "crypto_prices"
BINANCE_SYMBOL   = "btcusdt"


class RTDSClient:
    """
    Polymarket RTDS WebSocket client.

    Job: Maintain latest Chainlink and Binance price snapshots from RTDS.
    Input: config (url, topics, symbols, staleness thresholds)
    Output: chainlink_latest(), binance_latest()
    Failure:
      - Disconnect: reconnect with delay, re-subscribe
      - Parse error: skip message, log
      - websockets not installed: log error, all reads return None
    """

    def __init__(
        self,
        ws_url: str = RTDS_URL,
        chainlink_topic: str = CHAINLINK_TOPIC,
        chainlink_symbol: str = CHAINLINK_SYMBOL,
        binance_topic: str = BINANCE_TOPIC,
        binance_symbol: str = BINANCE_SYMBOL,
        chainlink_staleness_secs: float = 30.0,
        binance_staleness_secs: float = 20.0,
        reconnect_delay_secs: float = 3.0,
        ping_interval_secs: float = 5.0,
    ):
        self._ws_url = ws_url
        self._chainlink_topic  = chainlink_topic
        self._chainlink_symbol = chainlink_symbol.lower()
        self._binance_topic    = binance_topic
        self._binance_symbol   = binance_symbol.lower()
        self._chainlink_staleness = chainlink_staleness_secs
        self._binance_staleness   = binance_staleness_secs
        self._reconnect_delay = reconnect_delay_secs
        self._ping_interval   = ping_interval_secs

        self._chainlink_raw_price: Optional[float] = None
        self._chainlink_raw_ts: Optional[float] = None    # unix seconds

        self._binance_raw_price: Optional[float] = None
        self._binance_raw_ts: Optional[float] = None

        self._running = False
        self._ws = None

        if not WEBSOCKETS_AVAILABLE:
            logger.error(
                "websockets not installed — RTDS feed UNAVAILABLE. "
                "Run: pip install websockets"
            )

    async def start(self) -> None:
        """Start the WebSocket loop. Run as asyncio task."""
        if not WEBSOCKETS_AVAILABLE:
            return
        self._running = True
        logger.info("RTDS client starting: %s", self._ws_url)
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "RTDS disconnected: %s — reconnecting in %.1fs",
                    exc, self._reconnect_delay,
                )
                self._ws = None
                await asyncio.sleep(self._reconnect_delay)

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _connect_and_listen(self) -> None:
        async with websockets.connect(
            self._ws_url,
            ping_interval=self._ping_interval,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info("RTDS connected")
            await self._subscribe(ws)
            async for raw_msg in ws:
                if not self._running:
                    break
                await self._handle_message(raw_msg)

    async def _subscribe(self, ws) -> None:
        """Send subscription for both Chainlink and Binance topics."""
        sub_msg = json.dumps({
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": self._chainlink_topic,
                    "type": "*",
                    "filters": json.dumps({"symbol": self._chainlink_symbol}),
                },
                {
                    "topic": self._binance_topic,
                    "type": "*",
                    "filters": json.dumps({"symbol": self._binance_symbol}),
                },
            ],
        })
        await ws.send(sub_msg)
        logger.info(
            "RTDS subscribed: chainlink=%s/%s binance=%s/%s",
            self._chainlink_topic, self._chainlink_symbol,
            self._binance_topic, self._binance_symbol,
        )

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            topic = msg.get("topic", "")
            payload = msg.get("payload", {})
            symbol = (payload.get("symbol") or "").lower()

            if topic == self._chainlink_topic and symbol == self._chainlink_symbol:
                self._handle_chainlink(payload)
            elif topic == self._binance_topic and symbol == self._binance_symbol:
                self._handle_binance(payload)
            else:
                logger.debug("RTDS unhandled: topic=%s symbol=%s", topic, symbol)

        except json.JSONDecodeError as exc:
            logger.warning("RTDS JSON error: %s | raw=%r", exc, raw[:120])
        except Exception as exc:
            logger.warning("RTDS message handler error: %s", exc)

    def _handle_chainlink(self, payload: dict) -> None:
        value = payload.get("value")
        ts_ms = payload.get("timestamp_ms") or payload.get("timestamp")
        if value is None:
            logger.debug("Chainlink payload missing value: %r", payload)
            return
        try:
            self._chainlink_raw_price = float(value)
            self._chainlink_raw_ts = float(ts_ms) / 1000.0 if ts_ms else time.time()
            logger.debug(
                "Chainlink BTC/USD=%.2f ts=%.3f",
                self._chainlink_raw_price,
                self._chainlink_raw_ts,
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Chainlink value parse error: %s payload=%r", exc, payload)

    def _handle_binance(self, payload: dict) -> None:
        value = payload.get("value")
        ts_ms = payload.get("timestamp_ms") or payload.get("timestamp")
        if value is None:
            logger.debug("Binance payload missing value: %r", payload)
            return
        try:
            self._binance_raw_price = float(value)
            self._binance_raw_ts = float(ts_ms) / 1000.0 if ts_ms else time.time()
            logger.debug(
                "Binance BTC/USDT=%.2f ts=%.3f",
                self._binance_raw_price,
                self._binance_raw_ts,
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Binance value parse error: %s payload=%r", exc, payload)

    def chainlink_latest(self) -> Optional[ChainlinkPrice]:
        """
        Return latest Chainlink price with freshness computed at read time.
        Returns None if no data received yet.
        """
        if self._chainlink_raw_price is None or self._chainlink_raw_ts is None:
            return None

        freshness = check_freshness(
            last_updated_ts=self._chainlink_raw_ts,
            max_age_secs=self._chainlink_staleness,
        )
        return ChainlinkPrice(
            price_usd=self._chainlink_raw_price,
            round_id=0,          # RTDS does not expose round IDs
            updated_at=self._chainlink_raw_ts,
            fetched_at=time.time(),
            freshness=freshness,
            source="rtds_chainlink_btcusd",
        )

    def binance_latest(self) -> Optional[BinancePrice]:
        """
        Return latest Binance auxiliary price with freshness computed at read time.
        Returns None if no data received yet.
        RTDS provides a single value (not bid/ask); bid == ask == value.
        """
        if self._binance_raw_price is None or self._binance_raw_ts is None:
            return None

        freshness = check_freshness(
            last_updated_ts=self._binance_raw_ts,
            max_age_secs=self._binance_staleness,
        )
        price = self._binance_raw_price
        return BinancePrice(
            bid=price,
            ask=price,
            fetched_at=self._binance_raw_ts,
            freshness=freshness,
            source="rtds_binance_btcusdt",
        )

    def chainlink_last_ts(self) -> Optional[float]:
        """Return unix timestamp of last Chainlink update, or None."""
        return self._chainlink_raw_ts

    def is_chainlink_fresh(self) -> bool:
        p = self.chainlink_latest()
        return p is not None and p.freshness == FreshnessState.FRESH

    # Legacy alias — runner.py calls .latest()
    def latest(self) -> Optional[ChainlinkPrice]:
        return self.chainlink_latest()
