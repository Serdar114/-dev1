"""
feeds/rtds_chainlink_client.py — Polymarket RTDS Chainlink price feed (PRIMARY).

Connects to the Polymarket Real-Time Data Service WebSocket.
This is the PRIMARY Chainlink source. Polygon RPC becomes audit/fallback only.

IMPORTANT — VERIFY BEFORE RUNNING:
  The RTDS endpoint and subscription message format must be confirmed against
  your working sample. Edit config.yaml keys:
    rtds.ws_url
    rtds.subscription_msg
    rtds.price_field
    rtds.oracle_ts_field
  before expecting this to produce valid readings.

Message timestamp vs oracle timestamp:
  - oracle_updated_at: the timestamp from the Chainlink contract (canonical)
  - If the RTDS message does not include the oracle's updatedAt, we note the
    source as "rtds_msg_ts" and track this explicitly in state.
  - Resolution NEVER uses a "rtds_msg_ts" observation as canonical close truth.

Keepalive:
  - Sends a JSON ping every rtds.heartbeat_interval_seconds (default 20s)
  - websocket-client handles TCP-level ping/pong automatically
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

    def __init__(self, config: dict, buffer=None, event_logger=None) -> None:
        rtds_cfg = config.get("rtds", {})
        self._ws_url: str = rtds_cfg.get(
            "ws_url",
            "wss://ws-live-data.polymarket.com",
        )
        # Subscription message sent on connect. Verify against working sample.
        self._sub_msg: str = rtds_cfg.get(
            "subscription_msg",
            json.dumps({"type": "Asset", "assets": ["BTC-USD"]}),
        )
        self._price_field: str = rtds_cfg.get("price_field", "price")
        self._oracle_ts_field: str = rtds_cfg.get("oracle_ts_field", "oracle_updated_at")
        self._msg_ts_field: str = rtds_cfg.get("msg_ts_field", "timestamp")
        self._heartbeat_interval: float = float(rtds_cfg.get("heartbeat_interval_seconds", 20))
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
        log.info("RTDS Chainlink connected: url=%s sub=%s", self._ws_url, self._sub_msg)
        ws.send(self._sub_msg)
        self._last_heartbeat = time.time()

    def _on_message(self, ws, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        events = data if isinstance(data, list) else [data]
        for event in events:
            self._handle_event(event)

        # Application-level heartbeat
        now = time.time()
        if now - self._last_heartbeat >= self._heartbeat_interval:
            try:
                ws.send(json.dumps({"type": "ping"}))
                self._last_heartbeat = now
            except Exception:
                pass

    def _handle_event(self, event: dict) -> None:
        """Parse a price event from RTDS. Extract price and oracle timestamp."""
        raw_price = event.get(self._price_field)
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

        # Prefer oracle updatedAt; fall back to message timestamp; fall back to now
        raw_oracle_ts = event.get(self._oracle_ts_field)
        raw_msg_ts = event.get(self._msg_ts_field)

        if raw_oracle_ts is not None:
            try:
                oracle_ts = float(raw_oracle_ts)
                # Sanity: oracle ts should be a unix second in plausible range
                if oracle_ts > 1_000_000_000:
                    source = _SRC_RTDS_ORACLE
                else:
                    # Might be milliseconds
                    oracle_ts = oracle_ts / 1000.0
                    source = _SRC_RTDS_ORACLE
            except (TypeError, ValueError):
                oracle_ts = now
                source = _SRC_RTDS_MSG_TS
        elif raw_msg_ts is not None:
            try:
                oracle_ts = float(raw_msg_ts)
                if oracle_ts > 1_600_000_000_000:  # milliseconds
                    oracle_ts /= 1000.0
                source = _SRC_RTDS_MSG_TS
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
