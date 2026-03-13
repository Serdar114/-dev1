"""
risk_manager.py — Pre-trade risk checks for paper trading.

Checks are performed in order; the first failure rejects the trade.
All rejections are logged with their reason.
"""

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RiskCheckResult:
    ok: bool
    reason: str


class RiskManager:
    def __init__(self, config: dict):
        self._max_positions: int = config["risk"]["max_open_positions"]
        self._max_daily_loss: float = config["risk"]["max_daily_loss"]
        self._cooldown_count: int = config["risk"]["consecutive_loss_cooldown_count"]
        self._cooldown_min: float = config["risk"]["consecutive_loss_cooldown_min"]
        self._cooldown_until: float = 0.0  # unix timestamp; 0 = no cooldown active

        logger.info(
            "RiskManager initialized. max_positions=%d max_daily_loss=%.2f "
            "cooldown_after=%d_losses cooldown_min=%.0f",
            self._max_positions,
            self._max_daily_loss,
            self._cooldown_count,
            self._cooldown_min,
        )

    def check(self, signal, feed, trader) -> RiskCheckResult:
        """
        Run all pre-trade checks against the current system state.

        Parameters
        ----------
        signal : SignalResult
        feed   : BinanceFeed
        trader : PaperTrader
        """

        # 1. Stale data check
        if feed.is_stale:
            return self._reject("stale_data")

        # 2. Implied price sanity (signal.trade_price should be > 0)
        if signal.trade_price <= 0:
            return self._reject("no_market_data")

        # 3. Sufficient bankroll
        from polymarket_client import calc_fee
        fill_price = signal.trade_price + 0.01  # slippage already known at signal time
        fee = calc_fee(fill_price) * trader._stake
        stake_cost = fill_price * trader._stake + fee
        if trader.bankroll < stake_cost:
            return self._reject(
                f"insufficient_balance:bankroll={trader.bankroll:.4f} "
                f"need={stake_cost:.4f}"
            )

        # 4. Max open positions
        if trader.open_positions >= self._max_positions:
            return self._reject(
                f"position_open:open={trader.open_positions} max={self._max_positions}"
            )

        # 5. Daily loss limit
        if trader.daily_pnl <= -abs(self._max_daily_loss):
            return self._reject(
                f"daily_loss_limit:daily_pnl={trader.daily_pnl:.4f} "
                f"limit={-self._max_daily_loss:.4f}"
            )

        # 6. Consecutive loss cooldown
        now = time.time()
        if trader.consecutive_losses >= self._cooldown_count:
            if self._cooldown_until == 0.0:
                # Start cooldown
                self._cooldown_until = now + self._cooldown_min * 60
                logger.warning(
                    "Consecutive losses=%d >= %d. Cooldown active until %s.",
                    trader.consecutive_losses,
                    self._cooldown_count,
                    time.strftime("%H:%M:%S", time.localtime(self._cooldown_until)),
                )
            if now < self._cooldown_until:
                remaining = int(self._cooldown_until - now)
                return self._reject(
                    f"cooldown:consec_losses={trader.consecutive_losses} "
                    f"remaining={remaining}s"
                )
            else:
                # Cooldown expired
                logger.info(
                    "Cooldown expired. Consecutive losses reset from %d to 0.",
                    trader.consecutive_losses,
                )
                trader.consecutive_losses = 0
                self._cooldown_until = 0.0
        else:
            # Reset cooldown if loss streak ended externally (e.g. after a win)
            if self._cooldown_until > 0 and now >= self._cooldown_until:
                self._cooldown_until = 0.0

        return RiskCheckResult(ok=True, reason="ok")

    def _reject(self, reason: str) -> RiskCheckResult:
        logger.info("RiskManager REJECT: %s", reason)
        return RiskCheckResult(ok=False, reason=reason)
