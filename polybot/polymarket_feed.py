"""
Polymarket CLOB WebSocket feed — real-time orderbook stream.

wss://ws-subscriptions-clob.polymarket.com/ws/market
Subscribe: {"assets_ids": ["token_id1", "token_id2"], "type": "Market"}

Events:
  "book"         → tam orderbook snapshot (bids/asks listesi)
  "price_change" → incremental update (changes listesi)
  "tick_size"    → ignored
  "last_trade_price" → ignored (sadece volume tracking için)

Sakladığı veri (per token_id):
  BookSnapshot: bid, ask, mid, spread, spread_pct, ts
"""

import asyncio
import json
import time
import websockets
import logger as log_module
from dataclasses import dataclass


PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_DELAY = 2.0


@dataclass
class BookSnapshot:
    token_id: str
    bid: float        # best bid (en yüksek alış)
    ask: float        # best ask (en düşük satış)
    mid: float        # (bid + ask) / 2
    spread: float     # ask - bid
    spread_pct: float # spread / mid * 100
    ts: float         # unix timestamp


class PolymarketFeed:
    def __init__(self, ws_url: str = PM_WS_URL):
        self.ws_url = ws_url
        self._books: dict[str, BookSnapshot] = {}  # token_id → snapshot
        self._subscribed_ids: list[str] = []
        self._running = False
        self._task: asyncio.Task | None = None
        self._ws = None

    def get_book(self, token_id: str) -> BookSnapshot | None:
        return self._books.get(token_id)

    def subscribe_tokens(self, token_ids: list[str]) -> None:
        """Takip edilecek token listesini ayarla (bağlantı öncesi veya sonrası)."""
        self._subscribed_ids = list(token_ids)

    def _update_book(self, token_id: str, bid: float, ask: float) -> None:
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
            ts=time.time(),
        )

    def _handle_book_event(self, msg: dict) -> None:
        """Full snapshot: {"event_type":"book","asset_id":"...","bids":[...],"asks":[...]}"""
        token_id = msg.get("asset_id", "")
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])

        best_bid = 0.0
        best_ask = 1.0

        if bids:
            # bids = [{"price": "0.93", "size": "50"}, ...] — en yüksek fiyat
            try:
                best_bid = max(float(b["price"]) for b in bids if float(b.get("size", 0)) > 0)
            except (ValueError, KeyError):
                pass

        if asks:
            # asks = [{"price": "0.95", "size": "30"}, ...] — en düşük fiyat
            try:
                best_ask = min(float(a["price"]) for a in asks if float(a.get("size", 0)) > 0)
            except (ValueError, KeyError):
                pass

        if best_bid > 0 and best_ask > best_bid:
            self._update_book(token_id, best_bid, best_ask)

    def _handle_price_change(self, msg: dict) -> None:
        """Incremental update. Mevcut snapshot'ı güncelle."""
        token_id = msg.get("asset_id", "")
        changes = msg.get("changes", [])

        book = self._books.get(token_id)
        bid = book.bid if book else 0.0
        ask = book.ask if book else 1.0

        for change in changes:
            try:
                price = float(change["price"])
                size = float(change.get("size", 0))
                side = change.get("side", "").upper()

                if side == "BUY":
                    if size > 0:
                        bid = max(bid, price)
                    elif price == bid:
                        bid = 0.0  # best bid gitti, snapshot bekliyoruz
                elif side == "SELL":
                    if size > 0:
                        ask = min(ask, price)
                    elif price == ask:
                        ask = 1.0  # best ask gitti
            except (ValueError, KeyError):
                pass

        if bid > 0 and ask > bid:
            self._update_book(token_id, bid, ask)

    async def _send_subscribe(self, ws) -> None:
        if not self._subscribed_ids:
            return
        msg = json.dumps({
            "assets_ids": self._subscribed_ids,
            "type": "Market",
        })
        await ws.send(msg)
        await log_module.log("pm_ws_subscribed", {"tokens": self._subscribed_ids})

    async def _run_once(self) -> None:
        try:
            async with websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                self._ws = ws
                await log_module.log("pm_ws_connected", {"url": self.ws_url})
                await self._send_subscribe(ws)

                async for raw in ws:
                    if not self._running:
                        break
                    try:
                        msg = json.loads(raw)
                        # Polymarket bazen liste, bazen tek obje gönderir
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

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self._run_once()
            if self._running:
                await log_module.log("pm_ws_reconnect", {"delay": RECONNECT_DELAY})
                await asyncio.sleep(RECONNECT_DELAY)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run())
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
        """İlk orderbook snapshot'ı bekle."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            book = self._books.get(token_id)
            if book:
                return book
            await asyncio.sleep(0.1)
        return None

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
