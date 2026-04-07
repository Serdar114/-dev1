"""
resolution_truth.py — Canonical settlement truth for a market window.

Design:
  Settlement truth = Chainlink BTC/USD at window open vs window close.
  If EITHER capture is missing/stale, outcome = UNRESOLVED. Never guessed.
  Binance is NOT used as settlement truth at any point.
  outcome is only set when BOTH chainlink_open and chainlink_close are confirmed fresh.
  An unresolved outcome must log why it is unresolved.
"""

from __future__ import annotations
import logging
import time
from typing import Optional, TYPE_CHECKING

from loggingx.schemas import WindowTruth, ResolutionOutcome, ChainlinkPrice, FreshnessState
from truth.freshness import check_freshness

if TYPE_CHECKING:
    from loggingx.event_logger import EventLogger

logger = logging.getLogger("polybot.resolution_truth")


class ResolutionTruthTracker:
    """
    Tracks the canonical open/close Chainlink price for one market window.

    Job:
      - Record Chainlink price at window open
      - Record Chainlink price at window close
      - Compute outcome (UP/DOWN/UNRESOLVED) from those two captures

    Input:
      - condition_id, window_start_ts, window_end_ts
      - Chainlink price snapshots (provided by the feed layer at the right time)

    Output:
      - WindowTruth dataclass with all fields populated or explicitly None+error

    Failure:
      - If open is stale/missing: chainlink_open_ok=False, outcome=UNRESOLVED
      - If close is stale/missing: chainlink_close_ok=False, outcome=UNRESOLVED
      - Outcome is ONLY set to UP/DOWN when both captures succeed

    Non-canonical:
      - Binance prices are never used for resolution
      - Fallbacks are never used
    """

    def __init__(
        self,
        condition_id: str,
        window_start_ts: float,
        window_end_ts: float,
        chainlink_max_age_secs: float = 45.0,
    ):
        self.truth = WindowTruth(
            condition_id=condition_id,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
        )
        self._chainlink_max_age_secs = chainlink_max_age_secs

    def capture_open(self, chainlink: Optional[ChainlinkPrice]) -> None:
        """
        Called at window open time. Record Chainlink price if fresh.
        If chainlink is None or stale, marks open as failed.
        """
        if chainlink is None:
            self.truth.chainlink_open_ok = False
            self.truth.open_capture_error = "chainlink_missing_at_open"
            logger.warning("[%s] Open capture failed: Chainlink missing", self.truth.condition_id)
            return

        if chainlink.freshness != FreshnessState.FRESH:
            self.truth.chainlink_open_ok = False
            self.truth.open_capture_error = f"chainlink_{chainlink.freshness.lower()}_at_open"
            logger.warning(
                "[%s] Open capture failed: Chainlink %s (age=%.1fs)",
                self.truth.condition_id,
                chainlink.freshness,
                chainlink.age_secs(),
            )
            return

        self.truth.chainlink_open = chainlink.price_usd
        self.truth.chainlink_open_at = chainlink.fetched_at
        self.truth.chainlink_open_ok = True
        logger.info(
            "[%s] Open captured: Chainlink BTC/USD = %.2f at ts=%.3f",
            self.truth.condition_id,
            chainlink.price_usd,
            chainlink.fetched_at,
        )

    def capture_close(self, chainlink: Optional[ChainlinkPrice]) -> None:
        """
        Called at window close time. Record Chainlink price if fresh.
        Then compute outcome.
        """
        if chainlink is None:
            self.truth.chainlink_close_ok = False
            self.truth.close_capture_error = "chainlink_missing_at_close"
            self._set_unresolved("chainlink_missing_at_close")
            return

        if chainlink.freshness != FreshnessState.FRESH:
            self.truth.chainlink_close_ok = False
            self.truth.close_capture_error = f"chainlink_{chainlink.freshness.lower()}_at_close"
            self._set_unresolved(self.truth.close_capture_error)
            return

        self.truth.chainlink_close = chainlink.price_usd
        self.truth.chainlink_close_at = chainlink.fetched_at
        self.truth.chainlink_close_ok = True

        self._compute_outcome()

    def _compute_outcome(self) -> None:
        """
        Compute UP/DOWN only when both open and close are confirmed.
        If either is missing, stays UNRESOLVED.
        """
        if not self.truth.chainlink_open_ok:
            self._set_unresolved("open_not_captured")
            return

        if not self.truth.chainlink_close_ok:
            self._set_unresolved("close_not_captured")
            return

        if self.truth.chainlink_open is None or self.truth.chainlink_close is None:
            self._set_unresolved("price_fields_none_despite_ok_flags")
            return

        # UP = close > open at resolution time (Chainlink)
        if self.truth.chainlink_close > self.truth.chainlink_open:
            self.truth.outcome = ResolutionOutcome.UP
        else:
            # Ties go DOWN per typical Polymarket BTC 5m structure
            self.truth.outcome = ResolutionOutcome.DOWN

        self.truth.outcome_captured_at = time.time()

        logger.info(
            "[%s] Outcome = %s (open=%.2f, close=%.2f, delta=%.2f bps)",
            self.truth.condition_id,
            self.truth.outcome,
            self.truth.chainlink_open,
            self.truth.chainlink_close,
            self._delta_bps(),
        )

    def _delta_bps(self) -> float:
        if self.truth.chainlink_open and self.truth.chainlink_close and self.truth.chainlink_open != 0:
            return ((self.truth.chainlink_close - self.truth.chainlink_open)
                    / self.truth.chainlink_open * 10000.0)
        return 0.0

    def _set_unresolved(self, reason: str) -> None:
        self.truth.outcome = ResolutionOutcome.UNRESOLVED
        logger.warning(
            "[%s] Outcome UNRESOLVED: %s",
            self.truth.condition_id,
            reason,
        )

    def get_truth(self) -> WindowTruth:
        return self.truth

    def is_resolved(self) -> bool:
        return self.truth.outcome in (ResolutionOutcome.UP, ResolutionOutcome.DOWN)

    def is_unresolved(self) -> bool:
        return self.truth.outcome == ResolutionOutcome.UNRESOLVED
