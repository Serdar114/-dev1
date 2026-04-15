"""
rtds_client.py — Dual-source BTC reference price via Polymarket RTDS WebSocket.

Single connection to Polymarket RTDS WS:
  wss://ws-live-data.polymarket.com

On connect, two subscriptions are sent:
  {"type": "subscribe", "channel": "crypto_prices",           "ticker": "btcusdt"}
  {"type": "subscribe", "channel": "crypto_prices_chainlink", "ticker": "btc/usd"}

This yields two independent price streams over one connection:
  Binance    (crypto_prices)           — live traded spot; observer/reference only
  Chainlink  (crypto_prices_chainlink) — aggregator; settlement truth for btc-updown-*

Separate state is maintained for each source.
latest_dual() returns a DualPriceSnapshot with both prices, basis, and lag.
latest()      returns Binance-only ExternalPriceSnapshot (backward compat).

Binance REST fallback (api.binance.com) is used during WS reconnect back-off.
Chainlink has no REST fallback — if RTDS is down, chainlink fields are null/stale.

Zero strategy logic. No orders. No hardcoded price defaults.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

import requests
import websocket  # websocket-client

import logger as log
from schemas import DualPriceSnapshot, ExternalPriceSnapshot

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POLYMARKET_RTDS_URL = "wss://ws-live-data.polymarket.com"

# Binance REST fallback (used only when RTDS WS is down)
BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_REST_SYMBOL = "BTCUSDT"

# Stale thresholds
BINANCE_STALE_THRESHOLD_MS    = 5_000   # 5s — live WS feed; flag quickly
CHAINLINK_STALE_THRESHOLD_MS  = 30_000  # 30s — aggregator; less frequent updates ok

# Heartbeat miss threshold (no message at all from RTDS)
HEARTBEAT_MISS_THRESHOLD_MS = 15_000

# REST fallback polling interval
REST_FALLBACK_INTERVAL_S = 2.0

# Reconnect back-off delays (seconds)
RECONNECT_DELAYS_S = [1, 2, 5, 10, 30, 60]

HTTP_TIMEOUT_S = 5

# Subscription messages sent on connection open
_SUB_BINANCE = {
    "type": "subscribe",
    "channel": "crypto_prices",
    "ticker": "btcusdt",
}
_SUB_CHAINLINK = {
    "type": "subscribe",
    "channel": "crypto_prices_chainlink",
    "ticker": "btc/usd",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _parse_price(raw: Any, field: str, context: str) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        log.log_parse_anomaly(context, field, raw, f"cannot convert to float: {exc}")
        return None


def _parse_ts_ms(raw: Any, field: str, context: str) -> Optional[int]:
    """Accept epoch ms (int/float) or epoch seconds heuristic."""
    if raw is None:
        return None
    try:
        v = float(raw)
        return int(v) if v > 1e12 else int(v * 1000)
    except (TypeError, ValueError) as exc:
        log.log_parse_anomaly(context, field, raw, f"cannot parse timestamp: {exc}")
        return None


# ---------------------------------------------------------------------------
# RTDS client
# ---------------------------------------------------------------------------

class RtdsClient:
    """
    Maintains dual BTC reference prices (Binance + Chainlink) from Polymarket RTDS.

    Usage:
        client = RtdsClient()
        client.start()                    # non-blocking background thread
        dual = client.latest_dual()       # DualPriceSnapshot or None
        snap = client.latest()            # ExternalPriceSnapshot (Binance only, compat)
        client.stop()
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Binance state
        self._binance_price: Optional[float] = None
        self._binance_source_ts: Optional[int] = None   # exchange event time, epoch ms
        self._binance_local_ts: Optional[int] = None    # local receive time, epoch ms

        # Chainlink state
        self._chainlink_price: Optional[float] = None
        self._chainlink_source_ts: Optional[int] = None  # source epoch ms
        self._chainlink_local_ts: Optional[int] = None   # local receive time, epoch ms

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
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="rtds_client"
        )
        self._thread.start()
        log.log_system_event(
            "rtds_start",
            detail=f"connecting to {POLYMARKET_RTDS_URL}",
            extra={"subscriptions": [_SUB_BINANCE, _SUB_CHAINLINK]},
        )

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
        """
        Return Binance price as ExternalPriceSnapshot.
        Backward-compatible with callers that use single-source snapshot.
        Returns None if no Binance price received yet.
        """
        with self._lock:
            if self._binance_price is None or self._binance_local_ts is None:
                return None
            ts_local = _now_ms()
            age = ts_local - self._binance_local_ts
            return ExternalPriceSnapshot(
                ts_local=ts_local,
                price=self._binance_price,
                source_ts=self._binance_source_ts,
                data_age_ms=age,
                source="polymarket_rtds_binance",
                symbol="BTCUSDT",
            )

    def latest_dual(self) -> DualPriceSnapshot:
        """
        Return combined snapshot of both sources.
        Always returns a DualPriceSnapshot (never None); missing fields are null.
        """
        ts_local = _now_ms()

        with self._lock:
            b_price    = self._binance_price
            b_src_ts   = self._binance_source_ts
            b_local    = self._binance_local_ts
            cl_price   = self._chainlink_price
            cl_src_ts  = self._chainlink_source_ts
            cl_local   = self._chainlink_local_ts

        # Binance age / stale
        if b_local is not None:
            b_age   = ts_local - b_local
            b_stale = b_age > BINANCE_STALE_THRESHOLD_MS
        else:
            b_age   = None
            b_stale = True

        # Chainlink age / stale — measure from source_ts if available, else local_ts
        cl_ref = cl_src_ts if cl_src_ts is not None else cl_local
        if cl_ref is not None:
            cl_age   = ts_local - cl_ref
            cl_stale = cl_age > CHAINLINK_STALE_THRESHOLD_MS
        else:
            cl_age   = None
            cl_stale = True

        # Basis: (binance - chainlink) / chainlink * 10_000 bps
        basis_bps: Optional[float] = None
        if b_price is not None and cl_price is not None and cl_price > 0:
            basis_bps = round((b_price - cl_price) / cl_price * 10_000, 2)

        # Lag: binance_source_ts - chainlink_source_ts
        # Positive → Binance timestamp more recent than Chainlink timestamp
        lag_ms: Optional[int] = None
        if b_src_ts is not None and cl_src_ts is not None:
            lag_ms = b_src_ts - cl_src_ts

        return DualPriceSnapshot(
            ts_local=ts_local,
            binance_price=b_price,
            binance_source_ts=b_src_ts,
            binance_data_age_ms=b_age,
            binance_stale=b_stale,
            chainlink_price=cl_price,
            chainlink_source_ts=cl_src_ts,
            chainlink_data_age_ms=cl_age,
            chainlink_stale=cl_stale,
            basis_bps=basis_bps,
            lag_ms=lag_ms,
        )

    def is_stale(self) -> bool:
        """True if no fresh Binance price within BINANCE_STALE_THRESHOLD_MS."""
        with self._lock:
            if self._binance_local_ts is None:
                return True
            return (_now_ms() - self._binance_local_ts) > BINANCE_STALE_THRESHOLD_MS

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def last_message_ms(self) -> Optional[int]:
        return self._last_message_ms

    # -----------------------------------------------------------------------
    # Internal state updates
    # -----------------------------------------------------------------------

    def _update_binance(self, price: float, source_ts: Optional[int]) -> None:
        ts_local = _now_ms()
        with self._lock:
            self._binance_price     = price
            self._binance_source_ts = source_ts
            self._binance_local_ts  = ts_local
        self._last_message_ms = ts_local

        snap = ExternalPriceSnapshot(
            ts_local=ts_local,
            price=price,
            source_ts=source_ts,
            data_age_ms=(ts_local - source_ts) if source_ts else None,
            source="polymarket_rtds_binance",
            symbol="BTCUSDT",
        )
        log.log_external_price(snap)

    def _update_chainlink(self, price: float, source_ts: Optional[int]) -> None:
        ts_local = _now_ms()
        with self._lock:
            self._chainlink_price     = price
            self._chainlink_source_ts = source_ts
            self._chainlink_local_ts  = ts_local
        self._last_message_ms = ts_local

        snap = ExternalPriceSnapshot(
            ts_local=ts_local,
            price=price,
            source_ts=source_ts,
            data_age_ms=(ts_local - source_ts) if source_ts else None,
            source="polymarket_rtds_chainlink",
            symbol="BTCUSD",
        )
        log.log_external_price(snap)

    # -----------------------------------------------------------------------
    # WebSocket callbacks
    # -----------------------------------------------------------------------

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        ws.send(json.dumps(_SUB_BINANCE))
        ws.send(json.dumps(_SUB_CHAINLINK))
        log.log_system_event(
            "rtds_ws_connected",
            detail="RTDS connected; sent binance + chainlink subscriptions",
        )

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        ts_local = _now_ms()
        self._last_message_ms = ts_local

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.log_parse_anomaly("rtds_ws", "raw_message", raw[:200], f"JSON decode: {exc}")
            return

        if not isinstance(msg, dict):
            log.log_parse_anomaly("rtds_ws", "msg_type", type(msg).__name__, "expected dict")
            return

        # Route by channel field; fall back to type / event_type
        channel = (
            msg.get("channel")
            or msg.get("type")
            or msg.get("event_type")
            or ""
        ).lower()

        # Subscription confirmations and heartbeats — nothing to do
        if channel in ("subscribed", "heartbeat", "connected", "subscribe"):
            return

        # Route to correct handler
        if "chainlink" in channel:
            self._handle_price_message(msg, "chainlink", channel)
        elif channel in ("crypto_prices", "btcusdt", "price", "crypto_price"):
            self._handle_price_message(msg, "binance", channel)
        else:
            # Unknown channel — try to route by ticker content as fallback
            ticker = (
                msg.get("ticker") or msg.get("asset") or msg.get("symbol") or ""
            ).lower()
            if "chainlink" in channel or "usd" in ticker:
                self._handle_price_message(msg, "chainlink", channel)
            elif ticker in ("btcusdt", "btc", "btcusd"):
                self._handle_price_message(msg, "binance", channel)
            else:
                log.log_parse_anomaly(
                    "rtds_ws", "channel", channel,
                    f"unroutable message; ticker={ticker!r} raw={raw[:200]}"
                )

    def _handle_price_message(
        self, msg: dict, source: str, channel: str
    ) -> None:
        """
        Extract price and timestamp from a RTDS price message.
        Tries several common field name shapes defensively.
        """
        context = f"rtds_ws.{source}"

        # Price: try common field names
        price_raw = (
            msg.get("price")
            or msg.get("p")
            or (msg.get("data") or {}).get("price")
        )
        if price_raw is None:
            log.log_parse_anomaly(context, "price", msg, "no price field found")
            return

        price = _parse_price(price_raw, "price", context)
        if price is None or price <= 0:
            log.log_parse_anomaly(context, "price", price_raw, "price is None or ≤ 0")
            return

        # Timestamp: try common field names
        ts_raw = (
            msg.get("timestamp")
            or msg.get("ts")
            or msg.get("t")
            or msg.get("T")
            or msg.get("E")
            or (msg.get("data") or {}).get("timestamp")
        )
        source_ts = _parse_ts_ms(ts_raw, "timestamp", context)

        if source == "binance":
            self._update_binance(price, source_ts)
        else:
            self._update_chainlink(price, source_ts)

    def _on_error(self, ws: websocket.WebSocketApp, error: Any) -> None:
        log.log_exception(
            "rtds_ws.on_error",
            error if isinstance(error, Exception) else Exception(str(error)),
        )

    def _on_close(self, ws: websocket.WebSocketApp, code: Any, msg: Any) -> None:
        log.log_system_event("rtds_ws_closed", detail=f"code={code} msg={msg}")

    # -----------------------------------------------------------------------
    # REST fallback — Binance only (used during WS reconnect back-off)
    # -----------------------------------------------------------------------

    def _rest_poll_once(self) -> bool:
        """Fetch Binance price via REST. Returns True if successful."""
        try:
            resp = requests.get(
                BINANCE_REST_URL,
                params={"symbol": BINANCE_REST_SYMBOL},
                timeout=HTTP_TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
            price_raw = data.get("price")
            if price_raw is None:
                log.log_parse_anomaly("rtds_rest", "price", data, "no 'price' field")
                return False
            price = float(price_raw)
            ts_local = _now_ms()
            with self._lock:
                self._binance_price     = price
                self._binance_source_ts = None   # REST response has no exchange ts
                self._binance_local_ts  = ts_local
            snap = ExternalPriceSnapshot(
                ts_local=ts_local,
                price=price,
                source_ts=None,
                data_age_ms=None,
                source="binance_rest_fallback",
                symbol=BINANCE_REST_SYMBOL,
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

                # During back-off: poll Binance REST for Binance price.
                # Chainlink has no REST fallback — remains at last known value / stale.
                deadline = time.time() + delay
                while time.time() < deadline and not self._stop_event.is_set():
                    self._rest_poll_once()
                    time.sleep(min(REST_FALLBACK_INTERVAL_S, deadline - time.time()))
                if self._stop_event.is_set():
                    break

            try:
                ws = websocket.WebSocketApp(
                    POLYMARKET_RTDS_URL,
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
