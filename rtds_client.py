"""
rtds_client.py — Polymarket RTDS dual reference feed.

PATCH: Complete refactor. Single WebSocket to wss://ws-live-data.polymarket.com,
two subscriptions (Binance btcusdt + Chainlink btc/usd).

Subscribe messages (copy-paste ready):
  Binance:
    {"action":"subscribe","subscriptions":[{"topic":"crypto_prices","type":"update","filters":"btcusdt"}]}
  Chainlink:
    {"action":"subscribe","subscriptions":[{"topic":"crypto_prices_chainlink","type":"update","filters":"btc/usd"}]}

Expected payload format:
  {"topic":"crypto_prices","type":"update","timestamp":1753314088421,
   "payload":{"symbol":"btcusdt","timestamp":1753314088395,"value":67234.50}}

Stale thresholds:
  Binance   > 3 000 ms
  Chainlink > 5 000 ms  (Chainlink update frequency is lower — measure in practice)

No REST fallback: if RTDS WS is down, both feeds are unavailable; having
Binance-only data without Chainlink would be misleading for the dual-feed logs.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

import websocket  # websocket-client

from schemas import DualReferenceSnapshot

log = logging.getLogger(__name__)

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"

_BINANCE_STALE_MS = 3_000
_CHAINLINK_STALE_MS = 5_000

_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 60.0
_BACKOFF_FACTOR = 2.0

# ── subscription messages (verbatim, per spec) ─────────────────────────────────

_SUB_BINANCE: str = json.dumps({
    "action": "subscribe",
    "subscriptions": [
        {"topic": "crypto_prices", "type": "update", "filters": "btcusdt"},
    ],
})

_SUB_CHAINLINK: str = json.dumps({
    "action": "subscribe",
    "subscriptions": [
        {"topic": "crypto_prices_chainlink", "type": "update", "filters": "btc/usd"},
    ],
})


class RTDSClient:
    """
    Maintains a single WebSocket to Polymarket RTDS with dual subscription.
    State for each feed is tracked independently under a lock.
    Exposes get_snapshot() → DualReferenceSnapshot (always returns a valid
    object; fields are None / stale=True when feed has not yet delivered).
    """

    def __init__(self, shutdown_event: threading.Event) -> None:
        self._shutdown = shutdown_event
        self._lock = threading.Lock()

        # Binance feed state
        self._binance_price: Optional[float] = None
        self._binance_source_ts: Optional[int] = None
        self._binance_local_ts: Optional[int] = None

        # Chainlink feed state
        self._chainlink_price: Optional[float] = None
        self._chainlink_source_ts: Optional[int] = None
        self._chainlink_local_ts: Optional[int] = None

        self._thread: Optional[threading.Thread] = None

    # ── public ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, name="rtds", daemon=True
        )
        self._thread.start()

    def get_snapshot(self) -> DualReferenceSnapshot:
        """
        Thread-safe snapshot of both feeds.
        binance_stale / chainlink_stale are True when the feed has not
        delivered within its stale threshold (or never delivered).
        """
        now_ms = int(time.time() * 1000)
        with self._lock:
            b_price = self._binance_price
            b_src_ts = self._binance_source_ts
            b_loc_ts = self._binance_local_ts
            c_price = self._chainlink_price
            c_src_ts = self._chainlink_source_ts
            c_loc_ts = self._chainlink_local_ts

        b_stale = b_loc_ts is None or (now_ms - b_loc_ts) > _BINANCE_STALE_MS
        c_stale = c_loc_ts is None or (now_ms - c_loc_ts) > _CHAINLINK_STALE_MS

        basis_bps: Optional[float] = None
        if b_price is not None and c_price is not None and b_price != 0:
            basis_bps = ((c_price - b_price) / b_price) * 10_000.0

        lag_ms: Optional[int] = None
        if b_src_ts is not None and c_src_ts is not None:
            lag_ms = b_src_ts - c_src_ts

        return DualReferenceSnapshot(
            ts_local=now_ms,
            binance_price=b_price,
            binance_source_ts=b_src_ts,
            binance_local_ts=b_loc_ts,
            binance_stale=b_stale,
            chainlink_price=c_price,
            chainlink_source_ts=c_src_ts,
            chainlink_local_ts=c_loc_ts,
            chainlink_stale=c_stale,
            basis_bps=basis_bps,
            lag_ms=lag_ms,
        )

    # ── internal ──────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        backoff = _BACKOFF_BASE
        while not self._shutdown.is_set():
            try:
                self._connect_and_run()
                backoff = _BACKOFF_BASE
            except Exception as exc:
                log.warning("rtds: connection error: %s", exc)
            if self._shutdown.is_set():
                break
            wait = min(backoff, _BACKOFF_MAX)
            log.info("rtds: reconnect in %.1fs (backoff)", wait)
            self._shutdown.wait(wait)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)

    def _connect_and_run(self) -> None:
        log.info("rtds: connecting to %s", RTDS_WS_URL)
        ws_closed = threading.Event()

        def on_open(ws):
            ws.send(_SUB_BINANCE)
            ws.send(_SUB_CHAINLINK)
            log.info("rtds: subscribed binance + chainlink")

        def on_message(ws, message):
            try:
                self._handle_message(json.loads(message))
            except Exception as exc:
                log.debug("rtds: parse error: %s", exc)

        def on_error(ws, error):
            log.warning("rtds: ws error: %s", error)

        def on_close(ws, code, msg):
            log.info("rtds: ws closed (code=%s)", code)
            ws_closed.set()

        ws = websocket.WebSocketApp(
            RTDS_WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        runner = threading.Thread(
            target=ws.run_forever,
            kwargs={"ping_interval": 20, "ping_timeout": 10},
            daemon=True,
        )
        runner.start()

        while not self._shutdown.is_set() and not ws_closed.is_set():
            time.sleep(0.5)

        ws.close()
        runner.join(timeout=5.0)

    def _handle_message(self, data: dict) -> None:
        topic = data.get("topic")
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return

        value_raw = payload.get("value")
        if value_raw is None:
            return
        try:
            price = float(value_raw)
        except (TypeError, ValueError):
            return

        # Source timestamp: prefer payload-level ts, fall back to outer ts
        src_ts_raw = payload.get("timestamp") or data.get("timestamp")
        src_ts = int(src_ts_raw) if src_ts_raw is not None else None

        local_ts = int(time.time() * 1000)

        with self._lock:
            if topic == "crypto_prices":
                self._binance_price = price
                self._binance_source_ts = src_ts
                self._binance_local_ts = local_ts
            elif topic == "crypto_prices_chainlink":
                self._chainlink_price = price
                self._chainlink_source_ts = src_ts
                self._chainlink_local_ts = local_ts
