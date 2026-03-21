"""
feeds/chainlink_feed.py — Chainlink-like settlement / reference feed adapter.

RTDS channel: crypto_prices_chainlink
Purpose     : Slower, authoritative price reference used for:
              - Settlement verification
              - Basis mismatch computation
              - Open-price integrity checks

Channel semantics (from spec addendum):
  - Analogous to a Chainlink oracle price feed.
  - Updates less frequently than the fast feed (typically every few
    seconds to tens of seconds, mirroring on-chain oracle cadence).
  - Used as the reference price against which the fast feed is compared.

TODO: Confirm `crypto_prices_chainlink` message schema from Polymarket
      RTDS docs. Expected schema (mirrors fast feed with different channel):
        {
          "channel": "crypto_prices_chainlink",
          "data": {
            "asset": "<symbol>",
            "price": <float>,
            "timestamp": <unix_seconds_float>,
            "round_id": <optional int>   # Chainlink round identifier
          }
        }
      Update _parse_message if schema differs.

Gap monitoring note:
  chainlink_gap_seconds is intentionally more forgiving than fast_feed_gap
  because Chainlink-style oracles update on deviation thresholds, not on
  every tick.  The stale_threshold_seconds in settings.yaml applies to both
  feeds; adjust if Chainlink cadence proves systematically slower.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import BaseFeedAdapter, FeedSnapshot

logger = logging.getLogger(__name__)

_CHANNEL = "crypto_prices_chainlink"


class ChainlinkFeedAdapter(BaseFeedAdapter):
    """
    Chainlink-like reference / settlement feed via Polymarket RTDS
    `crypto_prices_chainlink`.

    Provides the authoritative reference price used by:
      - Signal engine (open-price integrity gate)
      - Basis mismatch computation
      - Kill condition: chainlink_gap_frequency_ceiling

    chainlink_gap_seconds is derived from FeedSnapshot.gap_seconds.
    """

    def _channel_name(self) -> str:
        return _CHANNEL

    def _feed_label(self) -> str:
        return "chainlink"

    def _parse_message(self, payload: dict) -> Optional[FeedSnapshot]:
        """
        Parse an incoming RTDS message from `crypto_prices_chainlink`.

        TODO: Update field paths once confirmed against live RTDS docs.
              round_id is logged for audit purposes if present; not used
              in current signal logic.
        """
        if payload.get("channel") != _CHANNEL:
            return None

        data = payload.get("data", {})
        if not data:
            return None

        if data.get("asset") and data["asset"] != self._symbol:
            return None

        try:
            price = float(data["price"])
            ts = float(data["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("[chainlink] Cannot parse tick: %s | payload=%s", exc, payload)
            return None

        snap = self._build_snapshot(price=price, ts=ts, raw=payload)

        # Log round_id if present (Chainlink-specific audit field).
        round_id = data.get("round_id")
        if round_id is not None:
            logger.debug("[chainlink] round_id=%s price=%.6f", round_id, price)

        return snap
