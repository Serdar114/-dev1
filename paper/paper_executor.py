"""
paper/paper_executor.py — Paper trade executor (Phase 2 only).

NOT ACTIVATED in measurement-only mode.
Gating: config.paper.enabled must be True.
Constraints: max 1 trade per window, max 1 open position.

In measurement mode, this module is imported but .execute() is never called.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)


@dataclass
class PaperTrade:
    window_id: int
    side: str
    entry_price: float
    entry_ts: float
    fee_rate: float
    fee_provenance: str
    net_payoff_if_win: float
    net_payoff_if_lose: float
    size_usdc: float
    outcome: Optional[str] = None       # "Up" | "Down" | None
    resolution_status: str = "open"     # "open" | "resolved" | "unresolved"
    pnl_usdc: Optional[float] = None
    resolved_at: Optional[float] = None


class PaperExecutor:
    """
    Manages paper positions.
    Thread-safe. Never opens more than 1 position per window.
    """

    def __init__(self, config: dict) -> None:
        p = config.get("paper", {})
        self._enabled: bool = bool(p.get("enabled", False))
        self._max_per_window: int = int(p.get("max_per_window", 1))
        self._max_open: int = int(p.get("max_open_positions", 1))
        self._lock = threading.Lock()
        self._trades: List[PaperTrade] = []
        self._trades_this_window: int = 0
        self._current_window: int = 0

    def can_trade(self, window_id: int) -> bool:
        with self._lock:
            if not self._enabled:
                return False
            open_count = sum(1 for t in self._trades if t.resolution_status == "open")
            if open_count >= self._max_open:
                return False
            if window_id == self._current_window and self._trades_this_window >= self._max_per_window:
                return False
            return True

    def execute(self, window_id: int, side: str, entry_price: float,
                fee_rate: float, fee_provenance: str,
                net_payoff_if_win: float, net_payoff_if_lose: float,
                size_usdc: float = 1.0) -> Optional[PaperTrade]:
        """
        Open a paper trade if conditions allow.
        Returns the PaperTrade or None if blocked.
        """
        with self._lock:
            if not self.can_trade(window_id):
                return None
            trade = PaperTrade(
                window_id=window_id,
                side=side,
                entry_price=entry_price,
                entry_ts=time.time(),
                fee_rate=fee_rate,
                fee_provenance=fee_provenance,
                net_payoff_if_win=net_payoff_if_win,
                net_payoff_if_lose=net_payoff_if_lose,
                size_usdc=size_usdc,
            )
            self._trades.append(trade)
            if window_id != self._current_window:
                self._current_window = window_id
                self._trades_this_window = 0
            self._trades_this_window += 1
            log.info(
                "Paper trade opened: window=%d side=%s price=%.4f",
                window_id, side, entry_price,
            )
            return trade

    def resolve(self, window_id: int, outcome: str, price_at_end: float) -> None:
        """Mark open trades for this window as resolved."""
        with self._lock:
            for trade in self._trades:
                if trade.window_id == window_id and trade.resolution_status == "open":
                    if outcome in ("Up", "Down"):
                        won = trade.side == outcome
                        trade.outcome = outcome
                        trade.resolution_status = "resolved"
                        per_unit = trade.net_payoff_if_win if won else trade.net_payoff_if_lose
                        trade.pnl_usdc = per_unit * trade.size_usdc
                        trade.resolved_at = time.time()
                    else:
                        trade.resolution_status = "unresolved"
                        trade.resolved_at = time.time()

    def mark_unresolved(self, window_id: int) -> None:
        """Mark trades as unresolved when truth is unavailable."""
        with self._lock:
            for trade in self._trades:
                if trade.window_id == window_id and trade.resolution_status == "open":
                    trade.resolution_status = "unresolved"
                    trade.resolved_at = time.time()

    def summary(self) -> dict:
        with self._lock:
            resolved = [t for t in self._trades if t.resolution_status == "resolved"]
            wins = [t for t in resolved if t.outcome == t.side]
            total_pnl = sum(t.pnl_usdc for t in resolved if t.pnl_usdc is not None)
            return {
                "total_trades": len(self._trades),
                "resolved": len(resolved),
                "wins": len(wins),
                "win_rate": len(wins) / len(resolved) if resolved else None,
                "total_pnl_usdc": total_pnl,
            }
