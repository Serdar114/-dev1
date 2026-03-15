"""
Metrics aggregator for polybot_v2.

Collects session-level statistics.
Call update_* methods as events occur.
Dump via snapshot() for logging or UI consumption.
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
                # Track stale rejects
                if "stale" in reason:
                    self._data.stale_reject_count += 1
                # Track spread/skew rejects
                if "wide_spread" in reason:
                    self._data.spread_reject_count += 1
                if "complement_skew" in reason:
                    self._data.skew_reject_count += 1

            if decision.after_fee_edge_yes != 0:
                self._data.edge_distribution.append(decision.after_fee_edge_yes)
            if decision.fair_yes_prob != 0:
                self._data.fair_prob_distribution.append(decision.fair_yes_prob)
            if decision.confidence_score != 0:
                self._data.confidence_distribution.append(decision.confidence_score)

        elif decision.lane == "maker_shadow":
            if decision.action == "SHADOW_QUOTE":
                self._data.shadow_quote_count += 1

    # ------------------------------------------------------------------ #
    # Shadow fill events
    # ------------------------------------------------------------------ #

    def on_shadow_fill(self, quote: ShadowQuote) -> None:
        """Called when a new quote is built (fill_status=pending or crossed)."""
        if quote.fill_status == "pending":
            self._data.shadow_pending_count += 1
        elif quote.fill_status == "crossed":
            self._data.shadow_crossed_count += 1
        # Legacy compat
        if quote.fill_would_happen:
            self._data.shadow_fillable_count += 1

    def on_shadow_state_change(self, quote: ShadowQuote) -> None:
        """Called when process_pending() yields a completed quote."""
        status = quote.fill_status
        if status == "filled":
            self._data.shadow_filled_count += 1
        elif status == "expired":
            self._data.shadow_expired_count += 1
        elif status == "adverse_fill":
            self._data.shadow_adverse_fill_count += 1
        elif status == "crossed":
            self._data.shadow_crossed_count += 1

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
        trades_total = d.taker_win_count + d.taker_loss_count
        win_rate = d.taker_win_count / max(trades_total, 1)
        edge_mean = (
            sum(d.edge_distribution) / len(d.edge_distribution)
            if d.edge_distribution else 0.0
        )
        confidence_mean = (
            sum(d.confidence_distribution) / len(d.confidence_distribution)
            if d.confidence_distribution else 0.0
        )
        stale_ratio = (
            d.stale_reject_count / max(d.taker_no_trade_count, 1)
        )
        top5_reasons = dict(
            sorted(d.no_trade_reasons.items(), key=lambda x: -x[1])[:5]
        )
        return {
            "session_duration_s": round(time.time() - self._session_start, 1),
            "total_signals": d.total_signals,
            "taker_trade_count": d.taker_trade_count,
            "taker_no_trade_count": d.taker_no_trade_count,
            "no_trade_reasons_top5": top5_reasons,
            "stale_reject_count": d.stale_reject_count,
            "stale_reject_ratio": round(stale_ratio, 3),
            "spread_reject_count": d.spread_reject_count,
            "skew_reject_count": d.skew_reject_count,
            "shadow_quote_count": d.shadow_quote_count,
            "shadow_pending_count": d.shadow_pending_count,
            "shadow_filled_count": d.shadow_filled_count,
            "shadow_expired_count": d.shadow_expired_count,
            "shadow_adverse_fill_count": d.shadow_adverse_fill_count,
            "shadow_crossed_count": d.shadow_crossed_count,
            "shadow_fillable_count": d.shadow_fillable_count,
            "paper_pnl": round(d.paper_pnl, 4),
            "taker_win_count": d.taker_win_count,
            "taker_loss_count": d.taker_loss_count,
            "win_rate": round(win_rate, 3),
            "edge_mean": round(edge_mean, 4),
            "confidence_mean": round(confidence_mean, 3),
            "bankroll_snapshots": len(d.bankroll_path),
        }
