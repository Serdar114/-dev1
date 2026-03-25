"""
Paper Trader — Dual Side Capture simülasyonu.

Trade akışı:
  1. DUAL_ENTRY sinyali → open_position() → her iki taraf ask'tan girilir
  2. Pencere kapanır → resolve_pending()
  3. Binance REST → btc_close çekilir
  4. Kazanan taraf belirlenir (logging için — PnL her iki durumda aynı):
       UP wins: btc_close >= btc_open
       DOWN wins: btc_close < btc_open
       Tie: UP kazanır
  5. PnL:
       pnl = shares * (1.0 - up_ask - down_ask)   ← FEE YOK (maker/post-only)
  6. JSONL: timestamp, window, up_ask, down_ask, pair_sum, net_edge, result, pnl

Fee = 0: post-only maker order, Polymarket maker rebate pozitif veya sıfır.
"""

import asyncio
import aiohttp
import time
from dataclasses import dataclass
import logger as log_module


BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"


@dataclass
class PaperPosition:
    trade_id: str
    up_ask: float
    down_ask: float
    pair_sum: float      # up_ask + down_ask
    net_edge: float      # 1.0 - pair_sum (beklenen kâr / share)
    shares: int          # shares_per_side (her taraf için)
    btc_open: float
    opened_at: float
    window_ts: int
    # resolve sonrası
    resolved: bool = False
    btc_close: float = 0.0
    winning_side: str = ""   # "up" | "down"
    result: str = ""         # "win_up" | "win_down"
    pnl: float = 0.0         # shares * net_edge


class PaperTrader:
    def __init__(self, config: dict, risk_manager=None):
        self.resolve_confirm_secs: int = config.get("resolve_confirm_secs", 130)
        self.shares_per_side: int = config.get("shares_per_side", 5)
        self._positions: list[PaperPosition] = []
        self._counter: int = 0
        self.risk_manager = risk_manager

    def open_position(
        self,
        up_ask: float,
        down_ask: float,
        shares: int,
        btc_open: float,
        window_ts: int,
    ) -> PaperPosition:
        """Dual entry simüle et — her iki taraf ask'tan fill edildi kabul edilir."""
        self._counter += 1
        pair_sum = round(up_ask + down_ask, 4)
        net_edge = round(1.0 - pair_sum, 4)

        pos = PaperPosition(
            trade_id=f"dual-{int(time.time())}-{self._counter}",
            up_ask=up_ask,
            down_ask=down_ask,
            pair_sum=pair_sum,
            net_edge=net_edge,
            shares=shares,
            btc_open=btc_open,
            opened_at=time.time(),
            window_ts=window_ts,
        )
        self._positions.append(pos)

        if self.risk_manager:
            self.risk_manager.on_trade_opened()

        return pos

    async def _fetch_btc_close(self, btc_open: float) -> float:
        """Binance REST anlık fiyat. Fallback: btc_open (tie → UP kazanır)."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    BINANCE_REST_URL,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return float(data["price"])
        except Exception as e:
            await log_module.log("resolve_fetch_error", {"error": str(e)})

        await log_module.log("resolve_fallback", {"using": "btc_open", "price": btc_open})
        return btc_open

    async def resolve_pending(self, wait_secs: int | None = None) -> list[PaperPosition]:
        """Bekleyen pozisyonları resolve et."""
        pending = [pos for pos in self._positions if not pos.resolved]
        if not pending:
            return []

        wait = wait_secs if wait_secs is not None else self.resolve_confirm_secs
        if wait > 0:
            await log_module.log("resolve_waiting", {"secs": wait, "count": len(pending)})
            await asyncio.sleep(wait)

        resolved = []
        for pos in pending:
            try:
                btc_close = await self._fetch_btc_close(pos.btc_open)

                # Kazanan taraf (logging için — PnL her iki durumda aynı)
                if btc_close >= pos.btc_open:
                    winning_side = "up"
                    result = "win_up"
                else:
                    winning_side = "down"
                    result = "win_down"

                # PnL: fee yok — post-only maker
                pnl = round(pos.shares * pos.net_edge, 4)

                pos.resolved = True
                pos.btc_close = round(btc_close, 2)
                pos.winning_side = winning_side
                pos.result = result
                pos.pnl = pnl

                if self.risk_manager:
                    self.risk_manager.on_trade_result(pnl)

                await log_module.log("trade_resolved", {
                    "trade_id": pos.trade_id,
                    "timestamp": time.time(),
                    "window": pos.window_ts,
                    "up_ask": pos.up_ask,
                    "down_ask": pos.down_ask,
                    "pair_sum": pos.pair_sum,
                    "net_edge": pos.net_edge,
                    "shares": pos.shares,
                    "btc_open": pos.btc_open,
                    "btc_close": pos.btc_close,
                    "result": pos.result,
                    "pnl": pos.pnl,
                })
                resolved.append(pos)

            except Exception as e:
                await log_module.log("resolve_error", {
                    "trade_id": pos.trade_id, "error": str(e),
                })

        return resolved

    def open_positions(self) -> list[PaperPosition]:
        return [pos for pos in self._positions if not pos.resolved]

    def all_positions(self) -> list[PaperPosition]:
        return list(self._positions)

    def total_pnl(self) -> float:
        return sum(pos.pnl for pos in self._positions if pos.resolved)

    def summary_stats(self) -> dict:
        """10 pencere sonunda rapor için istatistikler."""
        resolved = [pos for pos in self._positions if pos.resolved]
        if not resolved:
            return {"trades": 0, "total_pnl": 0.0, "avg_net_edge": 0.0,
                    "win_up": 0, "win_down": 0}
        total_pnl = sum(pos.pnl for pos in resolved)
        avg_edge = sum(pos.net_edge for pos in resolved) / len(resolved)
        win_up = sum(1 for pos in resolved if pos.result == "win_up")
        win_down = sum(1 for pos in resolved if pos.result == "win_down")
        return {
            "trades": len(resolved),
            "total_pnl": round(total_pnl, 4),
            "avg_net_edge": round(avg_edge, 4),
            "avg_pnl_per_trade": round(total_pnl / len(resolved), 4),
            "win_up": win_up,
            "win_down": win_down,
            "dual_fill_rate": "100%",  # paper modda her zaman %100
        }
