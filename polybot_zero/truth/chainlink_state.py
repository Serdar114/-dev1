"""
chainlink_state.py — Canonical Chainlink price wrapper with gap detection.

Design:
  Wraps RTDSClient.chainlink_latest() to provide:
    - latest() → Optional[ChainlinkPrice] with freshness
    - gap_flag: True if last update is older than gap_threshold_secs
      (Polymarket RTDS has documented ~8s intermittent gaps; GitHub issue #31)

  Gap vs Stale distinction:
    gap_flag: short-duration silence (e.g. 8-15s) that may self-heal
    STALE freshness: price older than staleness_secs (e.g. 30s) — hard block on trades
    MISSING: never received any data

  A gap at window boundary → caller should treat capture as failed (UNRESOLVED).
  The resolution_truth.py freshness check handles this automatically because
  a gapped price will show STALE or MISSING freshness.
"""

from __future__ import annotations
import logging
import time
from typing import Optional, TYPE_CHECKING

from loggingx.schemas import ChainlinkPrice, FreshnessState

if TYPE_CHECKING:
    from feeds.rtds_client import RTDSClient

logger = logging.getLogger("polybot.chainlink_state")


class ChainlinkState:
    """
    Canonical Chainlink price state tracker.

    Job: provide latest Chainlink price and gap detection flag.
    Input: RTDSClient instance (shared with runner)
    Output: latest() → Optional[ChainlinkPrice], gap_flag property

    Failure:
      - RTDSClient has no data: latest() returns None
      - Gap detected: gap_flag=True; latest() may still return a price but freshness=STALE
    """

    def __init__(
        self,
        rtds_client: "RTDSClient",
        gap_threshold_secs: float = 15.0,
    ):
        self._rtds = rtds_client
        self._gap_threshold = gap_threshold_secs

    def latest(self) -> Optional[ChainlinkPrice]:
        """
        Return latest Chainlink price snapshot.
        Freshness is computed at read time by RTDSClient.
        Returns None if no data ever received.
        """
        return self._rtds.chainlink_latest()

    @property
    def gap_flag(self) -> bool:
        """
        True if Chainlink feed has been silent for > gap_threshold_secs.
        This detects the documented ~8s RTDS gaps before they become STALE.
        """
        last_ts = self._rtds.chainlink_last_ts()
        if last_ts is None:
            return True    # never received any data — treat as gap
        return (time.time() - last_ts) > self._gap_threshold

    def is_fresh(self) -> bool:
        """True only if latest price exists and freshness == FRESH."""
        p = self.latest()
        return p is not None and p.freshness == FreshnessState.FRESH

    def price_or_none(self) -> Optional[float]:
        """Return price_usd if fresh, else None."""
        p = self.latest()
        if p and p.freshness == FreshnessState.FRESH:
            return p.price_usd
        return None

    def describe(self) -> str:
        p = self.latest()
        if p is None:
            return "ChainlinkState(MISSING)"
        return (
            f"ChainlinkState(price={p.price_usd:.2f} "
            f"freshness={p.freshness} "
            f"age={p.age_secs():.1f}s "
            f"gap_flag={self.gap_flag})"
        )
