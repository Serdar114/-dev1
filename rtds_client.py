"""
rtds_client.py — External BTC reference price stream.

Source: Binance public websocket (no auth, no rate limits for ticker stream).
Primary stream:  wss://stream.binance.com:9443/ws/btcusdt@aggTrade
Fallback poll:   https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT

Responsibilities:
  - Maintain current external BTC reference price
  - Track price, source timestamp, local receive timestamp, data_age_ms
  - Expose a latest-snapshot getter (thread-safe)
  - Log disconnects, stale data, reconnects clearly
  - Zero strategy logic

Stale threshold: if last price update is > STALE_THRESHOLD_MS old, mark as stale.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

import requests
import websocket  # websocket-client

import logger as log
from schemas import ExternalPriceSnapshot

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price"
SYMBOL = "BTCUSDT"

# If no update in this many ms, mark data as stale
STALE_THRESHOLD_MS = 5_000

# Heartbeat miss if no message for this long
HEARTBEAT_MISS_THRESHOLD_MS = 15_000

# REST fallback polling interval when WS is down
REST_FALLBACK_INTERVAL_S = 2.0

# Reconnect delays (seconds)
RECONNECT_DELAYS_S = [1, 2, 5, 10, 30, 60]

HTTP_TIMEOUT_S = 5

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# RTDS client
# ---------------------------------------------------------------------------

class RtdsClient:
    """
    Maintains a live BTC/USDT reference price from Binance.

    Usage:
        client = RtdsClient()
        client.start()               # non-blocking background thread
        snap = client.latest()       # ExternalPriceSnapshot or None
        client.stop()
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._price: Optional[float] = None
        self._source_ts: Optional[int] = None   # exchange event time, epoch ms
        self._local_ts: Optional[int] = None    # local receive time, epoch ms

        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._reconnect_count: int = 0
        self._last_message_ms: Optional[int] = None

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def start(self) -> None:
        """Start the price stream in a background daemon thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="rtds_client")
        self._thread.start()
        log.log_system_event("rtds_start", detail=f"connecting to {BINANCE_WS_URL}")

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        log.log_system_event("rtds_stop", detail="RTDS client stopped")

    def latest(self) -> Optional[ExternalPriceSnapshot]:
        """Return latest snapshot. Returns None if no data received yet."""
        with self._lock:
            if self._price is None or self._local_ts is None:
                return None
            ts_local = _now_ms()
            data_age_ms = ts_local - self._local_ts
            return ExternalPriceSnapshot(
                ts_local=ts_local,
                price=self._price,
                source_ts=self._source_ts,
                data_age_ms=data_age_ms,
                source="binance_ws" if self._reconnect_count == 0 else "binance",
                symbol=SYMBOL,
            )

    def is_stale(self) -> bool:
        """True if no fresh price within STALE_THRESHOLD_MS."""
        with self._lock:
            if self._local_ts is None:
                return True
            return (_now_ms() - self._local_ts) > STALE_THRESHOLD_MS

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def last_message_ms(self) -> Optional[int]:
        return self._last_message_ms

    # -----------------------------------------------------------------------
    # Internal update
    # -----------------------------------------------------------------------

    def _update(self, price: float, source_ts: Optional[int]) -> None:
        ts_local = _now_ms()
        with self._lock:
            self._price = price
            self._source_ts = source_ts
            self._local_ts = ts_local
        self._last_message_ms = ts_local

    # -----------------------------------------------------------------------
    # WebSocket callbacks
    # -----------------------------------------------------------------------

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        log.log_system_event("rtds_ws_connected", detail=f"Binance WS connected ({SYMBOL})")

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.log_parse_anomaly("rtds_ws", "raw_message", raw[:200], f"JSON decode: {exc}")
            return

        # aggTrade payload: {"e":"aggTrade","E":..., "p":"price", "T":tradeTime, ...}
        event_type = msg.get("e")
        if event_type in ("aggTrade", "trade"):
            price_raw = msg.get("p")
            event_time = msg.get("E") or msg.get("T")
            if price_raw is None:
                log.log_parse_anomaly("rtds_ws", "price", msg, "missing 'p' in aggTrade")
                return
            try:
                price = float(price_raw)
                source_ts = int(event_time) if event_time is not None else None
                self._update(price, source_ts)
                log.log_external_price(self.latest())
            except (TypeError, ValueError) as exc:
                log.log_parse_anomaly("rtds_ws", "price_float", price_raw, str(exc))
        elif event_type == "bookTicker":
            # fallback if stream changes: use mid of bid/ask
            bid_raw = msg.get("b")
            ask_raw = msg.get("a")
            if bid_raw and ask_raw:
                try:
                    price = (float(bid_raw) + float(ask_raw)) / 2
                    self._update(price, msg.get("T"))
                    log.log_external_price(self.latest())
                except (TypeError, ValueError) as exc:
                    log.log_parse_anomaly("rtds_ws", "bookTicker_price", msg, str(exc))
        else:
            # Unknown event type — log raw snippet, do not crash
            log.log_parse_anomaly(
                "rtds_ws", "event_type", event_type, f"unrecognised Binance event; raw={raw[:200]}"
            )

    def _on_error(self, ws: websocket.WebSocketApp, error: Any) -> None:
        log.log_exception(
            "rtds_ws.on_error",
            error if isinstance(error, Exception) else Exception(str(error)),
        )

    def _on_close(self, ws: websocket.WebSocketApp, code: Any, msg: Any) -> None:
        log.log_system_event("rtds_ws_closed", detail=f"code={code} msg={msg}")

    # -----------------------------------------------------------------------
    # REST fallback (poll when WS is unavailable)
    # -----------------------------------------------------------------------

    def _rest_poll_once(self) -> bool:
        """Fetch price via REST. Returns True if successful."""
        try:
            resp = requests.get(
                BINANCE_REST_URL, params={"symbol": SYMBOL}, timeout=HTTP_TIMEOUT_S
            )
            resp.raise_for_status()
            data = resp.json()
            price_raw = data.get("price")
            if price_raw is None:
                log.log_parse_anomaly("rtds_rest", "price", data, "no 'price' field")
                return False
            price = float(price_raw)
            self._update(price, None)
            snap = self.latest()
            if snap:
                snap = ExternalPriceSnapshot(
                    ts_local=snap.ts_local,
                    price=snap.price,
                    source_ts=snap.source_ts,
                    data_age_ms=snap.data_age_ms,
                    source="binance_rest",
                    symbol=SYMBOL,
                )
                log.log_external_price(snap)
            return True
        except Exception as exc:
            log.log_exception("rtds_rest_poll", exc)
            return False

    # -----------------------------------------------------------------------
    # Reconnect loop
    # -----------------------------------------------------------------------

    def _run_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            delay = RECONNECT_DELAYS_S[min(attempt, len(RECONNECT_DELAYS_S) - 1)]
            if attempt > 0:
                self._reconnect_count += 1
                log.log_reconnect("rtds_ws", attempt, reason=f"back-off {delay}s")

                # While waiting for reconnect, fall back to REST polling
                deadline = time.time() + delay
                while time.time() < deadline and not self._stop_event.is_set():
                    self._rest_poll_once()
                    time.sleep(min(REST_FALLBACK_INTERVAL_S, deadline - time.time()))
                if self._stop_event.is_set():
                    break

            try:
                ws = websocket.WebSocketApp(
                    BINANCE_WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws = ws
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                log.log_exception("rtds_ws.run_forever", exc, {"attempt": attempt})

            attempt += 1

        log.log_system_event("rtds_loop_exit", detail="stop event set; exiting")

    # -----------------------------------------------------------------------
    # Liveness check (call periodically from main loop)
    # -----------------------------------------------------------------------

    def check_liveness(self) -> None:
        if self._last_message_ms is None:
            return
        gap = _now_ms() - self._last_message_ms
        if gap > HEARTBEAT_MISS_THRESHOLD_MS:
            log.log_heartbeat_miss("rtds_ws", self._last_message_ms, HEARTBEAT_MISS_THRESHOLD_MS)
