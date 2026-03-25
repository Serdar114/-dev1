"""
Binance WebSocket bookTicker client.

wss://stream.binance.com:9443/ws/btcusdt@bookTicker
→ {"b": best_bid, "a": best_ask, ...}  mid = (bid + ask) / 2

Özellikler:
  - asyncio tabanlı, reconnect destekli
  - open_price: pencere başında (ilk open_price_capture_secs saniye) mid kaydedilir
  - Subscribers callback ile canlı fiyat alır
"""

import asyncio
import json
import time
import websockets
import logger as log_module
from typing import Callable, Awaitable


BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"
RECONNECT_DELAY = 3.0  # saniye


class BinanceFeed:
    def __init__(
        self,
        ws_url: str = BINANCE_WS,
        open_price_capture_secs: int = 5,
    ):
        self.ws_url = ws_url
        self.open_price_capture_secs = open_price_capture_secs

        self._mid: float | None = None
        self._open_price: float | None = None
        self._open_capture_until: float = 0.0

        self._subscribers: list[Callable[[float], Awaitable[None]]] = []
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def mid(self) -> float | None:
        return self._mid

    @property
    def open_price(self) -> float | None:
        return self._open_price

    def subscribe(self, callback: Callable[[float], Awaitable[None]]) -> None:
        """Yeni fiyat geldiğinde çağrılacak async callback kaydeder."""
        self._subscribers.append(callback)

    def mark_window_open(self) -> None:
        """Yeni pencere başladığında çağır — open_price sıfırlanır."""
        self._open_price = None
        self._open_capture_until = time.time() + self.open_price_capture_secs

    async def _notify(self, mid: float) -> None:
        # Open price capture
        if self._open_price is None and time.time() <= self._open_capture_until:
            self._open_price = mid
            await log_module.log("open_price_captured", {"btc_open": mid})

        for cb in self._subscribers:
            try:
                await cb(mid)
            except Exception as e:
                await log_module.log("subscriber_error", {"error": str(e)})

    async def _run_once(self) -> None:
        try:
            async with websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                await log_module.log("binance_ws_connected", {"url": self.ws_url})
                async for raw in ws:
                    if not self._running:
                        break
                    try:
                        msg = json.loads(raw)
                        bid = float(msg["b"])
                        ask = float(msg["a"])
                        mid = (bid + ask) / 2.0
                        self._mid = mid
                        await self._notify(mid)
                    except (KeyError, ValueError, json.JSONDecodeError):
                        pass
        except Exception as e:
            await log_module.log("binance_ws_error", {"error": str(e)})

    async def run(self) -> None:
        """Ana loop — reconnect destekli."""
        self._running = True
        while self._running:
            await self._run_once()
            if self._running:
                await log_module.log("binance_ws_reconnect", {"delay": RECONNECT_DELAY})
                await asyncio.sleep(RECONNECT_DELAY)

    async def wait_for_mid(self, timeout: float = 8.0) -> float | None:
        """
        İlk Binance tick'ini bekle (max timeout saniye).
        Bağlantı gelmezse None döndür.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._mid is not None:
                return self._mid
            await asyncio.sleep(0.1)
        return None

    def start(self) -> asyncio.Task:
        """Event loop'a task olarak ekle."""
        self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
