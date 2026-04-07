"""
hypothetical_entry.py — Measurement-only hypothetical entry engine.

Design:
  For every window where no-trade rules PASS, compute what WOULD have happened.
  This is NOT paper trading. No positions are opened.
  This is measurement: record the hypothetical entry and wait for resolution.
  After resolution, compute hypothetical PnL and log it.

  For every window:
    1. Compute hypothetical UP entry (buy Up token at best ask)
    2. Compute hypothetical DOWN entry (buy Down token at best ask)
    3. Record both in measurement log
    4. After resolution, compare to Chainlink outcome
    5. Compute gross_pnl, net_pnl for the "correct" side's hypothetical

  Important:
    We do NOT pick a side at entry time. We compute BOTH and compare later.
    This gives maximum measurement information per window.
    Picking a side comes in bucket analysis (phase 6) and paper mode (phase 7).
"""

from __future__ import annotations
import dataclasses
import logging
import time
from typing import Optional

from loggingx.schemas import (
    HypotheticalEntry, FeatureVector, ResolutionOutcome,
)
from paper.fill_model import compute_fill
from signals.bucket_probe import assign_bucket, bucket_features

logger = logging.getLogger("polybot.hypothetical_entry")


class HypotheticalEntryEngine:
    """
    Computes hypothetical entries for measurement purposes.

    Job: for each eligible window tick, record what entry WOULD cost and pay.
    Input: FeatureVector, bucket_id, stake config
    Output: Two HypotheticalEntry objects (UP and DOWN), both logged
    Failure: if fill is invalid, entry is marked invalid and logged
    """

    def __init__(self, stake_usdc: float = 5.0):
        self._stake_usdc = stake_usdc

    def compute(
        self,
        fv: FeatureVector,
        no_trade_reason: Optional[str] = None,
    ) -> tuple[HypotheticalEntry, HypotheticalEntry]:
        """
        Compute both UP and DOWN hypothetical entries from the current feature vector.
        Returns (up_entry, down_entry).
        no_trade_reason is set on both if rules didn't pass.
        """
        bucket_id = assign_bucket(fv)

        up_entry   = self._build_entry(fv, "UP",   fv.up_token_id,   fv.up_best_ask,   bucket_id, no_trade_reason)
        down_entry = self._build_entry(fv, "DOWN", fv.down_token_id, fv.down_best_ask, bucket_id, no_trade_reason)

        return up_entry, down_entry

    def _build_entry(
        self,
        fv: FeatureVector,
        side: str,
        token_id: str,
        best_ask: Optional[float],
        bucket_id: str,
        no_trade_reason: Optional[str],
    ) -> HypotheticalEntry:
        fill = compute_fill(
            best_ask=best_ask,
            stake_usdc=self._stake_usdc,
            fee_rate=fv.fee_rate,
        )

        entry = HypotheticalEntry(
            condition_id=fv.condition_id,
            window_start_ts=fv.window_start_ts,
            window_end_ts=fv.window_end_ts,
            recorded_at=time.time(),
            side=side,
            entry_token_id=token_id,
            entry_price=fill["entry_price"] if fill["fill_valid"] else (best_ask or 0.0),
            fill_assumption=fill["fill_model"],
            fill_is_optimistic=fill["fill_is_optimistic"],
            stake_usdc=self._stake_usdc,
            fee_rate=fv.fee_rate or 0.0,
            effective_fee_usdc=fill["fee_usdc"] or 0.0,
            effective_cost_usdc=fill["effective_cost"] or 0.0,
            max_payout_usdc=fill["max_payout"] or 0.0,
            feature_snapshot=self._snapshot(fv),
            bucket_id=bucket_id,
            no_trade_reason=no_trade_reason,
        )

        return entry

    def resolve(
        self,
        entry: HypotheticalEntry,
        outcome: str,
    ) -> HypotheticalEntry:
        """
        Fill in resolution data on a hypothetical entry.
        outcome: ResolutionOutcome.UP | DOWN | UNRESOLVED
        """
        if outcome == ResolutionOutcome.UNRESOLVED:
            entry.outcome_known = False
            entry.actual_outcome = ResolutionOutcome.UNRESOLVED
            return entry

        entry.outcome_known = True
        entry.actual_outcome = outcome
        entry.hypothetical_correct = (entry.side == outcome)

        if entry.effective_cost_usdc > 0 and entry.max_payout_usdc > 0:
            entry.gross_pnl_usdc = (
                entry.max_payout_usdc - entry.effective_cost_usdc
                if entry.hypothetical_correct
                else -entry.effective_cost_usdc
            )
            entry.net_pnl_usdc = (
                entry.max_payout_usdc - entry.effective_cost_usdc - entry.effective_fee_usdc
                if entry.hypothetical_correct
                else -entry.effective_cost_usdc - entry.effective_fee_usdc
            )
        else:
            entry.gross_pnl_usdc = None
            entry.net_pnl_usdc = None

        return entry

    def _snapshot(self, fv: FeatureVector) -> dict:
        """Compact snapshot of the feature vector for audit."""
        return {
            "chainlink_now":      fv.chainlink_now,
            "chainlink_open":     fv.chainlink_open,
            "chainlink_delta_bps": fv.chainlink_delta_bps,
            "chainlink_freshness": fv.chainlink_freshness,
            "binance_mid":        ((fv.binance_bid + fv.binance_ask) / 2.0
                                   if fv.binance_bid and fv.binance_ask else None),
            "binance_freshness":  fv.binance_freshness,
            "secs_to_expiry":     fv.secs_to_expiry,
            "up_best_ask":        fv.up_best_ask,
            "down_best_ask":      fv.down_best_ask,
            "pair_sum":           fv.pair_sum_best_ask,
            "fee_rate":           fv.fee_rate,
            **bucket_features(fv),
        }
