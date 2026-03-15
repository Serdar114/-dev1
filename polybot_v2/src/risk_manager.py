"""
Risk manager for polybot_v2.

Stateless rule-checker. Reads from RiskState (provided by caller).
Never modifies state — only returns allow/reject decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RiskState:
    """Mutable risk tracking state, owned by the caller (main loop)."""
    actions_this_window: int = 0
    shadow_quotes_this_window: int = 0
    consecutive_losses: int = 0
    cooldown_windows_remaining: int = 0
    # Position policy: track open paper trades in the current window
    open_paper_trades_this_window: int = 0
    # shadow: track how many theoretical fills we're "holding"
    shadow_notional_assumption: float = 0.0
    max_notional_per_trade: float = 5.0  # set from bankroll


@dataclass
class RiskDecision:
    allow: bool
    reason: str


class RiskManager:
    def __init__(
        self,
        max_actions_per_window: int,
        max_consecutive_losses: int,
        cooldown_windows: int,
        max_shadow_quotes_per_window: int,
        max_notional_per_trade: float,
        stale_binance_ms: int,
        stale_polymarket_ms: int,
        max_open_paper_trades_per_window: int = 1,
    ) -> None:
        self._max_actions = max_actions_per_window
        self._max_consec_losses = max_consecutive_losses
        self._cooldown_windows = cooldown_windows
        self._max_shadow_quotes = max_shadow_quotes_per_window
        self._max_notional = max_notional_per_trade
        self._stale_binance_ms = stale_binance_ms
        self._stale_polymarket_ms = stale_polymarket_ms
        # Position policy: enforce single open trade per window
        self._max_open_trades = max_open_paper_trades_per_window

    def check_taker(
        self,
        state: RiskState,
        binance_age_ms: float,
        polymarket_age_ms: float,
        proposed_notional: float,
    ) -> RiskDecision:
        """Check whether a selective taker paper trade is allowed."""
        if binance_age_ms > self._stale_binance_ms:
            return RiskDecision(False, f"stale_binance({binance_age_ms:.0f}ms)")

        if polymarket_age_ms > self._stale_polymarket_ms:
            return RiskDecision(False, f"stale_polymarket({polymarket_age_ms:.0f}ms)")

        if state.cooldown_windows_remaining > 0:
            return RiskDecision(False, f"cooldown({state.cooldown_windows_remaining}w)")

        if state.consecutive_losses >= self._max_consec_losses:
            return RiskDecision(False, f"consec_losses({state.consecutive_losses})")

        if state.actions_this_window >= self._max_actions:
            return RiskDecision(False, f"max_actions({state.actions_this_window})")

        # Position policy: only one open paper trade per window
        if state.open_paper_trades_this_window >= self._max_open_trades:
            return RiskDecision(
                False,
                f"position_policy:max_open_trades({state.open_paper_trades_this_window})",
            )

        if proposed_notional > self._max_notional:
            return RiskDecision(False, f"notional_too_large({proposed_notional:.2f}>{self._max_notional:.2f})")

        return RiskDecision(True, "ok")

    def check_shadow(
        self,
        state: RiskState,
        binance_age_ms: float,
        polymarket_age_ms: float,
    ) -> RiskDecision:
        """Check whether a shadow maker quote is allowed."""
        if binance_age_ms > self._stale_binance_ms:
            return RiskDecision(False, f"stale_binance({binance_age_ms:.0f}ms)")

        if polymarket_age_ms > self._stale_polymarket_ms:
            return RiskDecision(False, f"stale_polymarket({polymarket_age_ms:.0f}ms)")

        if state.shadow_quotes_this_window >= self._max_shadow_quotes:
            return RiskDecision(False, f"max_shadow_quotes({state.shadow_quotes_this_window})")

        return RiskDecision(True, "ok")

    def on_window_reset(self, state: RiskState) -> None:
        """Call at the start of each new window to reset per-window counters."""
        state.actions_this_window = 0
        state.shadow_quotes_this_window = 0
        state.open_paper_trades_this_window = 0
        state.shadow_notional_assumption = 0.0
        if state.cooldown_windows_remaining > 0:
            state.cooldown_windows_remaining -= 1

    def on_loss(self, state: RiskState) -> None:
        """Update state after a paper loss is confirmed."""
        state.consecutive_losses += 1
        if state.consecutive_losses >= self._max_consec_losses:
            state.cooldown_windows_remaining = self._cooldown_windows

    def on_win(self, state: RiskState) -> None:
        """Reset consecutive loss counter after a win."""
        state.consecutive_losses = 0
