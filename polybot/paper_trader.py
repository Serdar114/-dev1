"""
paper_trader.py — Paper trade simulation, resolution, and PnL tracking.

Simulates trade opens and resolves them when the window closes.
Tracks bankroll and per-trade results. Never sends real orders.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from polymarket_client import calc_fee
from signal_engine import SignalResult

logger = logging.getLogger(__name__)


@dataclass
class OpenTrade:
    window_ts: int            # Which 5-min window this trade belongs to
    market_slug: str
    action: str               # "UP" or "DOWN"
    side: str                 # "YES" or "NO"
    fill_price: float         # trade_price + slippage
    stake: float              # shares
    fee: float                # fee at open
    cost: float               # total cash out (fill_price * stake + fee)
    btc_open: float           # BTC price at window open
    delta_pct: float
    edge: float
    opened_at: float          # unix timestamp


@dataclass
class TradeResult:
    trade: OpenTrade
    btc_close: float
    outcome: str              # "UP" or "DOWN" (actual market result)
    result: str               # "WIN" or "LOSS"
    pnl: float
    bankroll_after: float
    resolved_at: float


class PaperTrader:
    def __init__(self, config: dict):
        self._stake: float = float(config["base_stake_shares"])
        self._slippage: float = float(config["paper"]["slippage_cents"])
        self._fee_cfg = config["fee"]
        self._bankroll: float = float(config["initial_bankroll"])
        self._initial_bankroll: float = self._bankroll

        self._open_trade: Optional[OpenTrade] = None
        self._open_prices: dict[int, float] = {}  # window_ts -> btc_open_price

        # For risk manager queries
        self.daily_pnl: float = 0.0
        self.consecutive_losses: int = 0
        self._last_day: int = 0  # tracks day rollover (date as int YYYYMMDD)
        self._last_cooldown_start: float = 0.0

        self.trade_history: list[TradeResult] = []

        logger.info(
            "PaperTrader initialized. bankroll=%.2f stake=%.1f shares slippage=%.3f",
            self._bankroll,
            self._stake,
            self._slippage,
        )

    # ------------------------------------------------------------------ #
    # Open price management
    # ------------------------------------------------------------------ #

    def record_open_price(self, window_ts: int, price: float) -> None:
        """Save the BTC open price for a window if not already recorded."""
        if window_ts not in self._open_prices:
            self._open_prices[window_ts] = price
            logger.info(
                "Open price recorded: window_ts=%d btc_open=%.2f", window_ts, price
            )
            # Prune old entries (keep only last 10 windows)
            if len(self._open_prices) > 10:
                oldest = min(self._open_prices)
                del self._open_prices[oldest]

    def has_open_price(self, window_ts: int) -> bool:
        return window_ts in self._open_prices

    def get_open_price(self, window_ts: int) -> Optional[float]:
        return self._open_prices.get(window_ts)

    # ------------------------------------------------------------------ #
    # Trade lifecycle
    # ------------------------------------------------------------------ #

    def has_open_trade(self) -> bool:
        return self._open_trade is not None

    @property
    def open_positions(self) -> int:
        return 1 if self._open_trade is not None else 0

    @property
    def bankroll(self) -> float:
        return self._bankroll

    def open_trade(self, signal: SignalResult, window_ts: int) -> Optional[OpenTrade]:
        """
        Simulate opening a paper trade from a signal.
        Returns the OpenTrade or None if preconditions fail.
        """
        if self._open_trade is not None:
            logger.warning(
                "Cannot open trade: already have open trade for %s",
                self._open_trade.market_slug,
            )
            return None

        fill_price = signal.trade_price + self._slippage
        fee = calc_fee(
            fill_price,
            fee_rate=self._fee_cfg["fee_rate"],
            exponent=self._fee_cfg["exponent"],
        ) * self._stake
        cost = fill_price * self._stake + fee

        if cost > self._bankroll:
            logger.warning(
                "Insufficient bankroll to open trade: cost=%.4f bankroll=%.4f",
                cost,
                self._bankroll,
            )
            return None

        trade = OpenTrade(
            window_ts=window_ts,
            market_slug=signal.market_slug,
            action=signal.action,
            side=signal.side,
            fill_price=fill_price,
            stake=self._stake,
            fee=fee,
            cost=cost,
            btc_open=signal.btc_open,
            delta_pct=signal.delta_pct,
            edge=signal.edge,
            opened_at=time.time(),
        )
        self._open_trade = trade
        logger.info(
            "PAPER TRADE OPENED | %s %s fill=%.4f stake=%.1f fee=%.4f cost=%.4f bankroll=%.4f",
            trade.action,
            trade.market_slug,
            fill_price,
            self._stake,
            fee,
            cost,
            self._bankroll,
        )
        return trade

    def resolve(self, btc_close: float) -> Optional[TradeResult]:
        """
        Resolve the open trade using btc_close as the settlement price.
        Updates bankroll, daily PnL, and consecutive loss counter.
        Returns TradeResult or None if no trade is open.
        """
        if self._open_trade is None:
            return None

        trade = self._open_trade

        # Determine actual market outcome
        if btc_close >= trade.btc_open:
            outcome = "UP"
        else:
            outcome = "DOWN"

        # PnL
        if trade.action == outcome:
            pnl = (1.0 - trade.fill_price) * trade.stake - trade.fee
            result_str = "WIN"
        else:
            pnl = -(trade.fill_price * trade.stake + trade.fee)
            result_str = "LOSS"

        self._bankroll += pnl
        self.daily_pnl += pnl

        # Consecutive loss tracking
        if result_str == "LOSS":
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        resolved_at = time.time()
        tr = TradeResult(
            trade=trade,
            btc_close=btc_close,
            outcome=outcome,
            result=result_str,
            pnl=pnl,
            bankroll_after=self._bankroll,
            resolved_at=resolved_at,
        )
        self.trade_history.append(tr)
        self._open_trade = None

        logger.info(
            "TRADE RESOLVED | %s action=%s outcome=%s btc_open=%.2f btc_close=%.2f "
            "pnl=%.4f bankroll=%.4f consec_losses=%d",
            trade.market_slug,
            trade.action,
            outcome,
            trade.btc_open,
            btc_close,
            pnl,
            self._bankroll,
            self.consecutive_losses,
        )
        return tr

    def reset_daily_pnl_if_new_day(self) -> None:
        """Call once per loop iteration to roll daily PnL at midnight UTC."""
        import datetime
        today = int(datetime.datetime.utcnow().strftime("%Y%m%d"))
        if today != self._last_day:
            if self._last_day != 0:
                logger.info(
                    "New UTC day — resetting daily_pnl (was %.4f)", self.daily_pnl
                )
            self.daily_pnl = 0.0
            self._last_day = today
