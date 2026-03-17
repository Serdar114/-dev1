"""
Metrics aggregator for polybot_v2.

Phase 2: maker-first evaluation metrics alongside taker baseline.
Call update_* methods as events occur.
Dump via snapshot() for logging or UI consumption.

Taker = benchmark lane.
Maker = primary evaluation target (Phase 2).
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
                if "stale" in reason:
                    self._data.stale_reject_count += 1
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
                # Count in both legacy and Phase 2 counters
                self._data.shadow_quote_count += 1
                self._data.maker_quote_count += 1
            else:
                self._data.maker_no_quote_count += 1

    # ------------------------------------------------------------------ #
    # Shadow fill events (legacy compatibility)
    # ------------------------------------------------------------------ #

    def on_shadow_fill(self, quote: ShadowQuote) -> None:
        """Called when a new quote is built (fill_status=pending or crossed_rejected)."""
        if quote.fill_status == "pending":
            self._data.shadow_pending_count += 1
            self._data.maker_pending_count += 1
        elif quote.fill_status == "crossed_rejected":
            self._data.shadow_crossed_count += 1
            self._data.maker_crossed_reject_count += 1

        # Track expected edge
        if quote.intended_passive_edge is not None:
            self._data.maker_expected_edge_list.append(quote.intended_passive_edge)

    def on_shadow_state_change(self, quote: ShadowQuote) -> None:
        """Called when process_pending() yields a completed quote.

        NOTE: process_pending() emits the same physical quote TWICE for a full-lifecycle
        fill: first as "filled" (intermediate, queued for next-tick measurement) and then
        as "filled_adverse" or "filled_favorable" (terminal, after next-tick measurement).
        To avoid double-counting, fill counts are incremented ONLY at terminal states.
        Quotes that hit boundary while still in "filled" state (never measured) are
        counted separately in on_boundary_resolved.
        """
        status = quote.fill_status
        # --- Legacy shadow counters (kept for compat) ---
        # Count only terminal fill states to avoid double-counting with "filled" intermediate.
        if status in ("filled_adverse", "filled_favorable"):
            self._data.shadow_filled_count += 1
        if status == "filled_adverse":
            self._data.shadow_adverse_fill_count += 1
        elif status == "expired_unfilled":
            self._data.shadow_expired_count += 1
        elif status == "crossed_rejected":
            self._data.shadow_crossed_count += 1

        # --- Phase 2 maker metrics ---
        # Count only terminal fill states ("filled_adverse", "filled_favorable").
        # "filled" is an intermediate state; the same quote will emit a terminal state
        # on the next tick. Boundary-cut fills (status="filled" at window end) are
        # counted in on_boundary_resolved instead.
        if status in ("filled_adverse", "filled_favorable"):
            self._data.maker_fill_count += 1
        if status == "filled_adverse":
            self._data.maker_adverse_fill_count += 1
        elif status == "filled_favorable":
            self._data.maker_favorable_fill_count += 1
        elif status == "expired_unfilled":
            self._data.maker_expired_count += 1

    # ------------------------------------------------------------------ #
    # Boundary resolution events (Phase 2)
    # ------------------------------------------------------------------ #

    def on_boundary_resolved(self, quote: ShadowQuote) -> None:
        """
        Called for each quote returned from MakerShadowProbe.resolve_boundary().
        Updates boundary outcome tracking and PnL-if-held aggregation.

        Boundary-cut fills: quotes that fill but hit the window boundary before the
        next-tick adverse/favorable measurement completes keep fill_status="filled".
        These are not counted by on_shadow_state_change (which only counts terminal
        states), so we count them here exactly once.
        """
        self._data.maker_boundary_resolved_count += 1

        # Count fills that were boundary-cut before quality measurement.
        # fill_status="filled" means the quote filled but the window ended before
        # the next tick could classify it as filled_adverse or filled_favorable.
        if quote.fill_status == "filled":
            self._data.maker_fill_count += 1
            self._data.shadow_filled_count += 1

        outcome = quote.boundary_outcome_for_side
        if outcome is not None:
            if outcome >= 1.0:
                self._data.maker_boundary_win_count += 1
            elif outcome <= 0.0:
                self._data.maker_boundary_loss_count += 1

        if quote.maker_pnl_if_held is not None:
            self._data.maker_pnl_if_held_list.append(quote.maker_pnl_if_held)

        if quote.maker_edge_realized_vs_expected is not None:
            self._data.maker_realized_edge_list.append(quote.maker_edge_realized_vs_expected)

        # By-side breakdown
        side = quote.side
        if side not in self._data.maker_by_side:
            self._data.maker_by_side[side] = _empty_breakdown()
        _update_breakdown(self._data.maker_by_side[side], quote)

        # By-regime breakdown
        regime = quote.regime or "UNKNOWN"
        if regime not in self._data.maker_by_regime:
            self._data.maker_by_regime[regime] = _empty_breakdown()
        _update_breakdown(self._data.maker_by_regime[regime], quote)

        # By-pattern breakdown
        pattern = quote.pattern or "UNKNOWN"
        if pattern not in self._data.maker_by_pattern:
            self._data.maker_by_pattern[pattern] = _empty_breakdown()
        _update_breakdown(self._data.maker_by_pattern[pattern], quote)

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

        # --- Taker metrics (baseline) ---
        trades_total = d.taker_win_count + d.taker_loss_count
        taker_win_rate = d.taker_win_count / max(trades_total, 1)
        edge_mean = (
            sum(d.edge_distribution) / len(d.edge_distribution)
            if d.edge_distribution else 0.0
        )
        confidence_mean = (
            sum(d.confidence_distribution) / len(d.confidence_distribution)
            if d.confidence_distribution else 0.0
        )
        stale_ratio = d.stale_reject_count / max(d.taker_no_trade_count, 1)
        top5_reasons = dict(
            sorted(d.no_trade_reasons.items(), key=lambda x: -x[1])[:5]
        )

        # --- Maker metrics (primary evaluation lane) ---
        total_placed = d.maker_pending_count + d.maker_crossed_reject_count
        maker_fill_rate = (
            d.maker_fill_count / max(d.maker_pending_count, 1)
        )
        maker_expiry_rate = (
            d.maker_expired_count / max(d.maker_pending_count, 1)
        )
        maker_adverse_ratio = (
            d.maker_adverse_fill_count / max(d.maker_fill_count, 1)
        )
        boundary_total = d.maker_boundary_win_count + d.maker_boundary_loss_count
        maker_boundary_win_rate = (
            d.maker_boundary_win_count / max(boundary_total, 1)
            if boundary_total > 0 else None
        )
        pnl_list = d.maker_pnl_if_held_list
        maker_pnl_total = sum(pnl_list) if pnl_list else 0.0
        maker_pnl_avg = sum(pnl_list) / len(pnl_list) if pnl_list else None
        exp_list = d.maker_expected_edge_list
        maker_expected_edge_mean = sum(exp_list) / len(exp_list) if exp_list else None
        real_list = d.maker_realized_edge_list
        maker_realized_edge_mean = sum(real_list) / len(real_list) if real_list else None

        return {
            "session_duration_s": round(time.time() - self._session_start, 1),
            "total_signals": d.total_signals,
            # --- TAKER (benchmark lane) ---
            "taker_trade_count": d.taker_trade_count,
            "taker_no_trade_count": d.taker_no_trade_count,
            "no_trade_reasons_top5": top5_reasons,
            "stale_reject_count": d.stale_reject_count,
            "stale_reject_ratio": round(stale_ratio, 3),
            "spread_reject_count": d.spread_reject_count,
            "skew_reject_count": d.skew_reject_count,
            "paper_pnl": round(d.paper_pnl, 4),
            "taker_win_count": d.taker_win_count,
            "taker_loss_count": d.taker_loss_count,
            "taker_win_rate": round(taker_win_rate, 3),
            "edge_mean": round(edge_mean, 4),
            "confidence_mean": round(confidence_mean, 3),
            "bankroll_snapshots": len(d.bankroll_path),
            # --- MAKER (primary evaluation lane) ---
            "maker_quote_count": d.maker_quote_count,
            "maker_no_quote_count": d.maker_no_quote_count,
            "maker_pending_count": d.maker_pending_count,
            "maker_crossed_reject_count": d.maker_crossed_reject_count,
            "maker_fill_count": d.maker_fill_count,
            "maker_fill_rate": round(maker_fill_rate, 3),
            "maker_expired_count": d.maker_expired_count,
            "maker_expiry_rate": round(maker_expiry_rate, 3),
            "maker_adverse_fill_count": d.maker_adverse_fill_count,
            "maker_favorable_fill_count": d.maker_favorable_fill_count,
            "maker_adverse_fill_ratio": round(maker_adverse_ratio, 3),
            "maker_boundary_resolved_count": d.maker_boundary_resolved_count,
            "maker_boundary_win_count": d.maker_boundary_win_count,
            "maker_boundary_loss_count": d.maker_boundary_loss_count,
            "maker_boundary_win_rate": (
                round(maker_boundary_win_rate, 3)
                if maker_boundary_win_rate is not None else None
            ),
            "maker_pnl_if_held_total": round(maker_pnl_total, 5),
            "maker_pnl_if_held_avg": (
                round(maker_pnl_avg, 5) if maker_pnl_avg is not None else None
            ),
            "maker_expected_edge_mean": (
                round(maker_expected_edge_mean, 5)
                if maker_expected_edge_mean is not None else None
            ),
            "maker_realized_edge_mean": (
                round(maker_realized_edge_mean, 5)
                if maker_realized_edge_mean is not None else None
            ),
            "maker_by_side": d.maker_by_side,
            "maker_by_regime": d.maker_by_regime,
            "maker_by_pattern": d.maker_by_pattern,
            # Legacy compat keys
            "shadow_quote_count": d.shadow_quote_count,
            "shadow_pending_count": d.shadow_pending_count,
            "shadow_filled_count": d.shadow_filled_count,
            "shadow_expired_count": d.shadow_expired_count,
            "shadow_adverse_fill_count": d.shadow_adverse_fill_count,
            "shadow_crossed_count": d.shadow_crossed_count,
        }


# ------------------------------------------------------------------ #
# Breakdown helpers (by-side / by-regime / by-pattern)
# ------------------------------------------------------------------ #

def _empty_breakdown() -> dict:
    return {
        "quote_count": 0,
        "fill_count": 0,
        "adverse_fill_count": 0,
        "boundary_win_count": 0,
        "boundary_loss_count": 0,
        "pnl_if_held_total": 0.0,
    }


def _update_breakdown(bd: dict, quote: ShadowQuote) -> None:
    bd["quote_count"] += 1
    if quote.fill_status in ("filled", "filled_adverse", "filled_favorable"):
        bd["fill_count"] += 1
    if quote.fill_status == "filled_adverse":
        bd["adverse_fill_count"] += 1
    outcome = quote.boundary_outcome_for_side
    if outcome is not None:
        if outcome >= 1.0:
            bd["boundary_win_count"] += 1
        elif outcome <= 0.0:
            bd["boundary_loss_count"] += 1
    if quote.maker_pnl_if_held is not None:
        bd["pnl_if_held_total"] = round(
            bd.get("pnl_if_held_total", 0.0) + quote.maker_pnl_if_held, 6
        )
