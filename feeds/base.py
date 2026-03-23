"""
feeds/base.py — Shared types and base class for RTDS feed adapters.

Polymarket RTDS (Real-Time Data Service) delivers price data over a
dedicated WebSocket endpoint that is SEPARATE from the CLOB market/user
WebSocket endpoints:

  RTDS endpoint:      wss://ws-live-data.polymarket.com
  CLOB market events: wss://ws-subscriptions-clob.polymarket.com/ws/market
  CLOB user events:   wss://ws-subscriptions-clob.polymarket.com/ws/user

Do NOT connect RTDS topics (crypto_prices, crypto_prices_chainlink) to the
CLOB ws-subscriptions host — that returns HTTP 404.

--- Subscription protocol ---

After connecting to wss://ws-live-data.polymarket.com, send:

    {
      "action": "subscribe",
      "subscriptions": [
        {
          "topic": "<topic>",
          "type": "update",
          "filters": "<optional JSON-encoded filter string>"
        }
      ]
    }

Supported topics: "crypto_prices", "crypto_prices_chainlink"

Filter example (fast feed, BTCUSDT):
    "filters": '{"symbol":"BTCUSDT"}'

--- Incoming message format ---

    {
      "topic": "crypto_prices",
      "type":  "update",
      "timestamp": <unix ms>,
      "payload": {
        "symbol":    "BTCUSDT",
        "value":     <float price>,
        "timestamp": <unix ms>
      }
    }

--- Heartbeat ---

Polymarket RTDS requires WebSocket PING frames every ~5 seconds to keep
the connection alive.  This adapter sends ws.ping() every HEARTBEAT_INTERVAL
(5 s).  Do not reuse CLOB market/user heartbeat assumptions (those use
application-level "{"type":"heartbeat"}" messages on a different socket).

--- Reconnect ---

Exponential back-off: [2, 4, 8, 16, 32] seconds, up to 5 attempts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

logger = logging.getLogger(__name__)


class FeedStatus(Enum):
    """Lifecycle state of a feed adapter."""
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    STALE = auto()          # Connected but no tick within stale_threshold_seconds
    ERROR = auto()


@dataclass
class FeedSnapshot:
    """
    Immutable snapshot of a single feed tick.

    Attributes
    ----------
    feed_name       Identifier string (e.g. 'fast' or 'chainlink').
    symbol          Asset symbol as used by this bot (e.g. 'BTC-USD').
    price           Mid-price or last trade price reported by the feed.
    timestamp       Unix epoch seconds of the tick as received from the feed.
    received_at     Local unix epoch seconds when the tick was processed.
    gap_seconds     Seconds since the previous tick on this feed (None for first).
    is_stale        True if gap_seconds > configured stale_threshold_seconds.
    raw             Original raw payload dict for audit / debugging.
    """
    feed_name: str
    symbol: str
    price: float
    timestamp: float
    received_at: float
    gap_seconds: Optional[float]
    is_stale: bool
    raw: dict = field(default_factory=dict)


class BaseFeedAdapter:
    """
    Async base class for Polymarket RTDS feed adapters.

    Connects to wss://ws-live-data.polymarket.com (RTDS), subscribes to
    the topic returned by _channel_name(), and streams FeedSnapshot objects.

    Subclasses must implement:
        _channel_name()       → str      RTDS topic name
        _rtds_symbol()        → str      Symbol in RTDS notation (e.g. "BTCUSDT")
        _parse_message()      → Optional[FeedSnapshot]

    Subclasses may override:
        _subscribe_payload()  → dict     Full subscribe action sent after connect
    """

    RECONNECT_DELAYS = [2, 4, 8, 16, 32]   # seconds, exponential back-off
    # Polymarket RTDS requires frequent pings to keep the connection alive.
    # Recommended interval: ~5 seconds.
    HEARTBEAT_INTERVAL = 5                   # seconds
    # Diagnostics
    _RAW_LOG_LIMIT = 5        # log first N raw messages per connection
    _NO_MSG_WARN_SECS = 30    # warn if no messages arrive within this window
    _COUNTER_LOG_INTERVAL = 60  # log message counters every N seconds

    def __init__(
        self,
        rtds_host: str,
        symbol: str,
        stale_threshold_seconds: float,
    ) -> None:
        self._rtds_host = rtds_host
        self._symbol = symbol
        self._stale_threshold = stale_threshold_seconds

        self._status: FeedStatus = FeedStatus.DISCONNECTED
        self._latest: Optional[FeedSnapshot] = None
        self._prev_timestamp: Optional[float] = None

        self._lock = asyncio.Lock()
        self._running = False
        self._ws = None

        # --- Diagnostics counters (cumulative across reconnects) ---
        self._total_received: int = 0
        self._total_parsed_ok: int = 0
        self._total_parse_failed: int = 0
        self._total_discarded: int = 0
        # Per-connection raw-log counter (reset on each connect)
        self._raw_log_count: int = 0
        # Set to True once first valid price is logged
        self._first_valid_logged: bool = False
        # Last time counters were logged (wall clock)
        self._last_counter_log: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def status(self) -> FeedStatus:
        return self._status

    @property
    def latest(self) -> Optional[FeedSnapshot]:
        """Return the most recent snapshot (thread-safe read)."""
        return self._latest

    def gap_seconds_now(self) -> Optional[float]:
        """Seconds since the last tick, computed against wall clock."""
        snap = self._latest
        if snap is None:
            return None
        return time.time() - snap.received_at

    async def start(self) -> None:
        """Begin connecting and consuming messages in the background."""
        self._running = True
        asyncio.ensure_future(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        attempt = 0
        while self._running:
            try:
                self._status = FeedStatus.CONNECTING
                logger.info(
                    "[%s] Connecting to RTDS %s topic=%s",
                    self._feed_label(), self._rtds_host, self._channel_name()
                )
                await self._connect_and_consume()
                attempt = 0   # reset on clean disconnect
            except Exception as exc:
                delay = self.RECONNECT_DELAYS[min(attempt, len(self.RECONNECT_DELAYS) - 1)]
                logger.warning(
                    "[%s] Feed error (%s); reconnecting in %ds (attempt %d)",
                    self._feed_label(), exc, delay, attempt + 1
                )
                self._status = FeedStatus.ERROR
                await asyncio.sleep(delay)
                attempt += 1

    async def _connect_and_consume(self) -> None:
        """
        Connect to wss://ws-live-data.polymarket.com and consume RTDS messages.

        Protocol:
          1. Open WebSocket to self._rtds_host (must be RTDS host, not CLOB host).
          2. Send RTDS subscribe action (see _subscribe_payload()).
          3. Receive messages; each has {"topic":..., "type":..., "payload":{...}}.
          4. Send WebSocket PING frames every HEARTBEAT_INTERVAL seconds to keep
             the connection alive (Polymarket RTDS requires ~5 s pings).
        """
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.error("[%s] websockets package not installed", self._feed_label())
            raise

        # Reset per-connection raw-log counter so first 5 messages of each
        # reconnect are always captured.
        self._raw_log_count = 0

        async with websockets.connect(self._rtds_host) as ws:
            self._ws = ws
            self._status = FeedStatus.CONNECTED
            logger.info("[%s] Connected to RTDS %s", self._feed_label(), self._rtds_host)

            # Send RTDS subscription — uses "subscriptions" list wrapper, not
            # the CLOB {"action":"subscribe","channel":...} format.
            sub_payload = self._subscribe_payload()
            await ws.send(json.dumps(sub_payload))
            logger.info(
                "[%s] Sent RTDS subscribe: %s", self._feed_label(), sub_payload
            )

            # Track messages received on this specific connection (for no-msg warning).
            msgs_this_conn: list[int] = [0]

            heartbeat_task = asyncio.ensure_future(self._heartbeat(ws))
            no_msg_task = asyncio.ensure_future(
                self._no_message_warning(msgs_this_conn)
            )
            try:
                async for raw_msg in ws:
                    if not self._running:
                        break

                    msgs_this_conn[0] += 1
                    self._total_received += 1

                    # Log first _RAW_LOG_LIMIT raw messages verbatim for
                    # wire-level diagnostics.
                    if self._raw_log_count < self._RAW_LOG_LIMIT:
                        self._raw_log_count += 1
                        raw_preview = (
                            raw_msg[:500]
                            if isinstance(raw_msg, str)
                            else str(raw_msg)[:500]
                        )
                        logger.info(
                            "[%s] RAW msg #%d: %s",
                            self._feed_label(), self._raw_log_count, raw_preview
                        )

                    try:
                        payload = json.loads(raw_msg)
                        snap = self._parse_message(payload)
                        if snap is not None:
                            self._total_parsed_ok += 1
                            if not self._first_valid_logged:
                                self._first_valid_logged = True
                                logger.info(
                                    "[%s] First valid price: %.6f at ts=%.3f",
                                    self._feed_label(), snap.price, snap.timestamp
                                )
                            async with self._lock:
                                self._latest = snap
                                self._prev_timestamp = snap.timestamp
                            self._log_tick(snap)
                        else:
                            self._total_discarded += 1
                    except Exception as parse_exc:
                        self._total_parse_failed += 1
                        raw_preview = (
                            raw_msg[:200]
                            if isinstance(raw_msg, str)
                            else str(raw_msg)[:200]
                        )
                        logger.warning(
                            "[%s] Parse failed for msg: %s — error: %s: %s",
                            self._feed_label(), raw_preview,
                            type(parse_exc).__name__, parse_exc
                        )

                    # Log counters every _COUNTER_LOG_INTERVAL seconds.
                    now = time.time()
                    if now - self._last_counter_log >= self._COUNTER_LOG_INTERVAL:
                        self._last_counter_log = now
                        logger.info(
                            "[%s] counters: received=%d parsed_ok=%d "
                            "parse_failed=%d discarded=%d",
                            self._feed_label(),
                            self._total_received, self._total_parsed_ok,
                            self._total_parse_failed, self._total_discarded,
                        )
            finally:
                heartbeat_task.cancel()
                no_msg_task.cancel()
                self._ws = None

    async def _heartbeat(self, ws) -> None:
        """
        Send WebSocket PING frames every HEARTBEAT_INTERVAL seconds.

        Polymarket RTDS keepalive is done via native WebSocket ping/pong,
        NOT via application-level heartbeat messages (those are only for
        the CLOB market/user sockets).  A ping every 5 seconds is sufficient.
        """
        while True:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            try:
                await ws.ping()
            except Exception:
                break

    async def _no_message_warning(self, msgs_this_conn: list) -> None:
        """
        Warn if no messages arrive within _NO_MSG_WARN_SECS after subscribe.
        Uses a mutable list[int] as a shared counter to avoid closure issues.
        """
        await asyncio.sleep(self._NO_MSG_WARN_SECS)
        if msgs_this_conn[0] == 0:
            logger.warning(
                "[%s] WARNING: No messages received %ds after subscribe — "
                "check RTDS host (%s) and subscription payload",
                self._feed_label(), self._NO_MSG_WARN_SECS, self._rtds_host
            )

    def _subscribe_payload(self) -> dict:
        """
        Build the RTDS subscribe action to send immediately after connection.

        Default: subscribe to _channel_name() with type "update".
        Subclasses override to add symbol filters.

        RTDS format (different from CLOB channel subscription):
          {
            "action": "subscribe",
            "subscriptions": [
              {"topic": "<topic>", "type": "update"}
            ]
          }
        """
        return {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": self._channel_name(),
                    "type": "update",
                }
            ],
        }

    def _build_snapshot(self, price: float, ts: float, raw: dict) -> FeedSnapshot:
        now = time.time()
        gap = (ts - self._prev_timestamp) if self._prev_timestamp is not None else None
        is_stale = (gap is not None and gap > self._stale_threshold)
        return FeedSnapshot(
            feed_name=self._feed_label(),
            symbol=self._symbol,
            price=price,
            timestamp=ts,
            received_at=now,
            gap_seconds=gap,
            is_stale=is_stale,
            raw=raw,
        )

    def _log_tick(self, snap: FeedSnapshot) -> None:
        level = logging.WARNING if snap.is_stale else logging.DEBUG
        logger.log(
            level,
            "[%s] tick price=%.6f ts=%.3f gap=%.2fs stale=%s",
            snap.feed_name,
            snap.price,
            snap.timestamp,
            snap.gap_seconds if snap.gap_seconds is not None else 0.0,
            snap.is_stale,
        )

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    def _channel_name(self) -> str:
        """RTDS topic name (e.g. 'crypto_prices')."""
        raise NotImplementedError

    def _feed_label(self) -> str:
        raise NotImplementedError

    def _parse_message(self, payload: dict) -> Optional[FeedSnapshot]:
        """
        Parse an incoming RTDS message envelope.

        Incoming structure:
          {
            "topic":     "<topic>",
            "type":      "update",
            "timestamp": <unix ms>,
            "payload": {
              "symbol":    "<RTDS symbol>",
              "value":     <float price>,
              "timestamp": <unix ms>
            }
          }
        """
        raise NotImplementedError
