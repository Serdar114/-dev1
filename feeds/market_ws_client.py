"""
feeds/market_ws_client.py — Polymarket CLOB market WebSocket client.

Subscribes to orderbook updates for Up and Down token IDs.
Handles:
  - Initial book snapshot (event_type: "book")
  - Incremental price changes (event_type: "price_change")

Subscription is restarted when token IDs change (market rollover).

Note: message format observed in Polymarket CLOB WS is an array of events.
      Handles both list and dict top-level messages defensively.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import List, Optional

from loggingx.schemas import (
    MarketWsConnectedEvent,
    MarketWsDisconnectedEvent,
    OrderbookSnapshotEvent,
)
from state import OrderbookSide

log = logging.getLogger(__name__)


class MarketWsClient:
    """
    Connects to Polymarket market WebSocket and maintains two OrderbookSide objects
    (one for Up token, one for Down token).

    Call .subscribe(up_token_id, down_token_id) to start/restart subscription.
    Call .stop() to cleanly disconnect.
    """

    def __init__(self, config: dict, event_logger=None) -> None:
        self._ws_url: str = config["polymarket"]["market_ws_url"]
        self._logger = event_logger

        self._lock = threading.Lock()
        self._up_book = OrderbookSide(outcome="Up")
        self._down_book = OrderbookSide(outcome="Down")
        self._up_token_id: str = ""
        self._down_token_id: str = ""

        self._stop_event = threading.Event()
        self._restart_event = threading.Event()
        self._ws = None
        self._thread: Optional[threading.Thread] = None

    def _log(self, event) -> None:
        if self._logger:
            self._logger.log(event)

    def subscribe(self, up_token_id: str, down_token_id: str) -> None:
        """
        Set new token IDs and restart the WebSocket connection.
        Safe to call from any thread.
        """
        with self._lock:
            self._up_token_id = up_token_id
            self._down_token_id = down_token_id
            # Reset books
            self._up_book = OrderbookSide(outcome="Up", token_id=up_token_id)
            self._down_book = OrderbookSide(outcome="Down", token_id=down_token_id)

        # Signal restart
        self._restart_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

        # Start thread if not running
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop, daemon=True, name="market-ws"
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def get_books(self):
        """Return (up_book_copy, down_book_copy) — shallow copies for display."""
        with self._lock:
            import copy
            return copy.copy(self._up_book), copy.copy(self._down_book)

    def inject_state(self, state) -> None:
        """Write current orderbooks into SystemState.
        Only overwrites a side if the WS has confirmed a snapshot.
        This preserves the REST seed until WS takes over.
        """
        with self._lock:
            import copy
            up = copy.copy(self._up_book)
            dn = copy.copy(self._down_book)
        with state._lock:
            if up.snapshot_received:
                state.up_book = up
            if dn.snapshot_received:
                state.down_book = dn

    # -----------------------------------------------------------------------
    # Internal WebSocket machinery
    # -----------------------------------------------------------------------

    def _build_sub_msg(self, up_id: str, down_id: str) -> str:
        return json.dumps({
            "assets_ids": [up_id, down_id],
            "type": "Market",
        })

    def _on_open(self, ws) -> None:
        with self._lock:
            up_id = self._up_token_id
            down_id = self._down_token_id
        if up_id and down_id:
            msg = self._build_sub_msg(up_id, down_id)
            ws.send(msg)
            log.info("Market WS subscribed: up=%s down=%s", up_id[:12], down_id[:12])
            self._log(MarketWsConnectedEvent(
                up_token_id=up_id,
                down_token_id=down_id,
            ))

    def _on_message(self, ws, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.debug("Market WS bad JSON: %s", exc)
            return

        events = data if isinstance(data, list) else [data]
        for event in events:
            self._handle_event(event)

    def _handle_event(self, event: dict) -> None:
        etype = event.get("event_type", "")
        asset_id = event.get("asset_id", "")

        with self._lock:
            up_id = self._up_token_id
            down_id = self._down_token_id
            if asset_id == up_id:
                book = self._up_book
            elif asset_id == down_id:
                book = self._down_book
            else:
                return  # unknown asset

            if etype == "book":
                # Support both Polymarket WS field variants:
                #   "buys"/"sells"  (some WS versions)
                #   "bids"/"asks"   (CLOB WS documented format)
                buys = event.get("bids") or event.get("buys", [])
                sells = event.get("asks") or event.get("sells", [])
                # Regression guard: a snapshot with 0 asks while we already hold a
                # valid ask side is structurally suspicious (malformed or half-populated
                # WS event). Preserve the current book rather than wipe it.
                if book.snapshot_received and len(book.asks) > 0 and len(sells) == 0:
                    log.warning(
                        "book_snapshot_rejected side=%s incoming_asks=0 current_asks=%d reason=suspicious_empty",
                        book.outcome, len(book.asks),
                    )
                    return
                is_first = not book.snapshot_received
                prev_asks = len(book.asks)
                book.apply_snapshot(buys, sells)
                side_label = "up" if asset_id == up_id else "dn"
                if is_first:
                    log.info(
                        "market_ws %s_ws_snapshot(first) asks=%d bids=%d",
                        side_label, len(book.asks), len(book.bids),
                    )
                if prev_asks > 0 and len(book.asks) == 0:
                    log.warning(
                        "book_regressed side=%s from asks=%d to asks=0 reason=snapshot",
                        book.outcome, prev_asks,
                    )
                ba = book.best_ask()
                bb = book.best_bid()
                self._log(OrderbookSnapshotEvent(
                    token_id=asset_id,
                    outcome=book.outcome,
                    best_bid=bb[0] if bb else None,
                    best_ask=ba[0] if ba else None,
                    bid_levels=len(book.bids),
                    ask_levels=len(book.asks),
                ))

            elif etype == "price_change":
                changes = event.get("changes", [])
                prev_asks = len(book.asks)
                book.apply_delta(changes)
                if prev_asks > 0 and len(book.asks) == 0:
                    log.warning(
                        "book_regressed side=%s from asks=%d to asks=0 reason=delta",
                        book.outcome, prev_asks,
                    )

    def _on_error(self, ws, error) -> None:
        log.warning("Market WS error: %s", error)

    def _on_close(self, ws, code, msg) -> None:
        log.info("Market WS closed: code=%s msg=%s", code, msg)
        self._log(MarketWsDisconnectedEvent(error=f"code={code}"))

    def _run_loop(self) -> None:
        import websocket

        while not self._stop_event.is_set():
            self._restart_event.clear()
            with self._lock:
                up_id = self._up_token_id
                down_id = self._down_token_id
            if not up_id or not down_id:
                self._stop_event.wait(timeout=2)
                continue
            try:
                self._ws = websocket.WebSocketApp(
                    self._ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=8)
            except Exception as exc:
                log.error("Market WS run_forever exception: %s", exc)
            if not self._stop_event.is_set() and not self._restart_event.is_set():
                log.info("Market WS reconnecting in 5s...")
                self._stop_event.wait(timeout=5)
