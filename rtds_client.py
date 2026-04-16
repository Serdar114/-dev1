"""
Polymarket RTDS WebSocket client.

Endpoint: wss://ws-live-data.polymarket.com
Maintains a single connection with two subscriptions:
  - crypto_prices (Binance BTC/USDT)
  - crypto_prices_chainlink (Chainlink BTC/USD)

Subscribe payloads use JSON-encoded filters strings (verified spec).
Ping text frame sent every 5 seconds to keep connection alive.
Reconnects with exponential backoff on disconnect.
No REST fallback — if RTDS is down, prices are stale.
"""
import json
import threading
import time
from typing import Callable, Optional

import websocket

from schemas import DualReferenceSnapshot

RTDS_ENDPOINT = "wss://ws-live-data.polymarket.com"
PING_INTERVAL_S = 5
BINANCE_STALE_MS = 3000
CHAINLINK_STALE_MS = 5000
BACKOFF_SEQUENCE = [1, 2, 5, 10, 30, 60]

# Verified subscribe payloads — filters must be JSON-encoded strings.
# Chainlink requires type="*" not "update".
SUBSCRIBE_BINANCE = json.dumps({
    "action": "subscribe",
    "subscriptions": [
        {
            "topic": "crypto_prices",
            "type": "update",
            "filters": json.dumps({"symbol": "btcusdt"}),
        }
    ],
})

SUBSCRIBE_CHAINLINK = json.dumps({
    "action": "subscribe",
    "subscriptions": [
        {
            "topic": "crypto_prices_chainlink",
            "type": "*",
            "filters": json.dumps({"symbol": "btc/usd"}),
        }
    ],
})


class RTDSClient:
    def __init__(
        self,
        on_event: Optional[Callable[[str, dict], None]] = None,
        shutdown_event: Optional[threading.Event] = None,
    ):
        self._lock = threading.Lock()
        self._on_event = on_event
        self._shutdown = shutdown_event or threading.Event()

        # Binance state
        self._binance_price: Optional[float] = None
        self._binance_source_ts: Optional[int] = None
        self._binance_local_ts: Optional[int] = None

        # Chainlink state
        self._chainlink_price: Optional[float] = None
        self._chainlink_source_ts: Optional[int] = None
        self._chainlink_local_ts: Optional[int] = None

        self._ws: Optional[websocket.WebSocketApp] = None
        self._connected = False
        self._backoff_index = 0

    def start(self) -> None:
        """Start the client in a daemon background thread."""
        t = threading.Thread(
            target=self._run_loop, daemon=True, name="rtds-run-loop"
        )
        t.start()

    def _run_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                self._connect()
            except Exception as exc:
                self._emit("error", {"component": "rtds", "message": str(exc)})

            if self._shutdown.is_set():
                break

            backoff = BACKOFF_SEQUENCE[
                min(self._backoff_index, len(BACKOFF_SEQUENCE) - 1)
            ]
            self._backoff_index = min(
                self._backoff_index + 1, len(BACKOFF_SEQUENCE) - 1
            )
            self._emit("reconnect_scheduled", {"component": "rtds", "backoff_s": backoff})
            self._shutdown.wait(backoff)

    def _connect(self) -> None:
        self._ws = websocket.WebSocketApp(
            RTDS_ENDPOINT,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever()

    # ------------------------------------------------------------------ #
    # WebSocketApp callbacks                                               #
    # ------------------------------------------------------------------ #

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        with self._lock:
            self._connected = True
            self._backoff_index = 0

        self._emit("connected", {"endpoint": RTDS_ENDPOINT})
        ws.send(SUBSCRIBE_BINANCE)
        ws.send(SUBSCRIBE_CHAINLINK)

        ping_thread = threading.Thread(
            target=self._ping_loop, args=(ws,), daemon=True, name="rtds-ping"
        )
        ping_thread.start()

    def _ping_loop(self, ws: websocket.WebSocketApp) -> None:
        """Send 'PING' text message every PING_INTERVAL_S seconds."""
        while not self._shutdown.is_set():
            with self._lock:
                still_connected = self._connected
            if not still_connected:
                break
            try:
                ws.send("PING")
            except Exception:
                break
            self._shutdown.wait(PING_INTERVAL_S)

    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, ValueError):
            # Could be a plain "PONG" text response — ignore.
            return

        # Subscribe rejection: body.message contains regex error text.
        body = data.get("body")
        if isinstance(body, dict):
            msg = body.get("message", "")
            if "invalid Subscription" in msg or "does not match regex" in msg:
                self._emit("subscribe_rejected", {"raw_message": msg})
                return

        topic = data.get("topic")
        payload = data.get("payload")
        if not topic or not payload or not isinstance(payload, dict):
            return

        now_ms = int(time.time() * 1000)

        if topic == "crypto_prices":
            symbol = payload.get("symbol")
            if symbol == "btcusdt":
                value = payload.get("value")
                src_ts = payload.get("timestamp")
                if value is not None:
                    with self._lock:
                        self._binance_price = float(value)
                        self._binance_source_ts = int(src_ts) if src_ts is not None else None
                        self._binance_local_ts = now_ms

        elif topic == "crypto_prices_chainlink":
            symbol = payload.get("symbol")
            if symbol == "btc/usd":
                value = payload.get("value")
                src_ts = payload.get("timestamp")
                if value is not None:
                    with self._lock:
                        self._chainlink_price = float(value)
                        self._chainlink_source_ts = int(src_ts) if src_ts is not None else None
                        self._chainlink_local_ts = now_ms

    def _on_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        self._emit("ws_error", {"component": "rtds", "error": str(error)})

    def _on_close(
        self,
        ws: websocket.WebSocketApp,
        close_status_code: Optional[int],
        close_msg: Optional[str],
    ) -> None:
        with self._lock:
            self._connected = False
        self._emit(
            "disconnected",
            {
                "component": "rtds",
                "status_code": close_status_code,
                "message": close_msg,
            },
        )

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get_snapshot(self) -> DualReferenceSnapshot:
        """
        Always returns a DualReferenceSnapshot.
        Prices reflect the last received value (possibly None if never received).
        stale_* = True when the last local timestamp exceeds the stale threshold.
        basis_bps is only set when both feeds are fresh.
        """
        now_ms = int(time.time() * 1000)

        with self._lock:
            binance_price = self._binance_price
            binance_source_ts = self._binance_source_ts
            binance_local_ts = self._binance_local_ts
            chainlink_price = self._chainlink_price
            chainlink_source_ts = self._chainlink_source_ts
            chainlink_local_ts = self._chainlink_local_ts

        binance_stale = (
            binance_local_ts is None
            or (now_ms - binance_local_ts) > BINANCE_STALE_MS
        )
        chainlink_stale = (
            chainlink_local_ts is None
            or (now_ms - chainlink_local_ts) > CHAINLINK_STALE_MS
        )

        basis_bps: Optional[float] = None
        if (
            binance_price is not None
            and chainlink_price is not None
            and not binance_stale
            and not chainlink_stale
            and binance_price != 0
        ):
            basis_bps = ((chainlink_price - binance_price) / binance_price) * 10_000

        lag_ms: Optional[int] = None
        if binance_source_ts is not None and chainlink_source_ts is not None:
            lag_ms = binance_source_ts - chainlink_source_ts

        return DualReferenceSnapshot(
            ts_local=now_ms,
            binance_price=binance_price,
            binance_source_ts=binance_source_ts,
            binance_local_ts=binance_local_ts,
            binance_stale=binance_stale,
            chainlink_price=chainlink_price,
            chainlink_source_ts=chainlink_source_ts,
            chainlink_local_ts=chainlink_local_ts,
            chainlink_stale=chainlink_stale,
            basis_bps=basis_bps,
            lag_ms=lag_ms,
        )

    def stop(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _emit(self, event_type: str, data: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event_type, data)
            except Exception:
                pass
