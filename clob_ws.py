"""
clob_ws.py — Public CLOB websocket subscriber for Polymarket market data.

Subscribes to the public market data feed for one or more token ids.
Maintains lightweight in-memory top-of-book state per token.
Detects and logs: stale book, empty ask/bid, crossed book, malformed updates, reconnects.

No auth. No order placement. Read-only market data intake only.

Polymarket CLOB WS endpoint:
  wss://ws-subscriptions-clob.polymarket.com/ws/market

Subscription message:
  {"assets_ids": ["<token_id>", ...], "type": "market"}

Event types received:
  "book"         — full book snapshot for an asset
  "price_change" — incremental price level changes
  (other types logged as anomalies but not crashed on)
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import websocket  # websocket-client

import logger as log
from schemas import BookSnapshot

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# After this many ms without a message, flag the book as stale
STALE_BOOK_THRESHOLD_MS = 15_000

# After this many ms without any message at all, log heartbeat miss
HEARTBEAT_MISS_THRESHOLD_MS = 30_000

# Keep last N events in memory for debugging
EVENT_BUFFER_SIZE = 200

# Reconnect back-off (seconds): attempt index → delay
RECONNECT_DELAYS_S = [1, 2, 5, 10, 30, 60]

# Log first N price_change messages in full for debugging
_PRICE_CHANGE_DEBUG_LIMIT = 5

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _best_bid_ask(
    levels: Dict[float, float], is_bid: bool
) -> Tuple[Optional[float], Optional[float]]:
    """Return (best_price, size_at_best) from a price→size dict."""
    if not levels:
        return None, None
    best = max(levels) if is_bid else min(levels)
    return best, levels[best]


# ---------------------------------------------------------------------------
# Per-token book state
# ---------------------------------------------------------------------------

class _TokenBookState:
    """
    Maintains current best bid/ask for one token, updated by WS events.
    All access is protected by a threading.Lock().
    """

    def __init__(self, token_id: str, side_label: Optional[str] = None) -> None:
        self.token_id = token_id
        self.side_label = side_label
        self._lock = threading.Lock()
        # price → size (float). Size=0 means level removed.
        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}
        self.last_update_ms: Optional[int] = None
        self.book_timestamp: Optional[int] = None   # exchange-reported ts if available
        self.update_count: int = 0

    def apply_snapshot(self, bids: List[Dict], asks: List[Dict], book_ts: Optional[int]) -> None:
        """Replace entire book with a new snapshot."""
        new_bids: Dict[float, float] = {}
        new_asks: Dict[float, float] = {}
        for lvl in bids:
            try:
                p, s = float(lvl.get("price", lvl.get("p", 0))), float(lvl.get("size", lvl.get("s", 0)))
                if s > 0:
                    new_bids[p] = s
            except (TypeError, ValueError):
                pass
        for lvl in asks:
            try:
                p, s = float(lvl.get("price", lvl.get("p", 0))), float(lvl.get("size", lvl.get("s", 0)))
                if s > 0:
                    new_asks[p] = s
            except (TypeError, ValueError):
                pass
        with self._lock:
            self._bids = new_bids
            self._asks = new_asks
            self.last_update_ms = _now_ms()
            self.book_timestamp = book_ts
            self.update_count += 1

    def apply_delta(self, side: str, price: float, size: float) -> None:
        """Apply a single price level change."""
        with self._lock:
            target = self._bids if side.upper() in ("BUY", "BID") else self._asks
            if size <= 0:
                target.pop(price, None)
            else:
                target[price] = size
            self.last_update_ms = _now_ms()
            self.update_count += 1

    def apply_top_of_book(
        self,
        best_bid: Optional[float],
        best_ask: Optional[float],
        size: float = 1.0,
    ) -> None:
        """
        Stamp best bid/ask directly from a price_change item's best_bid/best_ask fields.
        Replaces the stored levels with just the reported best — stale inner levels
        are discarded since we only need top-of-book for the observation run.
        """
        with self._lock:
            if best_bid is not None:
                self._bids = {best_bid: size}
            if best_ask is not None:
                self._asks = {best_ask: size}
            self.last_update_ms = _now_ms()
            self.update_count += 1

    def snapshot(self, market_slug: Optional[str] = None) -> BookSnapshot:
        """Return a BookSnapshot capturing current state."""
        ts_local = _now_ms()
        with self._lock:
            best_bid, bid_size = _best_bid_ask(self._bids, is_bid=True)
            best_ask, ask_size = _best_bid_ask(self._asks, is_bid=False)
            last_upd = self.last_update_ms
            book_ts = self.book_timestamp

        spread_abs: Optional[float] = None
        spread_pct: Optional[float] = None
        if best_bid is not None and best_ask is not None:
            spread_abs = round(best_ask - best_bid, 6)
            mid = (best_bid + best_ask) / 2
            if mid > 0:
                spread_pct = round(spread_abs / mid * 100, 4)

        book_age_ms: Optional[int] = None
        if book_ts:
            book_age_ms = ts_local - book_ts
        elif last_upd:
            book_age_ms = ts_local - last_upd

        flags: List[str] = []
        if best_bid is None:
            flags.append("empty_bid")
        if best_ask is None:
            flags.append("empty_ask")
        if best_bid is not None and best_ask is not None and best_bid >= best_ask:
            flags.append("crossed")
        if last_upd is None or (ts_local - last_upd) > STALE_BOOK_THRESHOLD_MS:
            flags.append("stale")

        return BookSnapshot(
            ts_local=ts_local,
            token_id=self.token_id,
            market_slug=market_slug,
            side_label=self.side_label,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            spread_abs=spread_abs,
            spread_pct=spread_pct,
            book_timestamp=book_ts,
            book_age_ms=book_age_ms,
            book_state_flags=flags,
        )


# ---------------------------------------------------------------------------
# CLOB WebSocket client
# ---------------------------------------------------------------------------

class ClobWsClient:
    """
    Manages a persistent websocket connection to the Polymarket CLOB.

    Usage:
        client = ClobWsClient(token_ids=["abc", "def"], side_labels={"abc": "up", "def": "down"})
        client.start()   # non-blocking, runs in background thread
        ...
        snap = client.get_snapshot("abc")  # any time
        client.stop()
    """

    def __init__(
        self,
        token_ids: List[str],
        side_labels: Optional[Dict[str, str]] = None,
        market_slug: Optional[str] = None,
    ) -> None:
        self.token_ids = token_ids
        self.market_slug = market_slug
        self._side_labels = side_labels or {}

        # One state object per token
        self._states: Dict[str, _TokenBookState] = {
            tid: _TokenBookState(tid, self._side_labels.get(tid))
            for tid in token_ids
        }

        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._reconnect_count = 0
        self._last_message_ms: Optional[int] = None
        self._event_buffer: Deque[Dict[str, Any]] = deque(maxlen=EVENT_BUFFER_SIZE)
        self._lock = threading.Lock()
        self._pc_debug_count = 0  # counts price_change messages logged at debug level

    # -----------------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------------

    def start(self) -> None:
        """Start the WS client in a background daemon thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="clob_ws")
        self._thread.start()
        log.log_system_event(
            "clob_ws_start",
            detail=f"subscribing to {len(self.token_ids)} tokens",
            extra={"token_ids": self.token_ids},
        )

    def stop(self) -> None:
        """Signal the client to stop and wait for the thread."""
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        log.log_system_event("clob_ws_stop", detail="websocket client stopped")

    def get_snapshot(self, token_id: str) -> Optional[BookSnapshot]:
        """Return current top-of-book snapshot for a token. Thread-safe."""
        state = self._states.get(token_id)
        if state is None:
            return None
        return state.snapshot(self.market_slug)

    def get_all_snapshots(self) -> Dict[str, BookSnapshot]:
        """Return snapshots for all subscribed tokens."""
        return {tid: s.snapshot(self.market_slug) for tid, s in self._states.items()}

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def last_message_ms(self) -> Optional[int]:
        return self._last_message_ms

    def is_connected(self) -> bool:
        return self._ws is not None and not self._stop_event.is_set()

    def recent_events(self) -> List[Dict[str, Any]]:
        """Return last N raw events (copy) for debugging."""
        with self._lock:
            return list(self._event_buffer)

    # -----------------------------------------------------------------------
    # Internal WS callbacks
    # -----------------------------------------------------------------------

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        sub_msg = json.dumps({"assets_ids": self.token_ids, "type": "market"})
        ws.send(sub_msg)
        log.log_system_event(
            "clob_ws_connected",
            detail=f"sent subscription for {len(self.token_ids)} tokens",
        )

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        ts_local = _now_ms()
        self._last_message_ms = ts_local

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.log_parse_anomaly("clob_ws", "raw_message", raw[:200], f"JSON decode error: {exc}")
            return

        if not isinstance(msg, dict):
            # Valid JSON but not an object (e.g. a bare list or scalar) — skip silently
            return

        # Buffer for debugging
        with self._lock:
            self._event_buffer.append({"ts_local": ts_local, "msg": msg})

        # Route by event_type
        event_type = msg.get("event_type") or msg.get("type") or msg.get("event")

        if event_type == "book":
            self._handle_book(msg, ts_local)
        elif event_type == "price_change":
            self._handle_price_change(msg, ts_local)
        elif event_type in ("last_trade_price", "tick_size_change", "tick_size"):
            # Informational; no book state change needed
            pass
        elif msg.get("status") == "connected" or event_type == "connected":
            log.log_system_event("clob_ws_handshake", detail="received connected confirmation")
        else:
            log.log_parse_anomaly(
                "clob_ws", "event_type", event_type, f"unrecognised event type; raw={raw[:200]}"
            )

    def _handle_book(self, msg: Dict[str, Any], ts_local: int) -> None:
        token_id = msg.get("asset_id") or msg.get("token_id")
        if not token_id:
            log.log_parse_anomaly("clob_ws.book", "asset_id", msg, "missing asset_id")
            return

        state = self._states.get(str(token_id))
        if state is None:
            # Received book for unexpected token — log and ignore
            log.log_parse_anomaly("clob_ws.book", "asset_id", token_id, "received book for unknown token_id")
            return

        raw_bids = msg.get("buys") or msg.get("bids") or []
        raw_asks = msg.get("sells") or msg.get("asks") or []

        # Parse exchange book_timestamp if available
        book_ts: Optional[int] = None
        ts_raw = msg.get("timestamp")
        if ts_raw is not None:
            try:
                v = float(ts_raw)
                book_ts = int(v * 1000) if v < 1e12 else int(v)
            except (TypeError, ValueError):
                pass

        if not isinstance(raw_bids, list) or not isinstance(raw_asks, list):
            log.log_parse_anomaly("clob_ws.book", "bids/asks", msg, "not lists")
            return

        state.apply_snapshot(raw_bids, raw_asks, book_ts)

    def _handle_price_change(self, msg: Dict[str, Any], ts_local: int) -> None:
        # asset_id is NOT at the top level for price_change — it lives inside
        # each element of the price_changes list.
        price_changes = msg.get("price_changes")
        if not isinstance(price_changes, list):
            log.log_parse_anomaly(
                "clob_ws.price_change", "price_changes", price_changes, "expected list"
            )
            return

        # Debug log for the first few messages to confirm shape
        if self._pc_debug_count < _PRICE_CHANGE_DEBUG_LIMIT:
            asset_ids = [ch.get("asset_id") for ch in price_changes if isinstance(ch, dict)]
            log.streams.system.write({
                "ts_local": ts_local, "event": "price_change_debug",
                "event_type": "price_change",
                "changes_count": len(price_changes),
                "asset_ids": asset_ids,
            })
            self._pc_debug_count += 1

        for ch in price_changes:
            if not isinstance(ch, dict):
                continue

            asset_id = ch.get("asset_id")
            if not asset_id:
                log.log_parse_anomaly(
                    "clob_ws.price_change", "asset_id", ch,
                    "price_changes item missing asset_id"
                )
                continue

            state = self._states.get(str(asset_id))
            if state is None:
                continue  # not our token

            try:
                best_bid_raw = ch.get("best_bid")
                best_ask_raw = ch.get("best_ask")
                price_raw = ch.get("price")
                size_raw = ch.get("size")
                side = str(ch.get("side", ""))

                best_bid = float(best_bid_raw) if best_bid_raw is not None else None
                best_ask = float(best_ask_raw) if best_ask_raw is not None else None
                size = float(size_raw) if size_raw is not None else 1.0

                if best_bid is not None or best_ask is not None:
                    state.apply_top_of_book(best_bid, best_ask, size)
                elif price_raw is not None and side:
                    state.apply_delta(side, float(price_raw), size)
            except (TypeError, ValueError) as exc:
                log.log_parse_anomaly("clob_ws.price_change", "change_item", ch, str(exc))

    def _on_error(self, ws: websocket.WebSocketApp, error: Any) -> None:
        try:
            exc = error if isinstance(error, Exception) else Exception(str(error))
            log.log_exception("clob_ws.on_error", exc)
        except Exception:
            pass  # never raise from on_error

    def _on_close(self, ws: websocket.WebSocketApp, close_status_code: Any, close_msg: Any) -> None:
        log.log_system_event(
            "clob_ws_closed",
            detail=f"code={close_status_code} msg={close_msg}",
        )

    # -----------------------------------------------------------------------
    # Reconnect loop
    # -----------------------------------------------------------------------

    def _run_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            delay = RECONNECT_DELAYS_S[min(attempt, len(RECONNECT_DELAYS_S) - 1)]
            if attempt > 0:
                self._reconnect_count += 1
                log.log_reconnect("clob_ws", attempt, reason=f"reconnect after {delay}s back-off")
                if self._stop_event.wait(timeout=delay):
                    break

            try:
                ws = websocket.WebSocketApp(
                    CLOB_WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws = ws
                # run_forever blocks until connection drops
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                log.log_exception("clob_ws.run_forever", exc, {"attempt": attempt})

            attempt += 1

        log.log_system_event("clob_ws_loop_exit", detail="stop event set; exiting reconnect loop")

    # -----------------------------------------------------------------------
    # Liveness monitoring (call from main loop)
    # -----------------------------------------------------------------------

    def check_liveness(self) -> None:
        """
        Check for heartbeat miss. Call periodically from main thread (e.g. every 5s).
        On miss, closes the socket so _run_loop's reconnect path picks it back up.
        """
        ts_local = _now_ms()
        if self._last_message_ms is None:
            return
        gap = ts_local - self._last_message_ms
        if gap > HEARTBEAT_MISS_THRESHOLD_MS:
            log.log_heartbeat_miss("clob_ws", self._last_message_ms, HEARTBEAT_MISS_THRESHOLD_MS)
            log.log_system_event(
                "clob_ws_heartbeat_restart",
                detail=f"gap_ms={gap} > {HEARTBEAT_MISS_THRESHOLD_MS}; closing socket to trigger reconnect",
                extra={
                    "gap_ms": gap,
                    "threshold_ms": HEARTBEAT_MISS_THRESHOLD_MS,
                    "token_ids": self.token_ids,
                    "market_slug": self.market_slug,
                },
            )
            # Reset before close so repeated check_liveness calls don't pile on
            self._last_message_ms = None
            ws = self._ws
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
