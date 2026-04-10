"""
clob_ws_client.py — Polymarket CLOB WebSocket for order book data.

Design:
  Connects to Polymarket's CLOB WebSocket (NOT RTDS — separate service).
  Subscribes to "market" channel for all active token IDs.
  Maintains per-token order book snapshots.
  On reconnect, re-subscribes to all known token IDs.

  WebSocket URL: wss://ws-subscriptions-clob.polymarket.com/ws/market
  Subscribe message:
    {"assets_ids": ["token_id_1", ...], "type": "Market"}
  Messages received:
    book update:  {"event_type": "book", "asset_id": "...", "bids": [...], "asks": [...]}
    price update: {"event_type": "price_change", ...}
    last_trade:   {"event_type": "last_trade_price", ...}

  This is distinct from RTDS (which carries Chainlink and Binance price feeds).
  CLOB WS carries order book state only.
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Dict, List, Optional, Set, Callable, Awaitable

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

from loggingx.schemas import OrderBookSnapshot, PriceLevel

logger = logging.getLogger("polybot.clob_ws")

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class CLOBWSClient:
    """
    Polymarket CLOB WebSocket client for order book feeds.

    Job: Maintain live order book snapshots for tracked token IDs.
    Input: Set of token IDs to subscribe to (updated dynamically).
    Output: OrderBookSnapshot per token_id (via get_book())
    Failure:
      - Disconnect: reconnect with delay, re-subscribe
      - Parse error: skip message, log
      - Missing websockets: log error, no books available
    """

    def __init__(
        self,
        ws_url: str = CLOB_WS_URL,
        reconnect_delay_secs: float = 5.0,
        on_book_update: Optional[Callable[[str, OrderBookSnapshot], Awaitable[None]]] = None,
    ):
        self._ws_url = ws_url
        self._reconnect_delay = reconnect_delay_secs
        self._on_book_update = on_book_update

        self._books: Dict[str, OrderBookSnapshot] = {}
        # _all_known_ids: superset of every token ID ever requested — persists across reconnects
        # _conn_subscribed: what the CURRENT connection has subscribed — cleared on disconnect
        # _connected: True only after the initial subscribe is sent on the current connection
        self._all_known_ids: Set[str] = set()
        self._conn_subscribed: Set[str] = set()
        self._connected = False
        self._ws = None
        self._running = False

        if not WEBSOCKETS_AVAILABLE:
            logger.error("websockets not installed — Polymarket CLOB book feed UNAVAILABLE")

    async def start(self) -> None:
        if not WEBSOCKETS_AVAILABLE:
            return
        self._running = True
        logger.info("CLOB WS starting: %s", self._ws_url)
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "CLOB WS disconnected: %s — reconnecting in %.1fs",
                    exc, self._reconnect_delay,
                )
                self._ws = None
                self._connected = False
                self._conn_subscribed.clear()   # connection-local state only; _all_known_ids kept
                await asyncio.sleep(self._reconnect_delay)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def subscribe(self, token_ids: Set[str]) -> None:
        """
        Subscribe to order book updates for a set of token IDs.

        New IDs are added to _all_known_ids (persists across reconnects).
        If currently connected, only the truly new IDs are sent to the server.
        If not connected, IDs are queued in _all_known_ids and sent on next connect.
        Never sends duplicate subscribe for IDs already subscribed on this connection.
        """
        new_ids = token_ids - self._all_known_ids
        self._all_known_ids.update(token_ids)
        if not new_ids:
            return  # nothing new; no send needed

        if self._connected:
            unsent = new_ids - self._conn_subscribed
            if unsent:
                await self._send_subscribe(list(unsent))
                self._conn_subscribed.update(unsent)
                logger.info("CLOB WS subscribed %d new token(s): %s", len(unsent), [t[:12] for t in unsent])

    async def _connect_and_listen(self) -> None:
        logger.info("CLOB_WS_CONNECT url=%s", self._ws_url)
        try:
            async with websockets.connect(
                self._ws_url,
                ping_interval=30,
                ping_timeout=15,
            ) as ws:
                self._ws = ws
                logger.info("CLOB_WS_CONNECTED url=%s", self._ws_url)

                # Subscribe all known IDs on this fresh connection.
                # _connected is set AFTER send to prevent concurrent subscribe()
                # calls from racing and sending duplicate payloads.
                if self._all_known_ids:
                    await self._send_subscribe(list(self._all_known_ids))
                    self._conn_subscribed = self._all_known_ids.copy()
                self._connected = True

                async for raw_msg in ws:
                    if not self._running:
                        break
                    await self._handle_message(raw_msg)

        except Exception as exc:
            logger.warning("CLOB_WS_CONNECT_FAILED url=%s error=%s", self._ws_url, exc)
            raise
        finally:
            self._connected = False
            self._conn_subscribed.clear()

    async def _send_subscribe(self, token_ids: List[str]) -> None:
        if self._ws is None:
            return
        payload = {"assets_ids": token_ids, "type": "Market"}
        msg = json.dumps(payload)
        logger.info(
            "CLOB_WS_SUBSCRIBE url=%s payload=%s",
            self._ws_url, msg,
        )
        try:
            await self._ws.send(msg)
            logger.info(
                "CLOB_WS_SUBSCRIBED token_count=%d tokens=%s",
                len(token_ids), [t[:12] for t in token_ids],
            )
        except Exception as exc:
            logger.warning(
                "CLOB_WS_SUBSCRIBE_FAILED url=%s payload=%s error=%s",
                self._ws_url, msg, exc,
            )
            raise

    async def _handle_message(self, raw: str) -> None:
        # Non-JSON control frames — "INVALID OPERATION" is sent by the server
        # on bad subscribe payloads or during reconnect; log once and continue.
        stripped = raw.strip() if raw else ""
        if not stripped:
            return
        if stripped == "INVALID OPERATION":
            logger.warning("CLOB_WS_INVALID_OPERATION received — check subscribe payload")
            return

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.warning("CLOB_WS_JSON_ERROR: %s | raw=%r", exc, raw[:100])
            return

        try:
            # First message (and some others) can arrive as a JSON list
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        await self._dispatch(item)
            elif isinstance(parsed, dict):
                await self._dispatch(parsed)
            else:
                logger.debug("CLOB_WS_UNKNOWN_SHAPE type=%s", type(parsed).__name__)
        except Exception as exc:
            logger.warning("CLOB_WS_HANDLER_ERROR: %s", exc)

    async def _dispatch(self, msg: dict) -> None:
        """Route a single parsed dict message to the correct handler."""
        event_type = msg.get("event_type", "")
        if event_type == "book":
            await self._handle_book(msg)
        # price_change and last_trade_price are informational; not needed here

    async def _handle_book(self, msg: dict) -> None:
        token_id = msg.get("asset_id") or msg.get("market")
        if not token_id:
            logger.debug("Book message missing asset_id")
            return

        bids = self._parse_levels(msg.get("bids", []), ascending=False)
        asks = self._parse_levels(msg.get("asks", []), ascending=True)

        book = OrderBookSnapshot(
            token_id=token_id,
            bids=bids,
            asks=asks,
            updated_at=time.time(),
        )
        self._books[token_id] = book

        if self._on_book_update:
            await self._on_book_update(token_id, book)

    def _parse_levels(self, raw_levels: list, ascending: bool) -> List[PriceLevel]:
        levels: List[PriceLevel] = []
        for raw in raw_levels:
            try:
                if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                    price, size = float(raw[0]), float(raw[1])
                elif isinstance(raw, dict):
                    price = float(raw.get("price", raw.get("p", 0)))
                    size  = float(raw.get("size",  raw.get("s", 0)))
                else:
                    continue
                if size > 0:
                    levels.append(PriceLevel(price=price, size=size))
            except (ValueError, TypeError):
                continue

        levels.sort(key=lambda lvl: lvl.price, reverse=not ascending)
        return levels

    def get_book(self, token_id: str) -> Optional[OrderBookSnapshot]:
        return self._books.get(token_id)

    def all_books(self) -> Dict[str, OrderBookSnapshot]:
        return dict(self._books)

    def book_age_secs(self, token_id: str) -> Optional[float]:
        book = self._books.get(token_id)
        if book:
            return time.time() - book.updated_at
        return None
