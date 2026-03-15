"""
Metrics aggregator for polybot_v2.

Collects session-level statistics.
Call update_* methods as events occur.
Dump via snapshot() for logging.
"""

from __future__ import annotations

import time
from typing import Any

from models import MetricsSnapshot, PaperTrade, ShadowQuote, SignalDecision


class MetricsCollector:
    def __init__(self) -> None:
        self._data = MetricsSnapshot()
        self._session_start = time.time()

    # ------------------------------------------------------------------ #
    # Signal events
    # ------------------------------------------------------------------ #

    def on_signal(self, decision: SignalDecision) -> None:
        self._data.total_signals += 1
        if decision.lane == "selective_taker":
            if decision.action == "PAPER_TRADE":
                self._data.taker_trade_count += 1
            else:
                self._data.taker_no_trade_count += 1
                reason = decision.reason or "unknown"
                self._data.no_trade_reasons[reason] = (
                    self._data.no_trade_reasons.get(reason, 0) + 1
                )
            if decision.after_fee_edge_yes != 0:
                self._data.edge_distribution.append(decision.after_fee_edge_yes)
            if decision.fair_yes_prob != 0:
                self._data.fair_prob_distribution.append(decision.fair_yes_prob)

        elif decision.lane == "maker_shadow":
            if decision.action == "SHADOW_QUOTE":
                self._data.shadow_quote_count += 1

    # ------------------------------------------------------------------ #
    # Shadow fill events
    # ------------------------------------------------------------------ #

    def on_shadow_fill(self, quote: ShadowQuote) -> None:
        if quote.fill_would_happen:
            self._data.shadow_fillable_count += 1

    # ------------------------------------------------------------------ #
    # Trade resolve events
    # ------------------------------------------------------------------ #

    def on_trade_resolved(self, trade: PaperTrade, bankroll: float) -> None:
        self._data.paper_pnl += trade.pnl
        self._data.bankroll_path.append({"ts": trade.ts_resolve, "bankroll": bankroll})
        if trade.pnl > 0:
            self._data.taker_win_count += 1
        elif trade.pnl < 0:
            self._data.taker_loss_count += 1

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        d = self._data
        win_rate = (
            d.taker_win_count / max(d.taker_win_count + d.taker_loss_count, 1)
        )
        edge_mean = (
            sum(d.edge_distribution) / len(d.edge_distribution)
            if d.edge_distribution else 0.0
        )
        return {
            "session_duration_s": round(time.time() - self._session_start, 1),
            "total_signals": d.total_signals,
            "taker_trade_count": d.taker_trade_count,
            "taker_no_trade_count": d.taker_no_trade_count,
            "no_trade_reasons": d.no_trade_reasons,
            "shadow_quote_count": d.shadow_quote_count,
            "shadow_fillable_count": d.shadow_fillable_count,
            "paper_pnl": round(d.paper_pnl, 4),
            "taker_win_count": d.taker_win_count,
            "taker_loss_count": d.taker_loss_count,
            "win_rate": round(win_rate, 3),
            "edge_mean": round(edge_mean, 4),
            "bankroll_snapshots": len(d.bankroll_path),
        }
