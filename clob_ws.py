"""
Polymarket CLOB WebSocket client.

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
Subscribe: {"assets_ids": [...], "type": "market"}

Handles two event types:
  "book"         — full order book snapshot, replaces local state
  "price_change" — incremental update; size=0 means remove that level

set_subscriptions(token_ids) triggers a reconnect when the token set changes.
get_snapshot(token_id) returns the latest BookSnapshot or None.
Reconnects with exponential backoff.
"""
import json
import threading
import time
from typing import Callable, Dict, Optional, Set

import websocket

from schemas import BookSnapshot

CLOB_WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BACKOFF_SEQUENCE = [1, 2, 5, 10, 30, 60]


def _book_metrics(
    bids: Dict[str, float], asks: Dict[str, float]
) -> tuple:
    """Return (best_bid, best_ask, mid, spread) from price→size dicts."""
    live_bids = [float(p) for p, s in bids.items() if s > 0]
    live_asks = [float(p) for p, s in asks.items() if s > 0]

    best_bid = max(live_bids) if live_bids else None
    best_ask = min(live_asks) if live_asks else None

    mid: Optional[float] = None
    spread: Optional[float] = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid

    return best_bid, best_ask, mid, spread


class CLOBWebSocket:
    def __init__(
        self,
        on_event: Optional[Callable[[str, dict], None]] = None,
        shutdown_event: Optional[threading.Event] = None,
    ):
        self._lock = threading.Lock()
        self._on_event = on_event
        self._shutdown = shutdown_event or threading.Event()

        # token_id → {bids: {price_str: size_float}, asks: {...}}
        self._books: Dict[str, Dict[str, Dict[str, float]]] = {}
        # token_id → latest BookSnapshot
        self._snapshots: Dict[str, BookSnapshot] = {}

        self._desired_tokens: Set[str] = set()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._connected = False
        self._backoff_index = 0

    def start(self) -> None:
        t = threading.Thread(
            target=self._run_loop, daemon=True, name="clob-ws-run-loop"
        )
        t.start()

    def set_subscriptions(self, token_ids: Set[str]) -> None:
        """Update the desired subscription set; reconnects if changed."""
        with self._lock:
            if token_ids == self._desired_tokens:
                return
            self._desired_tokens = set(token_ids)
            ws = self._ws
            connected = self._connected

        if connected and ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def get_snapshot(self, token_id: str) -> Optional[BookSnapshot]:
        with self._lock:
            return self._snapshots.get(token_id)

    def stop(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Internal run loop                                                    #
    # ------------------------------------------------------------------ #

    def _run_loop(self) -> None:
        while not self._shutdown.is_set():
            with self._lock:
                tokens = set(self._desired_tokens)

            if not tokens:
                self._shutdown.wait(2)
                continue

            try:
                self._connect(tokens)
            except Exception as exc:
                self._emit("error", {"component": "clob_ws", "message": str(exc)})

            if self._shutdown.is_set():
                break

            backoff = BACKOFF_SEQUENCE[
                min(self._backoff_index, len(BACKOFF_SEQUENCE) - 1)
            ]
            self._backoff_index = min(
                self._backoff_index + 1, len(BACKOFF_SEQUENCE) - 1
            )
            self._emit(
                "reconnect_scheduled",
                {"component": "clob_ws", "backoff_s": backoff},
            )
            self._shutdown.wait(backoff)

    def _connect(self, token_ids: Set[str]) -> None:
        def on_open(ws: websocket.WebSocketApp) -> None:
            with self._lock:
                self._connected = True
                self._backoff_index = 0
                self._books.clear()

            subscribe_msg = json.dumps(
                {"assets_ids": list(token_ids), "type": "market"}
            )
            ws.send(subscribe_msg)
            self._emit("clob_ws_connected", {"tokens": list(token_ids)})

        def on_message(ws: websocket.WebSocketApp, message: str) -> None:
            try:
                events = json.loads(message)
                if isinstance(events, dict):
                    events = [events]
                elif not isinstance(events, list):
                    return
                for event in events:
                    if isinstance(event, dict):
                        self._handle_event(event)
            except Exception as exc:
                self._emit(
                    "error",
                    {"component": "clob_ws", "message": f"parse error: {exc}"},
                )

        def on_error(ws: websocket.WebSocketApp, error: Exception) -> None:
            self._emit(
                "ws_error", {"component": "clob_ws", "error": str(error)}
            )

        def on_close(
            ws: websocket.WebSocketApp,
            close_status_code: Optional[int],
            close_msg: Optional[str],
        ) -> None:
            with self._lock:
                self._connected = False
            self._emit(
                "clob_ws_disconnected",
                {"status_code": close_status_code, "message": close_msg},
            )

        self._ws = websocket.WebSocketApp(
            CLOB_WS_ENDPOINT,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws.run_forever()

    # ------------------------------------------------------------------ #
    # Event handling                                                       #
    # ------------------------------------------------------------------ #

    def _handle_event(self, event: dict) -> None:
        event_type = event.get("event_type")
        token_id = event.get("asset_id")
        if not token_id:
            return

        now_ms = int(time.time() * 1000)

        if event_type == "book":
            bids: Dict[str, float] = {}
            asks: Dict[str, float] = {}
            for entry in event.get("bids", []):
                p, s = entry.get("price"), entry.get("size")
                if p is not None and s is not None:
                    bids[str(p)] = float(s)
            for entry in event.get("asks", []):
                p, s = entry.get("price"), entry.get("size")
                if p is not None and s is not None:
                    asks[str(p)] = float(s)

            best_bid, best_ask, mid, spread = _book_metrics(bids, asks)
            with self._lock:
                self._books[token_id] = {"bids": bids, "asks": asks}
                self._snapshots[token_id] = BookSnapshot(
                    ts_local=now_ms,
                    token_id=token_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    mid=mid,
                    spread=spread,
                    source="ws",
                )

        elif event_type == "price_change":
            with self._lock:
                if token_id not in self._books:
                    self._books[token_id] = {"bids": {}, "asks": {}}
                book = self._books[token_id]

                for change in event.get("changes", []):
                    side = str(change.get("side", "")).upper()
                    price = change.get("price")
                    size = change.get("size")
                    if price is None or size is None:
                        continue

                    price_key = str(price)
                    size_val = float(size)

                    if side == "BUY":
                        if size_val == 0:
                            book["bids"].pop(price_key, None)
                        else:
                            book["bids"][price_key] = size_val
                    elif side == "SELL":
                        if size_val == 0:
                            book["asks"].pop(price_key, None)
                        else:
                            book["asks"][price_key] = size_val

                best_bid, best_ask, mid, spread = _book_metrics(
                    book["bids"], book["asks"]
                )
                self._snapshots[token_id] = BookSnapshot(
                    ts_local=now_ms,
                    token_id=token_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    mid=mid,
                    spread=spread,
                    source="ws",
                )

    def _emit(self, event_type: str, data: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event_type, data)
            except Exception:
                pass
