"""
Signal Engine — BTC delta → direction → fiyat/spread/fee kontrolü → sinyal.

Basit mantık (bucket/p_fair/scaling yok):
  1. delta = (btc_mid - btc_open) / btc_open * 100
  2. direction = UP if delta > 0 else DOWN
  3. entry_ask = o yönün token ask fiyatı (UP token veya DOWN token)
  4. Koşul 1: abs(delta) > min_delta_pct        → değilse delta_too_small
  5. Koşul 2: min_market_price < ask < max       → değilse price_out_of_range
  6. Koşul 3: spread_pct < max_spread_pct        → değilse spread_too_wide
  7. Koşul 4: depth >= min_orderbook_depth       → değilse depth_low
  8. Koşul 5: fee sonrası net_pnl_win > 0        → değilse no_edge
  Hepsi geçerse → buy_up / buy_down sinyali

Entry window: secs_to_res ∈ [entry_window_end_ste, entry_window_start_ste]
  Örn: start=240, end=10 → 10s ila 240s kala trade açılabilir
"""

from dataclasses import dataclass
from polymarket_feed import BookSnapshot
import logger as log_module


@dataclass
class Signal:
    action: str        # "buy_up" | "buy_down" | "skip"
    direction: str     # "up" | "down" | "none"
    delta_pct: float
    entry_ask: float   # token ask fiyatı (giriş fiyatı)
    fee: float         # tahmin edilen fee (USDC)
    net_pnl_win: float # kazanırsa beklenen net PnL (shares * (1-ask) - fee)
    spread_pct: float
    secs_to_res: int
    reason: str


def _compute_fee(shares: float, ask: float, fee_rate: float, fee_exponent: float) -> float:
    """fee = shares * ask * fee_rate * (ask * (1-ask))^fee_exponent — yeni formül."""
    if not (0 < ask < 1):
        return 0.0
    try:
        raw = shares * ask * fee_rate * (ask * (1.0 - ask)) ** fee_exponent
        return max(round(raw, 4), 0.0001)
    except Exception:
        return 0.0001


class SignalEngine:
    def __init__(self, config: dict):
        self.min_delta_pct: float = config.get("min_delta_pct", 0.08)
        self.max_spread_pct: float = config.get("max_spread_pct", 2.0)
        self.min_depth: float = config.get("min_orderbook_depth", 50.0)
        self.min_price: float = config.get("min_market_price", 0.30)
        self.max_price: float = config.get("max_market_price", 0.70)
        self.entry_window_start: int = config.get("entry_window_start_ste", 240)
        self.entry_window_end: int = config.get("entry_window_end_ste", 10)
        self.fee_rate: float = config.get("fee_rate", 0.072)
        self.fee_exponent: float = config.get("fee_exponent", 1.0)
        self.min_shares: int = config.get("min_shares", 5)

    def _skip(self, reason: str, secs_to_res: int, delta_pct: float = 0.0) -> Signal:
        return Signal(
            action="skip", direction="none",
            delta_pct=delta_pct, entry_ask=0.0, fee=0.0,
            net_pnl_win=0.0, spread_pct=0.0,
            secs_to_res=secs_to_res, reason=reason,
        )

    def evaluate(
        self,
        open_price: float,
        current_price: float,
        book_up: BookSnapshot | None,
        book_down: BookSnapshot | None,
        secs_to_res: int,
        shares: int | None = None,
    ) -> Signal:
        """
        Her Binance tick'inde çağrılır (throttle: main'de, 1/sn).
        Tüm koşullar config'den — hardcoded değer yok.
        """
        shares = shares or self.min_shares

        # open_price kontrolü
        if open_price <= 0:
            return self._skip("no_open_price", secs_to_res)

        # Delta hesapla (her zaman — log'da görünsün)
        try:
            delta_pct = (current_price - open_price) / open_price * 100.0
        except ZeroDivisionError:
            return self._skip("no_open_price", secs_to_res)

        # Entry window kontrolü
        if secs_to_res > self.entry_window_start or secs_to_res < self.entry_window_end:
            return self._skip("outside_entry_window", secs_to_res, delta_pct)

        # Orderbook mevcut mu?
        if book_up is None or book_down is None:
            return self._skip("no_orderbook", secs_to_res, delta_pct)

        # Koşul 1: delta yeterince büyük mü?
        delta_abs = abs(delta_pct)
        if delta_abs < self.min_delta_pct:
            return self._skip(f"delta_too_small({delta_abs:.3f}%)", secs_to_res, delta_pct)

        # Yön ve ilgili kitap
        if delta_pct > 0:
            direction = "up"
            book = book_up
        else:
            direction = "down"
            book = book_down

        entry_ask = book.ask

        # Koşul 2: fiyat geçerli aralıkta mı?
        if not (self.min_price < entry_ask < self.max_price):
            return Signal(
                action="skip", direction=direction,
                delta_pct=delta_pct, entry_ask=entry_ask, fee=0.0,
                net_pnl_win=0.0, spread_pct=book.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"price_out_of_range({entry_ask:.3f} not in [{self.min_price},{self.max_price}])",
            )

        # Koşul 3: spread dar mı?
        if book.spread_pct > self.max_spread_pct:
            return Signal(
                action="skip", direction=direction,
                delta_pct=delta_pct, entry_ask=entry_ask, fee=0.0,
                net_pnl_win=0.0, spread_pct=book.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"spread_too_wide({book.spread_pct:.1f}%>{self.max_spread_pct}%)",
            )

        # Koşul 4: orderbook depth yeterli mi?
        if book.bid_size < self.min_depth or book.ask_size < self.min_depth:
            return Signal(
                action="skip", direction=direction,
                delta_pct=delta_pct, entry_ask=entry_ask, fee=0.0,
                net_pnl_win=0.0, spread_pct=book.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"depth_low(bid={book.bid_size:.0f} ask={book.ask_size:.0f} min={self.min_depth})",
            )

        # Koşul 5: fee sonrası net PnL pozitif mi?
        fee = _compute_fee(shares, entry_ask, self.fee_rate, self.fee_exponent)
        net_pnl_win = shares * (1.0 - entry_ask) - fee

        if net_pnl_win <= 0:
            return Signal(
                action="skip", direction=direction,
                delta_pct=delta_pct, entry_ask=entry_ask, fee=fee,
                net_pnl_win=net_pnl_win, spread_pct=book.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"no_edge(net_pnl_win={net_pnl_win:.4f})",
            )

        return Signal(
            action=f"buy_{direction}", direction=direction,
            delta_pct=delta_pct, entry_ask=entry_ask, fee=fee,
            net_pnl_win=round(net_pnl_win, 4), spread_pct=book.spread_pct,
            secs_to_res=secs_to_res, reason="ok",
        )

    async def log_signal(self, signal: Signal) -> None:
        await log_module.log("signal", {
            "action": signal.action,
            "direction": signal.direction,
            "delta_pct": round(signal.delta_pct, 4),
            "entry_ask": signal.entry_ask,
            "fee": signal.fee,
            "net_pnl_win": signal.net_pnl_win,
            "spread_pct": signal.spread_pct,
            "secs_to_res": signal.secs_to_res,
            "reason": signal.reason,
        })
