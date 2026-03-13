"""
core/bankroll.py — V21 bankroll ve risk yönetimi.

Kurallar:
  • Sabit stake: $5 (config.risk.stake_usd)
  • Drawdown >= 25% → bot paused
  • N loss in M saniye → bot paused
  • Max eş zamanlı pozisyon = 2
"""

import time
import utils.logger as logger_mod
from collections import deque

log = logger_mod.get("bankroll")


class Bankroll:
    def __init__(self, cfg: dict, state):
        self._cfg = cfg
        self._state = state
        risk = cfg.get("risk", {})

        self.stake_usd: float = risk.get("stake_usd", 5.0)
        self.max_open: int = risk.get("max_open_positions", 2)
        self.drawdown_pause_pct: float = risk.get("drawdown_pause_pct", 25.0)
        self.loss_streak_count: int = risk.get("loss_streak_count", 2)
        self.loss_streak_window_s: float = risk.get("loss_streak_window_s", 300.0)

        # Loss timestamp'lerini tut
        self._loss_times: deque = deque(maxlen=20)

        # Peak bankroll (drawdown hesabı için)
        self._peak: float = state.bankroll

        log.info(
            "Bankroll hazır: stake=$%.2f max_open=%d dd_pause=%.0f%% streak=%dx%ds",
            self.stake_usd, self.max_open,
            self.drawdown_pause_pct,
            self.loss_streak_count, self.loss_streak_window_s,
        )

    # ── Ana karar ────────────────────────────────────────────────────────────

    def can_open(self) -> tuple:
        """
        Yeni pozisyon açılabilir mi?
        Returns: (ok: bool, reason: str)
        """
        state = self._state

        if state.paused:
            return False, f"DURDURULDU: {state.pause_reason}"

        if len(state.open_positions) >= self.max_open:
            return False, f"Maks pozisyon ({self.max_open}) doldu"

        if state.bankroll < self.stake_usd:
            return False, f"Yetersiz bakiye (${state.bankroll:.2f} < ${self.stake_usd:.2f})"

        # Drawdown kontrolü
        dd = self._current_drawdown_pct()
        if dd >= self.drawdown_pause_pct:
            self._pause(f"Drawdown {dd:.1f}% ≥ {self.drawdown_pause_pct:.0f}%")
            return False, state.pause_reason

        # Loss streak kontrolü
        now = time.time()
        recent_losses = [t for t in self._loss_times if now - t <= self.loss_streak_window_s]
        if len(recent_losses) >= self.loss_streak_count:
            self._pause(
                f"Kayıp serisi: son {self.loss_streak_window_s:.0f}s içinde "
                f"{len(recent_losses)} tam kayıp"
            )
            return False, state.pause_reason

        return True, "OK"

    # ── Kayıp kaydı ──────────────────────────────────────────────────────────

    def record_loss(self) -> None:
        """Bir pozisyon zararla kapandığında çağrılır."""
        self._loss_times.append(time.time())

    # ── Peak güncelle ────────────────────────────────────────────────────────

    def update_peak(self) -> None:
        """Her pozisyon kapandıktan sonra çağrılır."""
        if self._state.bankroll > self._peak:
            self._peak = self._state.bankroll

    # ── Drawdown ─────────────────────────────────────────────────────────────

    def _current_drawdown_pct(self) -> float:
        if self._peak <= 0:
            return 0.0
        val = self._state.bankroll
        if val > self._peak:
            self._peak = val
        return max(0.0, (self._peak - val) / self._peak * 100.0)

    # ── Pause ────────────────────────────────────────────────────────────────

    def _pause(self, reason: str) -> None:
        self._state.paused = True
        self._state.pause_reason = reason
        self._state.bot_status = "DURDURULDU"
        log.warning("Bot durduruldu: %s", reason)
        self._state.log_event(f"⛔ PAUSE: {reason}")
