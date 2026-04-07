"""
paper_executor.py — Selective paper trade executor (Phase 2 only).

Design:
  This module is ONLY active in selective_paper mode.
  It is NOT active in measurement_only mode.
  It executes paper trades only on proven buckets (from analytics layer).
  Constraints:
    - One trade max per window
    - One open position max at any time
    - Must hold to resolution (no early exit)
    - Bucket must be proven (min observations + min net edge)
    - All standard no-trade rules must pass
    - Fee and fill are explicit

  This is a narrow validation layer, not a broad activity machine.
  Any deviation from proven buckets = no paper trade + explicit log.
"""

from __future__ import annotations
import logging
import time
import uuid
from typing import Dict, List, Optional

from loggingx.schemas import PaperTrade, FeatureVector, ResolutionOutcome, NoTradeReasonCode

logger = logging.getLogger("polybot.paper_executor")


class PaperExecutor:
    """
    Manages selective paper trades.

    Job: open/close paper trades on proven buckets within tight constraints.
    Input:
      - FeatureVector (must already have passed no_trade_rules)
      - bucket_id (must be in proven_buckets)
      - chosen_side ("UP" or "DOWN")
      - outcome (at resolution time)
    Output: PaperTrade record
    Failure: any constraint violation → no-trade + log
    """

    def __init__(
        self,
        max_open_positions: int = 1,
        max_stake_usdc: float = 5.0,
    ):
        self._max_open = max_open_positions
        self._max_stake = max_stake_usdc
        self._open_trades: Dict[str, PaperTrade] = {}   # trade_id → PaperTrade
        self._closed_trades: List[PaperTrade] = []

    def can_open(self, bucket_id: str, proven_buckets: set) -> tuple[bool, str]:
        """
        Check if a paper trade can be opened.
        Returns (can_open: bool, reason: str)
        """
        if len(self._open_trades) >= self._max_open:
            return False, NoTradeReasonCode.POSITION_LIMIT_REACHED

        if bucket_id not in proven_buckets:
            return False, NoTradeReasonCode.BUCKET_NOT_PROVEN

        return True, "ok"

    def open_trade(
        self,
        fv: FeatureVector,
        side: str,
        bucket_id: str,
        proven_buckets: set,
        rationale: str = "",
    ) -> Optional[PaperTrade]:
        """
        Open a paper trade if all constraints pass.
        Returns PaperTrade on success, None on any constraint failure.
        """
        can, reason = self.can_open(bucket_id, proven_buckets)
        if not can:
            logger.info(
                "[%s] Paper trade blocked: %s (bucket=%s)",
                fv.condition_id, reason, bucket_id,
            )
            return None

        if side == "UP":
            entry_price = fv.up_best_ask
            token_id    = fv.up_token_id
        elif side == "DOWN":
            entry_price = fv.down_best_ask
            token_id    = fv.down_token_id
        else:
            logger.error("[%s] Invalid side: %s", fv.condition_id, side)
            return None

        if entry_price is None:
            logger.warning("[%s] Paper trade blocked: entry_price is None for side=%s", fv.condition_id, side)
            return None

        if fv.fee_rate is None:
            logger.warning("[%s] Paper trade blocked: fee_rate is None", fv.condition_id)
            return None

        stake = min(self._max_stake, self._max_stake)
        fee   = fv.fee_rate * stake

        trade = PaperTrade(
            trade_id=str(uuid.uuid4())[:8],
            condition_id=fv.condition_id,
            window_start_ts=fv.window_start_ts,
            window_end_ts=fv.window_end_ts,
            side=side,
            entry_token_id=token_id,
            entry_price=entry_price,
            stake_usdc=stake,
            fee_usdc=fee,
            opened_at=time.time(),
            bucket_id=bucket_id,
            entry_rationale=rationale,
        )
        self._open_trades[trade.trade_id] = trade

        logger.info(
            "[%s] Paper trade OPEN: side=%s price=%.4f stake=%.2f fee=%.4f bucket=%s",
            fv.condition_id, side, entry_price, stake, fee, bucket_id,
        )
        return trade

    def close_trade(self, trade_id: str, outcome: str) -> Optional[PaperTrade]:
        """
        Close a paper trade at resolution.
        Returns updated PaperTrade or None if trade_id not found.
        """
        trade = self._open_trades.pop(trade_id, None)
        if trade is None:
            logger.warning("close_trade: trade_id %s not found", trade_id)
            return None

        trade.closed_at = time.time()
        trade.outcome = outcome
        trade.is_open = False

        if outcome == ResolutionOutcome.UNRESOLVED:
            trade.gross_pnl_usdc = None
            trade.net_pnl_usdc   = None
        else:
            correct = (trade.side == outcome)
            quantity = trade.stake_usdc / trade.entry_price

            if correct:
                payout           = quantity * 1.0
                trade.gross_pnl_usdc = payout - trade.stake_usdc
                trade.net_pnl_usdc   = payout - trade.stake_usdc - trade.fee_usdc
            else:
                trade.gross_pnl_usdc = -trade.stake_usdc
                trade.net_pnl_usdc   = -trade.stake_usdc - trade.fee_usdc

        self._closed_trades.append(trade)

        logger.info(
            "[%s] Paper trade CLOSE: side=%s outcome=%s net_pnl=%.4f",
            trade.condition_id, trade.side, outcome,
            trade.net_pnl_usdc if trade.net_pnl_usdc is not None else float("nan"),
        )
        return trade

    def close_all_for_market(self, condition_id: str, outcome: str) -> List[PaperTrade]:
        """Close all open trades for a given market (called at resolution)."""
        to_close = [
            tid for tid, t in self._open_trades.items()
            if t.condition_id == condition_id
        ]
        closed = []
        for tid in to_close:
            t = self.close_trade(tid, outcome)
            if t:
                closed.append(t)
        return closed

    def open_trades(self) -> List[PaperTrade]:
        return list(self._open_trades.values())

    def closed_trades(self) -> List[PaperTrade]:
        return list(self._closed_trades)

    def has_open_position_for(self, condition_id: str) -> bool:
        return any(t.condition_id == condition_id for t in self._open_trades.values())

    def total_open_count(self) -> int:
        return len(self._open_trades)
