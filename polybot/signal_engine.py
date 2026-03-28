"""
Signal Engine — Dual Side Capture + Single Side Taker stratejileri.

Dual (default):
  pair_sum = up_ask + down_ask
  net_edge = 1.0 - pair_sum
  pair_sum < target_sum_max → DUAL_ENTRY

Single side (strategy=single_side_taker, forced_side=up|down):
  Forced side'ın ask fiyatı < max_entry_price → SINGLE_ENTRY_UP|DOWN
  Yön kararı dışarıdan verilir (config forced_side).
  PnL: win → shares×(1-ask)-fee, lose → -(shares×ask+fee)
"""

from dataclasses import dataclass
from polymarket_feed import BookSnapshot
import logger as log_module


@dataclass
class Signal:
    action: str       # "dual_entry" | "single_entry_up" | "single_entry_down" | "skip"
    up_ask: float
    down_ask: float
    pair_sum: float   # up_ask + down_ask (0 for single-side)
    net_edge: float   # 1.0 - pair_sum (0 for single-side)
    spread_up: float
    spread_down: float
    secs_to_res: int
    reason: str
    side: str = ""            # "" for dual, "up"|"down" for single
    entry_price: float = 0.0  # ask price of the chosen side (single-side only)


class SignalEngine:
    def __init__(self, config: dict):
        self.target_sum_max: float = config.get("target_sum_max", 0.95)
        self.max_spread_pct: float = config.get("max_spread_pct", 2.0)
        self.min_depth: float = config.get("min_orderbook_depth", 50.0)
        self.entry_window_start: int = config.get("entry_window_start_ste", 240)
        self.entry_window_end: int = config.get("entry_window_end_ste", 10)
        # Single-side config
        self.strategy: str = config.get("strategy", "dual_side_capture")
        self.forced_side: str = config.get("forced_side", "")  # "up" | "down"
        self.max_entry_price: float = config.get("max_entry_price", 0.60)

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

    def evaluate_single(
        self,
        book_up: BookSnapshot | None,
        book_down: BookSnapshot | None,
        secs_to_res: int,
    ) -> Signal:
        """
        Single-side taker: forced_side'ın ask'ını değerlendir.
        Yön kararı config'den gelir — bu fonksiyon sadece giriş koşullarını kontrol eder.
        """
        side = self.forced_side
        if side not in ("up", "down"):
            return self._skip("invalid_forced_side", secs_to_res)

        # 1. Entry window
        if secs_to_res > self.entry_window_start or secs_to_res < self.entry_window_end:
            return self._skip("outside_entry_window", secs_to_res)

        # 2. Orderbook for forced side
        book = book_up if side == "up" else book_down
        if book is None:
            return self._skip("no_orderbook", secs_to_res)

        ask = book.ask
        spread = book.spread_pct
        depth = book.ask_size

        # Populate both ask fields for logging (0 if other side missing)
        up_ask = book_up.ask if book_up else 0.0
        down_ask = book_down.ask if book_down else 0.0

        # 3. Spread
        if spread > self.max_spread_pct:
            return Signal(
                action="skip", up_ask=up_ask, down_ask=down_ask,
                pair_sum=0.0, net_edge=0.0,
                spread_up=book_up.spread_pct if book_up else 0.0,
                spread_down=book_down.spread_pct if book_down else 0.0,
                secs_to_res=secs_to_res,
                reason=f"spread_too_wide({side}={spread:.1f}%>{self.max_spread_pct}%)",
                side=side, entry_price=ask,
            )

        # 4. Depth
        if depth < self.min_depth:
            return Signal(
                action="skip", up_ask=up_ask, down_ask=down_ask,
                pair_sum=0.0, net_edge=0.0,
                spread_up=book_up.spread_pct if book_up else 0.0,
                spread_down=book_down.spread_pct if book_down else 0.0,
                secs_to_res=secs_to_res,
                reason=f"depth_low({side}={depth:.0f}<{self.min_depth})",
                side=side, entry_price=ask,
            )

        # 5. Entry price cap
        if ask > self.max_entry_price:
            return Signal(
                action="skip", up_ask=up_ask, down_ask=down_ask,
                pair_sum=0.0, net_edge=0.0,
                spread_up=book_up.spread_pct if book_up else 0.0,
                spread_down=book_down.spread_pct if book_down else 0.0,
                secs_to_res=secs_to_res,
                reason=f"price_too_high({side}_ask={ask:.4f}>{self.max_entry_price})",
                side=side, entry_price=ask,
            )

        action = f"single_entry_{side}"
        return Signal(
            action=action, up_ask=up_ask, down_ask=down_ask,
            pair_sum=0.0, net_edge=0.0,
            spread_up=book_up.spread_pct if book_up else 0.0,
            spread_down=book_down.spread_pct if book_down else 0.0,
            secs_to_res=secs_to_res, reason="ok",
            side=side, entry_price=ask,
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
            "side": signal.side,
            "entry_price": signal.entry_price,
        })
