"""
feeds/rtds_chainlink_client.py — Polymarket RTDS Chainlink price feed (PRIMARY).

Connects to wss://ws-live-data.polymarket.com.
This is the PRIMARY Chainlink source. Polygon RPC becomes audit/fallback only.

Subscribe payload sent on connect:
  {"action":"subscribe","subscriptions":[{"topic":"crypto_prices_chainlink","type":"*","filters":"{\\"symbol\\":\\"btc/usd\\"}"}]}

Message shape expected:
  event["topic"]             == "crypto_prices_chainlink"
  event["payload"]["symbol"] == "btc/usd"
  event["payload"]["value"]  == price (float or string)
  event["payload"]["timestamp"] == oracle unix timestamp

Keepalive: send string "PING" every heartbeat_interval_seconds (default 5s).

Resolution eligibility:
  source="rtds"         → oracle timestamp from payload; eligible for close capture
  source="rtds_msg_ts"  → no oracle ts in payload; NOT eligible for close capture
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from loggingx.schemas import ChainlinkErrorEvent, ChainlinkUpdateEvent

log = logging.getLogger(__name__)

# Oracle timestamp source labels
_SRC_RTDS_ORACLE = "rtds"          # RTDS message contained oracle updatedAt
_SRC_RTDS_MSG_TS = "rtds_msg_ts"   # RTDS message had no oracle ts; used message receipt time


class RtdsChainlinkClient:
    """
    PRIMARY Chainlink price feed via Polymarket RTDS WebSocket.

    Usage:
        client = RtdsChainlinkClient(config, buffer, event_logger)
        client.start()
        snap = client.snapshot()

    Writes to ChainlinkBuffer on every new oracle observation.
    Sets source="rtds" or "rtds_msg_ts" on each write.
    """

    # Official subscribe payload — hardcoded to prevent misconfiguration
    _SUBSCRIBE_MSG: str = json.dumps({
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": '{"symbol":"btc/usd"}',
            }
        ],
    })

    def __init__(self, config: dict, buffer=None, event_logger=None) -> None:
        rtds_cfg = config.get("rtds", {})
        self._ws_url: str = rtds_cfg.get(
            "ws_url",
            "wss://ws-live-data.polymarket.com",
        )
        self._heartbeat_interval: float = float(rtds_cfg.get("heartbeat_interval_seconds", 5))
        self._price_min: float = float(config.get("chainlink", {}).get("price_min", 10000.0))
        self._price_max: float = float(config.get("chainlink", {}).get("price_max", 500000.0))

        self._buffer = buffer
        self._logger = event_logger

        self._lock = threading.Lock()
        self._price: Optional[float] = None
        self._oracle_updated_at: Optional[float] = None
        self._fetched_at: Optional[float] = None
        self._source: str = "none"
        self._last_error: Optional[str] = None

        self._stop_event = threading.Event()
        self._ws = None
        self._last_heartbeat: float = 0.0

    def _log(self, event) -> None:
        if self._logger:
            self._logger.log(event)

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run_loop, daemon=True, name="rtds-chainlink")
        t.start()
        return t

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def snapshot(self):
        """Return a snapshot compatible with ChainlinkClient.snapshot()."""
        from feeds.chainlink_client import ChainlinkSnapshot
        with self._lock:
            return ChainlinkSnapshot(
                price=self._price,
                oracle_updated_at=self._oracle_updated_at,
                fetched_at=self._fetched_at,
                round_id=None,
            )

    def is_healthy(self, max_age: float) -> bool:
        """True if we have a fresh reading."""
        with self._lock:
            if self._oracle_updated_at is None:
                return False
            return (time.time() - self._oracle_updated_at) <= max_age

    def inject_state(self, state) -> None:
        """Write current reading into SystemState under its lock."""
        with self._lock:
            price = self._price
            oracle_ts = self._oracle_updated_at
            fetched = self._fetched_at
            src = self._source
        with state._lock:
            state.chainlink.price = price
            state.chainlink.oracle_updated_at = oracle_ts
            state.chainlink.fetched_at = fetched
            state.chainlink.source = src

    # -----------------------------------------------------------------------
    # WebSocket internals
    # -----------------------------------------------------------------------

    def _on_open(self, ws) -> None:
        log.info("RTDS connected: url=%s  subscribe=%s", self._ws_url, self._SUBSCRIBE_MSG)
        ws.send(self._SUBSCRIBE_MSG)
        self._last_heartbeat = time.time()

    def _on_message(self, ws, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        events = data if isinstance(data, list) else [data]
        for event in events:
            self._handle_event(event)

        # Application-level heartbeat — send literal "PING" string
        now = time.time()
        if now - self._last_heartbeat >= self._heartbeat_interval:
            try:
                ws.send("PING")
                self._last_heartbeat = now
            except Exception:
                pass

    def _handle_event(self, event: dict) -> None:
        """Parse an RTDS crypto_prices_chainlink event.

        Official shape:
          event["topic"]             == "crypto_prices_chainlink"
          event["payload"]["symbol"] == "btc/usd"
          event["payload"]["value"]  == price
          event["payload"]["timestamp"] == oracle unix timestamp (seconds or ms)
        """
        # Only handle the Chainlink crypto price topic
        if event.get("topic") != "crypto_prices_chainlink":
            return

        payload = event.get("payload")
        if not isinstance(payload, dict):
            return

        # Symbol filter: must be btc/usd
        symbol = str(payload.get("symbol", "")).lower().strip()
        if symbol != "btc/usd":
            return

        raw_price = payload.get("value")
        if raw_price is None:
            return

        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            return

        if not (self._price_min <= price <= self._price_max):
            log.warning("RTDS price %f outside sanity range", price)
            return

        now = time.time()

        raw_ts = payload.get("timestamp")
        if raw_ts is not None:
            try:
                oracle_ts = float(raw_ts)
                if oracle_ts > 1_600_000_000_000:  # milliseconds → seconds
                    oracle_ts /= 1000.0
                source = _SRC_RTDS_ORACLE
            except (TypeError, ValueError):
                oracle_ts = now
                source = _SRC_RTDS_MSG_TS
        else:
            oracle_ts = now
            source = _SRC_RTDS_MSG_TS

        with self._lock:
            self._price = price
            self._oracle_updated_at = oracle_ts
            self._fetched_at = now
            self._source = source
            self._last_error = None

        # Record in buffer (only "rtds" source observations are buffer-worthy for resolution)
        if self._buffer is not None and source == _SRC_RTDS_ORACLE:
            self._buffer.record(oracle_ts, price, now, source)

        self._log(ChainlinkUpdateEvent(
            price=price,
            oracle_updated_at=oracle_ts,
            age_seconds=now - oracle_ts,
            round_id=0,
        ))

    def _on_error(self, ws, error) -> None:
        err = str(error)
        log.warning("RTDS error: %s", err)
        with self._lock:
            self._last_error = err
        self._log(ChainlinkErrorEvent(error=f"rtds:{err}"))

    def _on_close(self, ws, code, msg) -> None:
        log.info("RTDS closed: code=%s msg=%s", code, msg)

    def _run_loop(self) -> None:
        import websocket

        while not self._stop_event.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    self._ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                log.error("RTDS run_forever exception: %s", exc)
                with self._lock:
                    self._last_error = str(exc)
            if not self._stop_event.is_set():
                log.info("RTDS reconnecting in 5s...")
                self._stop_event.wait(timeout=5)
