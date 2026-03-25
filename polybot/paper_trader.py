"""
Paper Trader — simülasyon modu.

Trade akışı:
  1. Sinyal → open_position() → PaperPosition kaydedilir
  2. Pencere kapanır → resolve_pending() çağrılır
  3. Binance REST'ten pencere kapanış fiyatı çekilir (fallback: open_price → tie → UP kazanır)
  4. Sonuç belirlenir:
       UP: btc_close >= btc_open → WIN
       DOWN: btc_close < btc_open → WIN
       Tie (==): UP kazanır
  5. PnL:
       WIN:  shares * (1.0 - entry_ask) - fee
       LOSS: -(shares * entry_ask + fee)
  6. JSONL log: timestamp, window, direction, entry_price, fee, result, pnl

Fee formülü (YENİ, config'den):
  fee = shares * ask * fee_rate * (ask * (1-ask))^fee_exponent
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
    direction: str       # "up" | "down"
    shares: int
    entry_ask: float     # token ask fiyatı (giriş fiyatı)
    btc_open: float      # BTC pencere açılış fiyatı
    fee: float           # önceden hesaplanan fee
    opened_at: float     # unix timestamp
    window_ts: int
    # resolve sonrası doldurulur
    resolved: bool = False
    btc_close: float = 0.0
    result: str = ""     # "win" | "loss"
    pnl: float = 0.0


def _compute_fee(shares: float, ask: float, fee_rate: float, fee_exponent: float) -> float:
    """fee = shares * ask * fee_rate * (ask * (1-ask))^fee_exponent"""
    if not (0 < ask < 1):
        return 0.0
    try:
        raw = shares * ask * fee_rate * (ask * (1.0 - ask)) ** fee_exponent
        return max(round(raw, 4), 0.0001)
    except Exception:
        return 0.0001


class PaperTrader:
    def __init__(self, config: dict, risk_manager=None):
        self.fee_rate: float = config.get("fee_rate", 0.072)
        self.fee_exponent: float = config.get("fee_exponent", 1.0)
        self.resolve_confirm_secs: int = config.get("resolve_confirm_secs", 130)
        self._positions: list[PaperPosition] = []
        self._counter: int = 0
        self.risk_manager = risk_manager

    def open_position(
        self,
        direction: str,
        shares: int,
        entry_ask: float,
        btc_open: float,
        window_ts: int,
    ) -> PaperPosition:
        """Paper trade aç — gerçek emir gönderilmez."""
        self._counter += 1
        fee = _compute_fee(shares, entry_ask, self.fee_rate, self.fee_exponent)
        pos = PaperPosition(
            trade_id=f"paper-{int(time.time())}-{self._counter}",
            direction=direction,
            shares=shares,
            entry_ask=entry_ask,
            btc_open=btc_open,
            fee=fee,
            opened_at=time.time(),
            window_ts=window_ts,
        )
        self._positions.append(pos)
        if self.risk_manager:
            self.risk_manager.on_trade_opened()
        return pos

    async def _fetch_btc_close(self, btc_open: float) -> float:
        """Binance REST'ten anlık BTC fiyatını çek. Fallback: btc_open (tie → UP kazanır)."""
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

    @staticmethod
    def _is_win(direction: str, btc_open: float, btc_close: float) -> bool:
        """UP: close >= open (tie = UP kazanır). DOWN: close < open."""
        if direction == "up":
            return btc_close >= btc_open
        return btc_close < btc_open

    async def resolve_pending(self, wait_secs: int | None = None) -> list[PaperPosition]:
        """Bekleyen tüm pozisyonları resolve et."""
        pending = [p for p in self._positions if not p.resolved]
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
                win = self._is_win(pos.direction, pos.btc_open, btc_close)

                if win:
                    pnl = pos.shares * (1.0 - pos.entry_ask) - pos.fee
                else:
                    pnl = -(pos.shares * pos.entry_ask + pos.fee)

                pos.resolved = True
                pos.btc_close = btc_close
                pos.result = "win" if win else "loss"
                pos.pnl = round(pnl, 4)

                if self.risk_manager:
                    self.risk_manager.on_trade_result(pos.pnl)

                await log_module.log("trade_resolved", {
                    "trade_id": pos.trade_id,
                    "timestamp": time.time(),
                    "window": pos.window_ts,
                    "direction": pos.direction,
                    "entry_price": pos.entry_ask,
                    "fee": pos.fee,
                    "btc_open": pos.btc_open,
                    "btc_close": round(btc_close, 2),
                    "result": pos.result,
                    "pnl": pos.pnl,
                })
                resolved.append(pos)
            except Exception as e:
                await log_module.log("resolve_error", {"trade_id": pos.trade_id, "error": str(e)})

        return resolved

    def open_positions(self) -> list[PaperPosition]:
        return [p for p in self._positions if not p.resolved]

    def all_positions(self) -> list[PaperPosition]:
        return list(self._positions)

    def total_pnl(self) -> float:
        return sum(p.pnl for p in self._positions if p.resolved)
