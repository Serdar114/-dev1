"""
Risk Manager — kill conditions ve bankroll floor.

Kill koşulları:
  1. max_consecutive_losses ardışık kayıp → dur (varsayılan: 4)
  2. bankroll < bankroll_floor_pct × initial_bankroll → dur (varsayılan: %70)
  3. günlük max_daily_trades doldu → bugün dur (varsayılan: 5)

Pozisyon boyutlama:
  - min_shares: minimum share adedi (5)
  - Her trade sabit min_shares (büyüme için Kelly ileride eklenebilir)
"""

from datetime import datetime, timezone
import logger as log_module


class RiskManager:
    def __init__(self, config: dict, initial_bankroll: float | None = None):
        self.max_consecutive_losses: int = config.get("max_consecutive_losses", 4)
        self.bankroll_floor_pct: float = config.get("bankroll_floor_pct", 0.70)
        self.max_daily_trades: int = config.get("max_daily_trades", 5)
        self.min_shares: int = config.get("min_shares", 5)

        self._bankroll: float = config.get("bankroll", 30.0)
        self._initial_bankroll: float = initial_bankroll or self._bankroll
        self._floor: float = self._initial_bankroll * self.bankroll_floor_pct

        self._consecutive_losses: int = 0
        self._daily_trades: int = 0
        self._trade_day: str = ""  # YYYY-MM-DD
        self._killed: bool = False
        self._kill_reason: str = ""

    @property
    def bankroll(self) -> float:
        return self._bankroll

    @property
    def is_killed(self) -> bool:
        return self._killed

    @property
    def kill_reason(self) -> str:
        return self._kill_reason

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _reset_daily_if_needed(self) -> None:
        today = self._today()
        if self._trade_day != today:
            self._trade_day = today
            self._daily_trades = 0

    def can_trade(self) -> tuple[bool, str]:
        """
        Trade açılabilir mi?
        Returns (bool, reason).
        """
        if self._killed:
            return False, self._kill_reason

        self._reset_daily_if_needed()

        if self._daily_trades >= self.max_daily_trades:
            return False, f"daily_limit_reached({self._daily_trades}/{self.max_daily_trades})"

        if self._bankroll < self._floor:
            self._kill("bankroll_floor_breached")
            return False, self._kill_reason

        if self._consecutive_losses >= self.max_consecutive_losses:
            self._kill(f"consecutive_losses({self._consecutive_losses})")
            return False, self._kill_reason

        return True, "ok"

    def get_shares(self) -> int:
        """Trade başına share adedi."""
        return self.min_shares

    def on_trade_opened(self) -> None:
        """Trade açıldığında çağır."""
        self._reset_daily_if_needed()
        self._daily_trades += 1

    def on_trade_result(self, pnl: float) -> None:
        """
        Trade kapandığında çağır.
        pnl: pozitif = kâr, negatif = zarar (USDC)
        """
        self._bankroll += pnl

        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        # Kill kontrolleri
        if self._bankroll < self._floor:
            self._kill("bankroll_floor_breached")

        if self._consecutive_losses >= self.max_consecutive_losses:
            self._kill(f"consecutive_losses({self._consecutive_losses})")

    def _kill(self, reason: str) -> None:
        if not self._killed:
            self._killed = True
            self._kill_reason = reason

    async def log_state(self) -> None:
        await log_module.log("risk_state", {
            "bankroll": round(self._bankroll, 4),
            "initial": round(self._initial_bankroll, 4),
            "floor": round(self._floor, 4),
            "consecutive_losses": self._consecutive_losses,
            "daily_trades": self._daily_trades,
            "max_daily": self.max_daily_trades,
            "killed": self._killed,
            "kill_reason": self._kill_reason,
        })

    def summary(self) -> dict:
        return {
            "bankroll": round(self._bankroll, 4),
            "consecutive_losses": self._consecutive_losses,
            "daily_trades": self._daily_trades,
            "killed": self._killed,
            "kill_reason": self._kill_reason,
        }
