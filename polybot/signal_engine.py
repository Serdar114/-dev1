"""
signal_engine.py — Delta-based signal generation for BTC 5-min Up/Down markets.

Converts Binance BTC price delta into a directional probability, compares
against Polymarket implied odds, and decides whether to trade.

The delta_to_prob lookup table is a PLACEHOLDER — calibrate from paper data.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from polymarket_client import calc_fee

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    timestamp: float
    market_slug: str
    seconds_to_expiry: float
    btc_spot: float
    btc_open: float
    delta_pct: float
    implied_prob: float          # YES implied price from Polymarket
    model_prob: float            # Our model probability
    edge: float
    fee_pct: float
    action: str                  # "UP" | "DOWN" | "NO_TRADE"
    trade_price: float           # Price at which we would trade
    reason: str                  # Why we traded or didn't
    side: str                    # "YES" | "NO" | ""


class SignalEngine:
    def __init__(self, config: dict):
        self._cfg = config["signal"]
        self._fee_cfg = config["fee"]

        entry = self._cfg["entry_window_sec"]
        self._entry_max: int = entry[0]   # 30
        self._entry_min: int = entry[1]   # 5

        self._min_delta: float = self._cfg["min_delta_pct"]
        self._price_band: tuple[float, float] = tuple(self._cfg["price_band"])
        self._min_edge: float = self._cfg["min_edge"]

        # Parse and sort delta_to_prob thresholds (ascending)
        raw = self._cfg["delta_to_prob"]
        self._delta_levels: list[tuple[float, float]] = sorted(
            [(float(k), float(v)) for k, v in raw.items()], key=lambda x: x[0]
        )
        logger.info(
            "SignalEngine initialized. entry_window=[%d,%d]s min_delta=%.3f%% "
            "min_edge=%.3f price_band=%s delta_levels=%s",
            self._entry_min,
            self._entry_max,
            self._min_delta,
            self._min_edge,
            self._price_band,
            self._delta_levels,
        )

    def evaluate(
        self,
        btc_current: float,
        btc_open: float,
        implied_yes_price: float,
        seconds_to_expiry: float,
        market_slug: str = "",
    ) -> SignalResult:
        """
        Evaluate whether to trade this window.
        Returns a SignalResult with action="NO_TRADE" if no trade is warranted.
        """
        ts = time.time()

        def no_trade(reason: str, delta_pct: float = 0.0) -> SignalResult:
            return SignalResult(
                timestamp=ts,
                market_slug=market_slug,
                seconds_to_expiry=seconds_to_expiry,
                btc_spot=btc_current,
                btc_open=btc_open,
                delta_pct=delta_pct,
                implied_prob=implied_yes_price,
                model_prob=0.0,
                edge=0.0,
                fee_pct=0.0,
                action="NO_TRADE",
                trade_price=0.0,
                reason=reason,
                side="",
            )

        # ---- 1. Timing check ----
        if seconds_to_expiry > self._entry_max or seconds_to_expiry < self._entry_min:
            return no_trade(
                f"outside_entry_window:{seconds_to_expiry:.1f}s "
                f"(window={self._entry_min}-{self._entry_max}s)"
            )

        # ---- 2. Delta calculation ----
        if btc_open <= 0:
            return no_trade("btc_open_invalid")
        delta_pct = (btc_current - btc_open) / btc_open * 100.0

        # ---- 3. Delta → model probability ----
        abs_delta = abs(delta_pct)
        model_prob = self._lookup_prob(abs_delta)
        if model_prob is None:
            return no_trade(f"delta_too_small:{delta_pct:.4f}%", delta_pct)

        # ---- 4. Direction ----
        side = "YES" if delta_pct > 0 else "NO"
        action = "UP" if delta_pct > 0 else "DOWN"

        # ---- 5. Trade price ----
        if side == "YES":
            trade_price = implied_yes_price
        else:
            trade_price = 1.0 - implied_yes_price

        # ---- 6. Price band check ----
        lo, hi = self._price_band
        if trade_price < lo or trade_price > hi:
            return SignalResult(
                timestamp=ts,
                market_slug=market_slug,
                seconds_to_expiry=seconds_to_expiry,
                btc_spot=btc_current,
                btc_open=btc_open,
                delta_pct=delta_pct,
                implied_prob=implied_yes_price,
                model_prob=model_prob,
                edge=0.0,
                fee_pct=0.0,
                action="NO_TRADE",
                trade_price=trade_price,
                reason=f"price_out_of_band:{trade_price:.4f} band=[{lo},{hi}]",
                side=side,
            )

        # ---- 7. Edge calculation ----
        fee_pct = calc_fee(
            trade_price,
            fee_rate=self._fee_cfg["fee_rate"],
            exponent=self._fee_cfg["exponent"],
        )
        edge = model_prob - trade_price - fee_pct

        # ---- 8. Edge check ----
        if edge < self._min_edge:
            return SignalResult(
                timestamp=ts,
                market_slug=market_slug,
                seconds_to_expiry=seconds_to_expiry,
                btc_spot=btc_current,
                btc_open=btc_open,
                delta_pct=delta_pct,
                implied_prob=implied_yes_price,
                model_prob=model_prob,
                edge=edge,
                fee_pct=fee_pct,
                action="NO_TRADE",
                trade_price=trade_price,
                reason=f"edge_low:{edge:.4f} min={self._min_edge}",
                side=side,
            )

        # ---- 9. Trade! ----
        logger.info(
            "SIGNAL %s | slug=%s tte=%.1fs delta=%.4f%% model=%.3f "
            "implied=%.3f trade_price=%.3f fee=%.4f edge=%.4f",
            action,
            market_slug,
            seconds_to_expiry,
            delta_pct,
            model_prob,
            implied_yes_price,
            trade_price,
            fee_pct,
            edge,
        )
        return SignalResult(
            timestamp=ts,
            market_slug=market_slug,
            seconds_to_expiry=seconds_to_expiry,
            btc_spot=btc_current,
            btc_open=btc_open,
            delta_pct=delta_pct,
            implied_prob=implied_yes_price,
            model_prob=model_prob,
            edge=edge,
            fee_pct=fee_pct,
            action=action,
            trade_price=trade_price,
            reason="signal_ok",
            side=side,
        )

    def _lookup_prob(self, abs_delta: float) -> Optional[float]:
        """
        Look up model probability from delta thresholds (config-driven).
        Returns None if abs_delta is below the minimum threshold.
        NOTE: This table is a PLACEHOLDER — calibrate with paper data.
        """
        result = None
        for threshold, prob in self._delta_levels:
            if abs_delta >= threshold:
                result = prob
            else:
                break
        return result
