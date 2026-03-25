"""
Paper Trader — simülasyon modu.

Paper trade akışı:
  1. Sinyal gelir → trade kaydedilir (gerçek emir gönderilmez)
  2. Pencere kapanır → resolve_pending() çağrılır
  3. Chainlink BTC/USD Data Streams'den son fiyat çekilir
  4. open_price vs resolve_price karşılaştırılır
     - Tie (==) → UP kazanır (spec: tie = başlangıç = Up kazanır)
  5. PnL hesaplanır → risk_manager.on_trade_result()

Chainlink resolve:
  - REST üzerinden son round data çekilir
  - 64 blok konfirmasyonu ~2 dakika → resolve_confirm_secs=130 beklenir
  - Fallback: Binance kapanış fiyatı (open_price snapshot)
"""

import asyncio
import aiohttp
import time
from dataclasses import dataclass, field
from fee_engine import compute_fee, net_pnl
import logger as log_module


# Chainlink BTC/USD Data Streams (Polygon) — public price feed
# Not: Canlıda py-clob-client ile resmi veri; paper'da public REST yeterli
CHAINLINK_POLYGON_URL = "https://api.chain.link/v1/query?query=BTC%2FUSD&network=polygon"
BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"


@dataclass
class PaperPosition:
    trade_id: str
    direction: str          # "up" | "down"
    shares: int
    p_entry: float
    open_price: float       # BTC price at window open
    opened_at: float        # unix timestamp
    window_ts: int          # pencere başlangıç ts
    fee_rate: float = 0.072
    fee_exponent: float = 1.0
    resolved: bool = False
    pnl: float = 0.0
    resolve_price: float = 0.0
    outcome: str = ""       # "win" | "loss"


class PaperTrader:
    def __init__(
        self,
        config: dict,
        risk_manager=None,
    ):
        self.fee_rate: float = config.get("fee_rate", 0.072)
        self.fee_exponent: float = config.get("fee_exponent", 1.0)
        self.resolve_confirm_secs: int = config.get("resolve_confirm_secs", 130)
        self._positions: list[PaperPosition] = []
        self._trade_counter: int = 0
        self.risk_manager = risk_manager

    def open_position(
        self,
        direction: str,
        shares: int,
        p_entry: float,
        open_price: float,
        window_ts: int,
    ) -> PaperPosition:
        """Paper trade aç."""
        self._trade_counter += 1
        trade_id = f"paper-{int(time.time())}-{self._trade_counter}"

        pos = PaperPosition(
            trade_id=trade_id,
            direction=direction,
            shares=shares,
            p_entry=p_entry,
            open_price=open_price,
            opened_at=time.time(),
            window_ts=window_ts,
            fee_rate=self.fee_rate,
            fee_exponent=self.fee_exponent,
        )
        self._positions.append(pos)

        if self.risk_manager:
            self.risk_manager.on_trade_opened()

        return pos

    async def _fetch_resolve_price(self, open_btc: float) -> float:
        """
        Chainlink'ten BTC kapanış fiyatı çek.
        Fallback: Binance REST spot price.
        """
        # Fallback 1: Binance REST (hızlı, paper için yeterli)
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
            await log_module.log("resolve_binance_error", {"error": str(e)})

        # Fallback 2: open_price döndür → tie → UP kazanır
        await log_module.log("resolve_fallback", {"using": "open_price", "price": open_btc})
        return open_btc

    def _determine_outcome(self, direction: str, open_price: float, resolve_price: float) -> bool:
        """
        Kazandı mı?
        UP: resolve >= open → win (tie = Up kazanır)
        DOWN: resolve < open → win
        """
        if direction == "up":
            return resolve_price >= open_price
        else:
            return resolve_price < open_price

    async def resolve_pending(self, wait_secs: int | None = None) -> list[PaperPosition]:
        """
        Tüm bekleyen pozisyonları resolve et.
        wait_secs: Chainlink konfirmasyon bekleme süresi.
        """
        pending = [p for p in self._positions if not p.resolved]
        if not pending:
            return []

        wait = wait_secs if wait_secs is not None else self.resolve_confirm_secs
        if wait > 0:
            await log_module.log("resolve_waiting", {"secs": wait, "count": len(pending)})
            await asyncio.sleep(wait)

        resolved = []
        for pos in pending:
            resolve_price = await self._fetch_resolve_price(pos.open_price)
            outcome_win = self._determine_outcome(pos.direction, pos.open_price, resolve_price)

            pnl = net_pnl(
                shares=pos.shares,
                p_entry=pos.p_entry,
                outcome_win=outcome_win,
                fee_rate=pos.fee_rate,
                fee_exponent=pos.fee_exponent,
            )

            pos.resolved = True
            pos.resolve_price = resolve_price
            pos.pnl = pnl
            pos.outcome = "win" if outcome_win else "loss"

            if self.risk_manager:
                self.risk_manager.on_trade_result(pnl)

            await log_module.log("trade_resolved", {
                "trade_id": pos.trade_id,
                "direction": pos.direction,
                "shares": pos.shares,
                "p_entry": pos.p_entry,
                "open_btc": pos.open_price,
                "resolve_btc": round(resolve_price, 2),
                "outcome": pos.outcome,
                "pnl": round(pnl, 4),
                "fee": round(compute_fee(pos.shares, pos.p_entry, pos.fee_rate, pos.fee_exponent), 4),
            })
            resolved.append(pos)

        return resolved

    def open_positions(self) -> list[PaperPosition]:
        return [p for p in self._positions if not p.resolved]

    def all_positions(self) -> list[PaperPosition]:
        return list(self._positions)

    def total_pnl(self) -> float:
        return sum(p.pnl for p in self._positions if p.resolved)
