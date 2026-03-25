"""
Signal Engine — Dual Side Capture stratejisi.

Mantık (delta/direction YOK):
  pair_sum = up_ask + down_ask
  net_edge = 1.0 - pair_sum   (fee = 0, maker/post-only order)

  pair_sum < target_sum_max (0.95) → DUAL_ENTRY
  pair_sum >= target_sum_max       → skip "pair_sum_too_high"

Neden pair_sum < 1.0 kârlı:
  Her pencerede ya UP ya DOWN kazanır → payout = 1.0
  Maliyet = up_ask + down_ask = pair_sum
  Kâr = 1.0 - pair_sum (fee yok — post-only maker)

Koşullar (sırayla):
  1. Entry window   → outside_entry_window
  2. Orderbook mevcut → no_orderbook
  3. Spread dar     → spread_too_wide
  4. Depth yeterli  → depth_low
  5. pair_sum < 0.95 → pair_sum_too_high
  6. net_edge > 0   → no_edge  (güvenlik — pair_sum < 0.95 ise zaten > 0)
"""

from dataclasses import dataclass
from polymarket_feed import BookSnapshot
import logger as log_module


@dataclass
class Signal:
    action: str       # "dual_entry" | "skip"
    up_ask: float
    down_ask: float
    pair_sum: float   # up_ask + down_ask
    net_edge: float   # 1.0 - pair_sum (beklenen kâr / share)
    spread_up: float
    spread_down: float
    secs_to_res: int
    reason: str


class SignalEngine:
    def __init__(self, config: dict):
        self.target_sum_max: float = config.get("target_sum_max", 0.95)
        self.max_spread_pct: float = config.get("max_spread_pct", 2.0)
        self.min_depth: float = config.get("min_orderbook_depth", 50.0)
        self.entry_window_start: int = config.get("entry_window_start_ste", 240)
        self.entry_window_end: int = config.get("entry_window_end_ste", 10)

    def _skip(self, reason: str, secs_to_res: int,
              up_ask: float = 0.0, down_ask: float = 0.0) -> Signal:
        pair_sum = up_ask + down_ask
        return Signal(
            action="skip", up_ask=up_ask, down_ask=down_ask,
            pair_sum=pair_sum, net_edge=round(1.0 - pair_sum, 4),
            spread_up=0.0, spread_down=0.0,
            secs_to_res=secs_to_res, reason=reason,
        )

    def evaluate(
        self,
        book_up: BookSnapshot | None,
        book_down: BookSnapshot | None,
        secs_to_res: int,
    ) -> Signal:
        """
        Her saniye çağrılır (tick throttle main'de).
        BTC delta / yön bilgisi gerekmez.
        """
        # 1. Entry window
        if secs_to_res > self.entry_window_start or secs_to_res < self.entry_window_end:
            return self._skip("outside_entry_window", secs_to_res)

        # 2. Orderbook mevcut mu?
        if book_up is None or book_down is None:
            return self._skip("no_orderbook", secs_to_res)

        up_ask = book_up.ask
        down_ask = book_down.ask

        # 3. Her iki tarafın spread'i dar mı?
        if book_up.spread_pct > self.max_spread_pct:
            return Signal(
                action="skip", up_ask=up_ask, down_ask=down_ask,
                pair_sum=up_ask + down_ask, net_edge=0.0,
                spread_up=book_up.spread_pct, spread_down=book_down.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"spread_too_wide(up={book_up.spread_pct:.1f}%>{self.max_spread_pct}%)",
            )
        if book_down.spread_pct > self.max_spread_pct:
            return Signal(
                action="skip", up_ask=up_ask, down_ask=down_ask,
                pair_sum=up_ask + down_ask, net_edge=0.0,
                spread_up=book_up.spread_pct, spread_down=book_down.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"spread_too_wide(down={book_down.spread_pct:.1f}%>{self.max_spread_pct}%)",
            )

        # 4. Depth yeterli mi?
        min_depth_side = min(book_up.ask_size, book_down.ask_size)
        if min_depth_side < self.min_depth:
            return Signal(
                action="skip", up_ask=up_ask, down_ask=down_ask,
                pair_sum=up_ask + down_ask, net_edge=0.0,
                spread_up=book_up.spread_pct, spread_down=book_down.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"depth_low(min={min_depth_side:.0f}<{self.min_depth})",
            )

        # 5. Pair sum kontrolü
        pair_sum = round(up_ask + down_ask, 4)
        net_edge = round(1.0 - pair_sum, 4)

        if pair_sum >= self.target_sum_max:
            return Signal(
                action="skip", up_ask=up_ask, down_ask=down_ask,
                pair_sum=pair_sum, net_edge=net_edge,
                spread_up=book_up.spread_pct, spread_down=book_down.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"pair_sum_too_high({pair_sum:.4f}>={self.target_sum_max})",
            )

        # 6. Net edge pozitif mi? (pair_sum < 1.0 ise her zaman geçer, güvenlik)
        if net_edge <= 0:
            return Signal(
                action="skip", up_ask=up_ask, down_ask=down_ask,
                pair_sum=pair_sum, net_edge=net_edge,
                spread_up=book_up.spread_pct, spread_down=book_down.spread_pct,
                secs_to_res=secs_to_res, reason="no_edge",
            )

        return Signal(
            action="dual_entry", up_ask=up_ask, down_ask=down_ask,
            pair_sum=pair_sum, net_edge=net_edge,
            spread_up=book_up.spread_pct, spread_down=book_down.spread_pct,
            secs_to_res=secs_to_res, reason="ok",
        )

    async def log_signal(self, signal: Signal) -> None:
        await log_module.log("signal", {
            "action": signal.action,
            "up_ask": signal.up_ask,
            "down_ask": signal.down_ask,
            "pair_sum": signal.pair_sum,
            "net_edge": signal.net_edge,
            "spread_up": signal.spread_up,
            "spread_down": signal.spread_down,
            "secs_to_res": signal.secs_to_res,
            "reason": signal.reason,
        })
