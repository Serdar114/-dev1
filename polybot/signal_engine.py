"""
Signal Engine — Dual Side Capture + Single Side Taker.

Dual mode (strategy="dual_side_capture"):
  pair_sum = up_ask + down_ask
  net_edge = 1.0 - pair_sum
  pair_sum < target_sum_max (0.95) → dual_entry

Single-side mode (strategy="single_side_taker"):
  Gates (in order):
    1. entry_window
    2. orderbook present
    3. determine side: forced_side OR btc_open_delta
    4. spread for chosen side
    5. depth for chosen side
    6. price cap (entry_price <= max_entry_price)
  → single_entry  |  skip(reason)

btc_open_delta: side = "up" if (btc_mid - btc_open)/btc_open*10000 > min_move_bps else "down"
"""

from dataclasses import dataclass, field
from polymarket_feed import BookSnapshot
import logger as log_module


@dataclass
class Signal:
    action: str        # "dual_entry" | "single_entry" | "skip"
    up_ask: float
    down_ask: float
    pair_sum: float    # up_ask + down_ask (dual); 0.0 for single
    net_edge: float    # 1.0 - pair_sum (dual); 0.0 for single
    spread_up: float
    spread_down: float
    secs_to_res: int
    reason: str
    # single-side fields (zero/empty for dual)
    side: str = ""           # "up" | "down"
    entry_price: float = 0.0 # ask of chosen side
    delta_bps: float = 0.0   # btc_open_delta value (0.0 for forced_side or dual)


class SignalEngine:
    def __init__(self, config: dict):
        self.target_sum_max: float = config.get("target_sum_max", 0.95)
        self.max_spread_pct: float = config.get("max_spread_pct", 2.0)
        self.min_depth: float = config.get("min_orderbook_depth", 50.0)
        self.entry_window_start: int = config.get("entry_window_start_ste", 240)
        self.entry_window_end: int = config.get("entry_window_end_ste", 10)
        # single-side config
        self.signal_mode: str = config.get("signal_mode", "btc_open_delta")
        self.forced_side: str = config.get("forced_side", "")
        self.min_move_bps: float = config.get("min_move_bps", 5.0)
        self.max_entry_price: float = config.get("max_entry_price", 0.65)

    # ── helpers ────────────────────────────────────────────────────────────────

    def _skip(self, reason: str, secs_to_res: int,
              up_ask: float = 0.0, down_ask: float = 0.0,
              spread_up: float = 0.0, spread_down: float = 0.0,
              side: str = "", entry_price: float = 0.0,
              delta_bps: float = 0.0) -> Signal:
        pair_sum = up_ask + down_ask
        return Signal(
            action="skip", up_ask=up_ask, down_ask=down_ask,
            pair_sum=pair_sum, net_edge=round(1.0 - pair_sum, 4),
            spread_up=spread_up, spread_down=spread_down,
            secs_to_res=secs_to_res, reason=reason,
            side=side, entry_price=entry_price, delta_bps=delta_bps,
        )

    # ── dual side capture ──────────────────────────────────────────────────────

    def evaluate(
        self,
        book_up: BookSnapshot | None,
        book_down: BookSnapshot | None,
        secs_to_res: int,
    ) -> Signal:
        """Dual side capture — BTC direction not required."""
        # 1. Entry window
        if secs_to_res > self.entry_window_start or secs_to_res < self.entry_window_end:
            return self._skip("outside_entry_window", secs_to_res)

        # 2. Orderbook present?
        if book_up is None or book_down is None:
            return self._skip("no_orderbook", secs_to_res)

        up_ask = book_up.ask
        down_ask = book_down.ask

        # 3. Spread gates (both sides)
        if book_up.spread_pct > self.max_spread_pct:
            return self._skip(
                f"spread_too_wide(up={book_up.spread_pct:.1f}%>{self.max_spread_pct}%)",
                secs_to_res, up_ask, down_ask, book_up.spread_pct, book_down.spread_pct,
            )
        if book_down.spread_pct > self.max_spread_pct:
            return self._skip(
                f"spread_too_wide(down={book_down.spread_pct:.1f}%>{self.max_spread_pct}%)",
                secs_to_res, up_ask, down_ask, book_up.spread_pct, book_down.spread_pct,
            )

        # 4. Depth gate
        min_depth_side = min(book_up.ask_size, book_down.ask_size)
        if min_depth_side < self.min_depth:
            return self._skip(
                f"depth_low(min={min_depth_side:.0f}<{self.min_depth})",
                secs_to_res, up_ask, down_ask, book_up.spread_pct, book_down.spread_pct,
            )

        # 5. Pair sum gate
        pair_sum = round(up_ask + down_ask, 4)
        net_edge = round(1.0 - pair_sum, 4)
        if pair_sum >= self.target_sum_max:
            return self._skip(
                f"pair_sum_too_high({pair_sum:.4f}>={self.target_sum_max})",
                secs_to_res, up_ask, down_ask, book_up.spread_pct, book_down.spread_pct,
            )

        # 6. Net edge positive (safety — pair_sum < 0.95 always passes this)
        if net_edge <= 0:
            return self._skip(
                "no_edge", secs_to_res, up_ask, down_ask,
                book_up.spread_pct, book_down.spread_pct,
            )

        return Signal(
            action="dual_entry", up_ask=up_ask, down_ask=down_ask,
            pair_sum=pair_sum, net_edge=net_edge,
            spread_up=book_up.spread_pct, spread_down=book_down.spread_pct,
            secs_to_res=secs_to_res, reason="ok",
        )

    # ── single side taker ──────────────────────────────────────────────────────

    def evaluate_single(
        self,
        book_up: BookSnapshot | None,
        book_down: BookSnapshot | None,
        secs_to_res: int,
        btc_mid: float = 0.0,
        btc_open: float = 0.0,
    ) -> Signal:
        """Single-side taker — one side chosen by forced_side or btc_open_delta."""
        # 1. Entry window
        if secs_to_res > self.entry_window_start or secs_to_res < self.entry_window_end:
            return self._skip("outside_entry_window", secs_to_res)

        # 2. Orderbook present?
        if book_up is None or book_down is None:
            return self._skip("no_orderbook", secs_to_res)

        up_ask = book_up.ask
        down_ask = book_down.ask

        # 3. Determine side
        delta_bps: float = 0.0
        if self.signal_mode == "forced_side":
            if not self.forced_side:
                return self._skip("no_forced_side_configured", secs_to_res,
                                  up_ask, down_ask, book_up.spread_pct, book_down.spread_pct)
            side = self.forced_side

        elif self.signal_mode == "btc_open_delta":
            if btc_open <= 0 or btc_mid <= 0:
                return self._skip("btc_price_unavailable", secs_to_res,
                                  up_ask, down_ask, book_up.spread_pct, book_down.spread_pct)
            delta_bps = round((btc_mid - btc_open) / btc_open * 10000, 1)
            if abs(delta_bps) < self.min_move_bps:
                return self._skip(
                    f"no_signal(delta={delta_bps:.1f}bps<{self.min_move_bps}bps)",
                    secs_to_res, up_ask, down_ask,
                    book_up.spread_pct, book_down.spread_pct,
                    delta_bps=delta_bps,
                )
            side = "up" if delta_bps > 0 else "down"

        else:
            return self._skip(f"unknown_signal_mode({self.signal_mode})", secs_to_res,
                              up_ask, down_ask, book_up.spread_pct, book_down.spread_pct)

        # 4. Spread gate for chosen side only
        book = book_up if side == "up" else book_down
        if book.spread_pct > self.max_spread_pct:
            return self._skip(
                f"spread_too_wide({side}={book.spread_pct:.1f}%>{self.max_spread_pct}%)",
                secs_to_res, up_ask, down_ask,
                book_up.spread_pct, book_down.spread_pct,
                side=side, delta_bps=delta_bps,
            )

        # 5. Depth gate for chosen side only
        if book.ask_size < self.min_depth:
            return self._skip(
                f"depth_low({side}={book.ask_size:.0f}<{self.min_depth})",
                secs_to_res, up_ask, down_ask,
                book_up.spread_pct, book_down.spread_pct,
                side=side, delta_bps=delta_bps,
            )

        # 6. Price cap
        entry_price = book.ask
        if entry_price > self.max_entry_price:
            return self._skip(
                f"price_too_high({side}_ask={entry_price:.4f}>{self.max_entry_price})",
                secs_to_res, up_ask, down_ask,
                book_up.spread_pct, book_down.spread_pct,
                side=side, entry_price=entry_price, delta_bps=delta_bps,
            )

        # All gates passed
        reason = (
            f"ok(side={side},entry={entry_price:.4f},delta={delta_bps:.1f}bps)"
            if self.signal_mode == "btc_open_delta"
            else f"ok(side={side},entry={entry_price:.4f},forced)"
        )
        return Signal(
            action="single_entry",
            up_ask=up_ask, down_ask=down_ask,
            pair_sum=0.0, net_edge=0.0,
            spread_up=book_up.spread_pct, spread_down=book_down.spread_pct,
            secs_to_res=secs_to_res, reason=reason,
            side=side, entry_price=entry_price, delta_bps=delta_bps,
        )

    # ── signal logging ─────────────────────────────────────────────────────────

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
            "delta_bps": signal.delta_bps,
        })
