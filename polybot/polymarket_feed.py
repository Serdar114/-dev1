"""
Polymarket CLOB WebSocket feed — real-time orderbook stream.

wss://ws-subscriptions-clob.polymarket.com/ws/market
Subscribe: {"assets_ids": ["token_id1", "token_id2"], "type": "Market"}

Events:
  "book"         → tam orderbook snapshot (bids/asks listesi)
  "price_change" → incremental update (changes listesi)

Fallback: WS kopunca 3s'de bir HTTP /book polling. WS reconnect olunca durur.

Sakladığı veri (per token_id):
  BookSnapshot: bid, ask, mid, spread, spread_pct, bid_size, ask_size, ts
"""

import asyncio
import json
import time
import websockets
import aiohttp
import logger as log_module
from dataclasses import dataclass


PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_BASE = "https://clob.polymarket.com"
RECONNECT_DELAY = 2.0
FALLBACK_POLL_INTERVAL = 3.0


@dataclass
class BookSnapshot:
    token_id: str
    bid: float        # best bid (en yüksek alış)
    ask: float        # best ask (en düşük satış)
    mid: float        # (bid + ask) / 2
    spread: float     # ask - bid
    spread_pct: float # spread / mid * 100
    bid_size: float   # top-of-book bid depth (share adedi)
    ask_size: float   # top-of-book ask depth (share adedi)
    ts: float         # unix timestamp


class PolymarketFeed:
    def __init__(self, ws_url: str = PM_WS_URL, clob_base: str = CLOB_BASE):
        self.ws_url = ws_url
        self.clob_base = clob_base
        self._books: dict[str, BookSnapshot] = {}
        self._subscribed_ids: list[str] = []
        self._running = False
        self._ws_connected = False
        self._task: asyncio.Task | None = None
        self._fallback_task: asyncio.Task | None = None
        self._ws = None

    def get_book(self, token_id: str) -> BookSnapshot | None:
        return self._books.get(token_id)

    def subscribe_tokens(self, token_ids: list[str]) -> None:
        self._subscribed_ids = list(token_ids)

    def _update_book(
        self, token_id: str,
        bid: float, ask: float,
        bid_size: float = 0.0, ask_size: float = 0.0,
    ) -> None:
        if bid <= 0 or ask <= 0 or ask <= bid:
            return
        mid = (bid + ask) / 2.0
        spread = ask - bid
        spread_pct = spread / mid * 100.0 if mid > 0 else 0.0
        self._books[token_id] = BookSnapshot(
            token_id=token_id,
            bid=round(bid, 4),
            ask=round(ask, 4),
            mid=round(mid, 4),
            spread=round(spread, 4),
            spread_pct=round(spread_pct, 2),
            bid_size=round(bid_size, 2),
            ask_size=round(ask_size, 2),
            ts=time.time(),
        )

    def _handle_book_event(self, msg: dict) -> None:
        """Full snapshot: bids/asks listesiyle best bid/ask ve depth hesapla."""
        token_id = msg.get("asset_id", "")
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])

        best_bid = best_bid_size = 0.0
        best_ask = best_ask_size = 0.0

        try:
            valid_bids = [
                (float(b["price"]), float(b.get("size", 0)))
                for b in bids if float(b.get("size", 0)) > 0
            ]
            if valid_bids:
                best_bid, best_bid_size = max(valid_bids, key=lambda x: x[0])
        except (ValueError, KeyError):
            pass

        try:
            valid_asks = [
                (float(a["price"]), float(a.get("size", 0)))
                for a in asks if float(a.get("size", 0)) > 0
            ]
            if valid_asks:
                best_ask, best_ask_size = min(valid_asks, key=lambda x: x[0])
        except (ValueError, KeyError):
            pass

        if best_bid > 0 and best_ask > best_bid:
            self._update_book(token_id, best_bid, best_ask, best_bid_size, best_ask_size)

    def _handle_price_change(self, msg: dict) -> None:
        """Incremental update — mevcut snapshot üzerinden best price güncelle."""
        token_id = msg.get("asset_id", "")
        changes = msg.get("changes", [])

        book = self._books.get(token_id)
        bid = book.bid if book else 0.0
        ask = book.ask if book else 1.0
        bid_size = book.bid_size if book else 0.0
        ask_size = book.ask_size if book else 0.0

        for change in changes:
            try:
                price = float(change["price"])
                size = float(change.get("size", 0))
                side = change.get("side", "").upper()

                if side == "BUY":
                    if size > 0 and price > bid:
                        bid, bid_size = price, size
                    elif size == 0 and price == bid:
                        bid, bid_size = 0.0, 0.0  # en iyi bid gitti
                elif side == "SELL":
                    if size > 0 and price < ask:
                        ask, ask_size = price, size
                    elif size == 0 and price == ask:
                        ask, ask_size = 1.0, 0.0  # en iyi ask gitti
            except (ValueError, KeyError):
                pass

        if bid > 0 and ask > bid:
            self._update_book(token_id, bid, ask, bid_size, ask_size)

    async def _send_subscribe(self, ws) -> None:
        if not self._subscribed_ids:
            return
        try:
            msg = json.dumps({"assets_ids": self._subscribed_ids, "type": "Market"})
            await ws.send(msg)
            await log_module.log("pm_ws_subscribed", {"tokens": self._subscribed_ids})
        except Exception as e:
            await log_module.log("pm_ws_subscribe_error", {"error": str(e)})

    async def _fallback_poll(self) -> None:
        """WS bağlı değilken HTTP /book polling — 3s interval, WS gelince durur."""
        while self._running:
            try:
                if not self._ws_connected and self._subscribed_ids:
                    async with aiohttp.ClientSession() as session:
                        for token_id in self._subscribed_ids:
                            try:
                                async with session.get(
                                    f"{self.clob_base}/book",
                                    params={"token_id": token_id},
                                    timeout=aiohttp.ClientTimeout(total=5),
                                ) as r:
                                    if r.status == 200:
                                        data = await r.json()
                                        self._handle_book_event({
                                            "asset_id": token_id,
                                            "bids": data.get("bids", []),
                                            "asks": data.get("asks", []),
                                        })
                            except Exception:
                                pass
            except Exception as e:
                await log_module.log("pm_fallback_error", {"error": str(e)})
            await asyncio.sleep(FALLBACK_POLL_INTERVAL)

    async def _run_once(self) -> None:
        try:
            async with websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                self._ws = ws
                self._ws_connected = True
                await log_module.log("pm_ws_connected", {"url": self.ws_url})
                await self._send_subscribe(ws)

                async for raw in ws:
                    if not self._running:
                        break
                    try:
                        msg = json.loads(raw)
                        events = msg if isinstance(msg, list) else [msg]
                        for event in events:
                            etype = event.get("event_type", "")
                            if etype == "book":
                                self._handle_book_event(event)
                            elif etype == "price_change":
                                self._handle_price_change(event)
                    except (json.JSONDecodeError, TypeError):
                        pass
        except Exception as e:
            await log_module.log("pm_ws_error", {"error": str(e)})
        finally:
            self._ws = None
            self._ws_connected = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self._run_once()
            if self._running:
                await log_module.log("pm_ws_reconnect", {"delay": RECONNECT_DELAY})
                await asyncio.sleep(RECONNECT_DELAY)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run())
        self._fallback_task = asyncio.create_task(self._fallback_poll())
        return self._task

    async def resubscribe(self, token_ids: list[str]) -> None:
        """Yeni pencere başladığında yeni token'lara subscribe ol."""
        self._subscribed_ids = token_ids
        self._books.clear()
        if self._ws is not None:
            try:
                msg = json.dumps({"assets_ids": token_ids, "type": "Market"})
                await self._ws.send(msg)
                await log_module.log("pm_ws_resubscribed", {"tokens": token_ids})
            except Exception as e:
                await log_module.log("pm_ws_resub_error", {"error": str(e)})

    async def wait_for_book(self, token_id: str, timeout: float = 8.0) -> BookSnapshot | None:
        """İlk orderbook snapshot'ı bekle (WS veya fallback HTTP'den)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            book = self._books.get(token_id)
            if book:
                return book
            await asyncio.sleep(0.1)
        return None

    async def stop(self) -> None:
        self._running = False
        for task in [self._task, self._fallback_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
