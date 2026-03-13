"""
core/polymarket_ws.py — V21 Polymarket orderbook REST poller.

WebSocket subscribe formatı sorunlu olduğundan doğrulanmış REST polling kullanılır.
Her 2 saniyede bir UP ve DOWN token'larının en iyi bid/ask fiyatlarını çeker.
BookSnapshot'ı SharedState'e yazar.
"""

import asyncio
import time
import utils.logger as logger_mod
from core.state import BookSnapshot

log = logger_mod.get("polymarket_poller")


class PolymarketPoller:
    def __init__(self, cfg: dict, state):
        self._cfg = cfg
        self._state = state
        net = cfg.get("network", {})
        self._clob_url = net.get("clob_url", "https://clob.polymarket.com")
        self._poll_interval: float = 2.0
        self._running: bool = False
        self._stale_limit_ms: float = cfg.get("stale_data_limit_ms", 5000)

    async def start(self) -> None:
        self._running = True
        log.info("Polymarket poller başladı (interval=%.1fs)", self._poll_interval)

        while self._running:
            market = self._state.market
            up_id = market.up_token_id
            down_id = market.down_token_id

            if up_id and down_id:
                try:
                    await self._poll(up_id, down_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Poll hatası: %s", exc)

            await asyncio.sleep(self._poll_interval)

    async def _poll(self, up_id: str, down_id: str) -> None:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            up_result, down_result = await asyncio.gather(
                self._get_book(sess, up_id),
                self._get_book(sess, down_id),
                return_exceptions=True,
            )

        if isinstance(up_result, Exception) or isinstance(down_result, Exception):
            log.debug("Book fetch hatası: up=%s down=%s", up_result, down_result)
            return

        if up_result is None or down_result is None:
            return

        up_bid, up_ask = up_result
        down_bid, down_ask = down_result

        self._state.book = BookSnapshot(
            up_bid=up_bid,
            up_ask=up_ask,
            down_bid=down_bid,
            down_ask=down_ask,
            timestamp=time.time(),
        )

        log.debug(
            "Book: UP bid=%.3f ask=%.3f | DOWN bid=%.3f ask=%.3f",
            up_bid, up_ask, down_bid, down_ask,
        )

    async def _get_book(self, sess, token_id: str):
        """
        (best_bid, best_ask) döner veya None.
        CLOB REST endpoint: GET /book?token_id=<id>
        """
        url = f"{self._clob_url}/book"
        params = {"token_id": token_id}

        async with sess.get(url, params=params) as resp:
            if resp.status != 200:
                log.debug("Book HTTP %d for token %s...", resp.status, token_id[:12])
                return None
            data = await resp.json(content_type=None)

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if not bids or not asks:
            return None

        try:
            best_bid = max(float(b.get("price", 0)) for b in bids)
            best_ask = min(float(a.get("price", 0)) for a in asks)
        except (ValueError, TypeError) as exc:
            log.debug("Price parse hatası: %s", exc)
            return None

        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return None

        return best_bid, best_ask

    async def stop(self) -> None:
        self._running = False
        log.info("Polymarket poller durduruldu")
