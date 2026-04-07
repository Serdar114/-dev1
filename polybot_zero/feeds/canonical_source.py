"""
canonical_source.py — Canonical price source selector and arbitration layer.

Design:
  This layer arbitrates between the two candidate canonical sources and
  presents a single CanonicalPriceSnapshot to the rest of the system.
  The rest of the system NEVER reads individual sources directly.

  Source priority:
    1. RTDS oracle feed (RTDSOracleClient) — first candidate
    2. Direct Chainlink RPC (ChainlinkRPCClient) — second candidate (fallback)

  Selection logic (per read):
    - If RTDS is enabled and returns a FRESH price → use RTDS
    - Otherwise → use direct RPC if FRESH
    - If neither is FRESH → return None (caller must treat as CANONICAL_PRICE_MISSING)

  Logging requirements:
    - Log the selected source and selection reason on every source change
    - Log explicitly when fallback becomes active or is deactivated
    - Log at startup after the first successful read from either source

  This module does NOT assume either source is the exact oracle that
  Polymarket resolution reads. That claim requires official documentation
  or runtime confirmation that is outside this system's scope.
"""

from __future__ import annotations
import logging
import time
from typing import Optional

from feeds.rtds_client import RTDSOracleClient
from feeds.chainlink_rpc import ChainlinkRPCClient
from loggingx.schemas import CanonicalPriceSnapshot, FreshnessState

logger = logging.getLogger("polybot.canonical_source")


class CanonicalSourceSelector:
    """
    Arbitrates between RTDS oracle feed and direct Chainlink RPC.

    Job: Return a CanonicalPriceSnapshot from the highest-priority available source.
    Input: RTDSOracleClient (first candidate), ChainlinkRPCClient (second candidate)
    Output: CanonicalPriceSnapshot | None
    Failure: None when both sources are unavailable or stale.
    """

    def __init__(
        self,
        rtds: RTDSOracleClient,
        rpc: ChainlinkRPCClient,
    ):
        self._rtds = rtds
        self._rpc  = rpc

        # Track last selected source to detect transitions
        self._last_source_tag: Optional[str]  = None
        self._first_log_done:  bool = False

    def latest(self) -> Optional[CanonicalPriceSnapshot]:
        """
        Return the best available fresh canonical price, or None.

        Selection is evaluated on every call.
        Source transitions are logged explicitly.
        """
        # ── Candidate 1: RTDS oracle feed ────────────────────────
        if self._rtds.is_enabled():
            rtds_price = self._rtds.latest()
            if rtds_price is not None and rtds_price.freshness == FreshnessState.FRESH:
                snap = CanonicalPriceSnapshot(
                    price_usd=rtds_price.price_usd,
                    round_id=rtds_price.round_id,
                    updated_at=rtds_price.updated_at,
                    fetched_at=rtds_price.fetched_at,
                    freshness=rtds_price.freshness,
                    source_tag="rtds",
                    selected_reason="rtds_fresh",
                    fallback_active=False,
                )
                self._log_if_changed(snap)
                return snap

            # RTDS enabled but not fresh — determine why
            if rtds_price is None:
                rtds_skip_reason = "rtds_unavailable"
            else:
                rtds_skip_reason = "rtds_stale"
        else:
            rtds_skip_reason = "rtds_disabled"

        # ── Candidate 2: direct Chainlink RPC ────────────────────
        rpc_price = self._rpc.latest()
        if rpc_price is not None and rpc_price.freshness == FreshnessState.FRESH:
            snap = CanonicalPriceSnapshot(
                price_usd=rpc_price.price_usd,
                round_id=rpc_price.round_id,
                updated_at=rpc_price.updated_at,
                fetched_at=rpc_price.fetched_at,
                freshness=rpc_price.freshness,
                source_tag="chainlink_rpc",
                selected_reason=rtds_skip_reason,
                fallback_active=True,
            )
            self._log_if_changed(snap)
            return snap

        # ── Neither source available ──────────────────────────────
        rpc_state = (
            "rpc_stale" if rpc_price is not None else "rpc_unavailable"
        )
        logger.warning(
            "canonical_source: NO fresh price — rtds=%s rpc=%s",
            rtds_skip_reason, rpc_state,
        )
        return None

    def _log_if_changed(self, snap: CanonicalPriceSnapshot) -> None:
        """Log source selection on first read and on every source transition."""
        if not self._first_log_done or snap.source_tag != self._last_source_tag:
            logger.info(
                "canonical_source SELECTED: source=%s reason=%s fallback=%s "
                "price=%.2f round=%d age=%.1fs",
                snap.source_tag,
                snap.selected_reason,
                snap.fallback_active,
                snap.price_usd,
                snap.round_id,
                snap.age_secs(),
            )
            self._first_log_done  = True
            self._last_source_tag = snap.source_tag
