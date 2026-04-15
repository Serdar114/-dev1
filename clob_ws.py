"""
clob_ws.py — Polymarket CLOB WebSocket book-diff listener.

Maintains best bid/ask state for a subscribed set of token IDs.
Reconnects with exponential backoff on failure.
Thread-safe: book state is protected by an internal RLock.

DO NOT MODIFY this file as part of the observation enrichment patch.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Dict, List, Optional, Set

import websocket  # websocket-client

from schemas import BookSnapshot

log = logging.getLogger(__name__)

CLOB_WS_URL = "wss://ws-clob.polymarket.com"

_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 60.0
_BACKOFF_FACTOR = 2.0


class CLOBWSClient:
    """
    WebSocket client for Polymarket CLOB order book diffs.

    Usage:
        client = CLOBWSClient(shutdown_event)
        client.set_subscriptions(["token_id_1", "token_id_2"])
        client.start()
        snap = client.get_snapshot("token_id_1")   # None until first message
    """

    def __init__(self, shutdown_event: threading.Event) -> None:
        self._shutdown = shutdown_event
        self._lock = threading.RLock()
        self._subscriptions: Set[str] = set()
        # {token_id: {"bids": {price: size}, "asks": {price: size}}}
        self._raw_books: Dict[str, dict] = {}
        self._snapshots: Dict[str, BookSnapshot] = {}
        self._reconnect_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── public ────────────────────────────────────────────────────────────────

    def set_subscriptions(self, token_ids: List[str]) -> None:
        """Replace the subscribed token set. Triggers reconnect if changed."""
        with self._lock:
            new_set = set(token_ids)
            if new_set == self._subscriptions:
                return
            self._subscriptions = new_set
            self._reconnect_flag.set()
            log.info("clob_ws: subscriptions updated (%d tokens), reconnect scheduled", len(new_set))

    def get_snapshot(self, token_id: str) -> Optional[BookSnapshot]:
        with self._lock:
            return self._snapshots.get(token_id)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, name="clob_ws", daemon=True
        )
        self._thread.start()

    # ── internal loop ─────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        backoff = _BACKOFF_BASE
        while not self._shutdown.is_set():
            self._reconnect_flag.clear()
            with self._lock:
                subs = list(self._subscriptions)

            if not subs:
                log.debug("clob_ws: no subscriptions, idle")
                self._shutdown.wait(5.0)
                continue

            try:
                self._connect_and_run(subs)
                backoff = _BACKOFF_BASE  # clean exit → reset backoff
            except Exception as exc:
                log.warning("clob_ws: error: %s", exc)

            if self._shutdown.is_set():
                break

            wait = min(backoff, _BACKOFF_MAX)
            log.info("clob_ws: reconnect in %.1fs (backoff)", wait)
            self._shutdown.wait(wait)
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)

    def _connect_and_run(self, token_ids: List[str]) -> None:
        log.info("clob_ws: connecting, %d tokens", len(token_ids))
        ws_closed = threading.Event()

        def on_open(ws):
            msg = json.dumps({"assets_ids": token_ids, "type": "Market"})
            ws.send(msg)
            log.info("clob_ws: subscribed to %d tokens", len(token_ids))

        def on_message(ws, message):
            try:
                self._handle_message(json.loads(message))
            except Exception as exc:
                log.debug("clob_ws: message parse error: %s", exc)

        def on_error(ws, error):
            log.warning("clob_ws: ws error: %s", error)

        def on_close(ws, code, msg):
            log.info("clob_ws: closed (code=%s)", code)
            ws_closed.set()

        ws = websocket.WebSocketApp(
            CLOB_WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        runner = threading.Thread(
            target=ws.run_forever,
            kwargs={"ping_interval": 30, "ping_timeout": 10},
            daemon=True,
        )
        runner.start()

        # Block until: WS closed, shutdown, or subscription change
        while not self._shutdown.is_set() and not ws_closed.is_set():
            if self._reconnect_flag.is_set():
                log.info("clob_ws: subscription change — closing WS")
                ws.close()
                break
            time.sleep(0.5)

        ws.close()
        runner.join(timeout=5.0)

    # ── message handler ───────────────────────────────────────────────────────

    def _handle_message(self, data: dict) -> None:
        event_type = data.get("event_type")
        asset_id = data.get("asset_id")
        if not asset_id:
            return

        ts_local = int(time.time() * 1000)

        with self._lock:
            if asset_id not in self._raw_books:
                self._raw_books[asset_id] = {"bids": {}, "asks": {}}
            book = self._raw_books[asset_id]

            if event_type == "book":
                # Full snapshot — replace both sides
                book["bids"] = {}
                book["asks"] = {}
                for level in data.get("bids") or []:
                    try:
                        p, s = float(level["price"]), float(level["size"])
                        if s > 0:
                            book["bids"][p] = s
                    except (KeyError, TypeError, ValueError):
                        pass
                for level in data.get("asks") or []:
                    try:
                        p, s = float(level["price"]), float(level["size"])
                        if s > 0:
                            book["asks"][p] = s
                    except (KeyError, TypeError, ValueError):
                        pass

            elif event_type == "price_change":
                for change in data.get("changes") or []:
                    try:
                        side = (change.get("side") or "").upper()
                        p = float(change["price"])
                        s = float(change["size"])
                        target = book["bids"] if side == "BUY" else book["asks"]
                        if s == 0:
                            target.pop(p, None)
                        else:
                            target[p] = s
                    except (KeyError, TypeError, ValueError):
                        pass
            else:
                return  # ignore unknown event types

            # Rebuild best-bid/ask snapshot
            best_bid = max(book["bids"]) if book["bids"] else None
            best_ask = min(book["asks"]) if book["asks"] else None
            mid = ((best_bid + best_ask) / 2.0) if (best_bid is not None and best_ask is not None) else None
            spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None

            self._snapshots[asset_id] = BookSnapshot(
                ts_local=ts_local,
                token_id=asset_id,
                best_bid=best_bid,
                best_ask=best_ask,
                mid=mid,
                spread=spread,
                source="ws",
            )
