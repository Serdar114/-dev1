"""
Paper executor for polybot_v2.

Simulates taker trade lifecycle:
  - open a paper trade
  - on window resolution: resolve with 1.0 (win) or 0.0 (loss)
  - compute pnl
  - update bankroll state

No real orders. No partial fills. Simple and deterministic.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from fee_engine import FeeEngine
from models import BankrollState, PaperTrade, SignalDecision
from stake_policy import StakePolicy

log = logging.getLogger(__name__)


class PaperExecutor:
    def __init__(
        self,
        fee_engine: FeeEngine,
        stake_policy: StakePolicy,
    ) -> None:
        self._fee = fee_engine
        self._stake = stake_policy
        self._open_trades: dict[str, PaperTrade] = {}

    # ------------------------------------------------------------------ #
    # Open
    # ------------------------------------------------------------------ #

    def open_trade(
        self,
        decision: SignalDecision,
        entry_price: float,
        bankroll_state: BankrollState,
    ) -> Optional[PaperTrade]:
        """
        Open a paper trade from a PAPER_TRADE decision.
        Returns None if sizing yields zero notional.
        """
        if decision.action != "PAPER_TRADE" or decision.chosen_side is None:
            return None

        stake = self._stake.compute(
            bankroll=bankroll_state.bankroll,
            entry_price=entry_price,
            last_drawdown=bankroll_state.drawdown,
            lane="selective_taker",
        )

        if stake.suggested_notional <= 0 or stake.suggested_shares <= 0:
            log.warning("Stake computed zero size, skipping trade")
            return None

        trade = PaperTrade(
            trade_id=str(uuid.uuid4())[:8],
            ts_open=decision.ts,
            side=decision.chosen_side,
            entry_price=entry_price,
            shares=stake.suggested_shares,
            notional=stake.suggested_notional,
            window_ts=decision.window_ts,
            fair_yes_at_entry=decision.fair_yes_prob,
            after_fee_edge_at_entry=(
                decision.after_fee_edge_yes
                if decision.chosen_side == "yes"
                else decision.after_fee_edge_no
            ),
        )

        self._open_trades[trade.trade_id] = trade
        log.info(
            "PAPER_TRADE opened: id=%s side=%s price=%.4f shares=%.4f notional=%.4f",
            trade.trade_id, trade.side, trade.entry_price,
            trade.shares, trade.notional,
        )
        return trade

    # ------------------------------------------------------------------ #
    # Resolve
    # ------------------------------------------------------------------ #

    def resolve_trade(
        self,
        trade: PaperTrade,
        outcome_yes: float,             # 1.0 if YES wins, 0.0 if NO wins
        bankroll_state: BankrollState,
    ) -> PaperTrade:
        """
        Resolve a paper trade at window end.
        outcome_yes: 1.0 means BTC ended higher (YES wins), else 0.0.
        """
        if trade.resolved:
            return trade

        if trade.side == "yes":
            outcome = outcome_yes
        else:
            outcome = 1.0 - outcome_yes

        pnl = self._fee.taker_after_fee_pnl_per_share(trade.entry_price, outcome)
        total_pnl = pnl * trade.shares

        trade.resolved = True
        trade.ts_resolve = time.time()
        trade.resolve_price = outcome
        trade.pnl = total_pnl

        bankroll_state.update(total_pnl)

        log.info(
            "PAPER_TRADE resolved: id=%s side=%s outcome=%.0f pnl=%.4f bankroll=%.4f",
            trade.trade_id, trade.side, outcome, total_pnl, bankroll_state.bankroll,
        )

        # Remove from open
        self._open_trades.pop(trade.trade_id, None)
        return trade

    def get_open_trades(self) -> list[PaperTrade]:
        return list(self._open_trades.values())

    def resolve_all_pending(
        self,
        outcome_yes: float,
        bankroll_state: BankrollState,
    ) -> list[PaperTrade]:
        """Resolve all open trades for the current window."""
        resolved = []
        for trade in list(self._open_trades.values()):
            resolved.append(
                self.resolve_trade(trade, outcome_yes, bankroll_state)
            )
        return resolved
