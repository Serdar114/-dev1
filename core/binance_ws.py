"""
core/binance_ws.py — V21 Binance WebSocket istemcisi.

Binance combined stream'e bağlanır:
  btcusdt@depth20@100ms  → depth snapshot → OFI engine
  btcusdt@trade          → trade tick    → OFI engine (TFI)

BTC mid fiyatını SharedState'e yazar.
Kopma durumunda 5s bekleyip yeniden bağlanır.
"""

import asyncio
import json
import time
import utils.logger as logger_mod

log = logger_mod.get("binance_ws")

BINANCE_WS_URL = (
    "wss://stream.binance.com:9443/stream"
    "?streams=btcusdt@depth20@100ms/btcusdt@trade"
)


class BinanceStream:
    def __init__(self, state, ofi_engine):
        self._state = state
        self._ofi = ofi_engine
        self._running = False
        self._ws = None

    async def start(self) -> None:
        """Sonsuz döngüde bağlanır; kopunca yeniden dener."""
        self._running = True
        retry_delay = 2.0
        while self._running:
            try:
                await self._connect()
                retry_delay = 2.0  # başarılı bağlantıdan sonra sıfırla
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("Binance WS koptu: %s — %gs sonra yeniden dener", exc, retry_delay)
                if self._running:
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30.0)

    async def _connect(self) -> None:
        # websockets importunu burada yapıyoruz; kurulu değilse net hata verir
        try:
            import websockets
        except ImportError:
            raise RuntimeError("'websockets' paketi eksik: pip install websockets")

        log.info("Binance WS bağlanıyor: %s", BINANCE_WS_URL)
        async with websockets.connect(
            BINANCE_WS_URL,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            log.info("Binance WS bağlantı kuruldu")
            self._state.log_event("Binance WS bağlandı")

            async for raw in ws:
                if not self._running:
                    break
                try:
                    self._dispatch(json.loads(raw))
                except Exception as exc:
                    log.debug("Mesaj parse hatası: %s", exc)

    def _dispatch(self, msg: dict) -> None:
        stream = msg.get("stream", "")
        data = msg.get("data", {})

        if "depth" in stream:
            self._handle_depth(data)
        elif "trade" in stream:
            self._handle_trade(data)

    # ── Depth handler ────────────────────────────────────────────

    def _handle_depth(self, data: dict) -> None:
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        # BTC mid fiyatını güncelle
        if bids and asks:
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            self._state.btc_price = (best_bid + best_ask) / 2.0
            self._state.btc_price_ts = time.time()

        # OFI engine'e depth ver
        self._ofi.on_depth(bids, asks)

    # ── Trade handler ────────────────────────────────────────────

    def _handle_trade(self, data: dict) -> None:
        try:
            price = float(data.get("p", 0))
            qty = float(data.get("q", 0))
            is_buyer_maker = bool(data.get("m", False))
            self._ofi.on_trade(price, qty, is_buyer_maker)
        except Exception as exc:
            log.debug("Trade parse hatası: %s", exc)

    # ── Temiz kapatma ─────────────────────────────────────────────

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        log.info("Binance WS durduruldu")
