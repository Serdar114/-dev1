"""
feeds/fast_feed.py — Fast price feed adapter (Binance-like).

RTDS channel: crypto_prices
Purpose     : High-frequency intra-window price updates for signal computation.

Channel semantics (from spec addendum):
  - Analogous to a Binance spot mid-price stream.
  - Delivers the "live" market price that the signal engine uses as the
    primary input for direction assessment.
  - Expected tick rate: sub-second to a few seconds.

TODO: Confirm `crypto_prices` message schema from Polymarket RTDS docs.
      Expected fields based on CLOB naming conventions:
        {
          "channel": "crypto_prices",
          "data": {
            "asset": "<symbol>",
            "price": <float>,
            "timestamp": <unix_seconds_float>
          }
        }
      If the schema differs (e.g. nested differently, uses integer ms
      timestamps), update _parse_message accordingly.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import BaseFeedAdapter, FeedSnapshot

logger = logging.getLogger(__name__)

# Channel name as specified in the RTDS spec addendum.
_CHANNEL = "crypto_prices"


class FastFeedAdapter(BaseFeedAdapter):
    """
    Binance-like fast price feed via Polymarket RTDS `crypto_prices`.

    Provides rapid intra-window price ticks used by:
      - Signal engine (primary price input)
      - Basis mismatch computation (fast vs. Chainlink reference)
      - Feed freshness gate

    fast_feed_gap_seconds is derived from FeedSnapshot.gap_seconds.
    """

    def _channel_name(self) -> str:
        return _CHANNEL

    def _feed_label(self) -> str:
        return "fast"

    def _parse_message(self, payload: dict) -> Optional[FeedSnapshot]:
        """
        Parse an incoming RTDS message from `crypto_prices`.

        TODO: Update field paths once confirmed against live RTDS docs.
              Currently expecting:
                payload["channel"] == "crypto_prices"
                payload["data"]["price"]     → float
                payload["data"]["timestamp"] → float (unix seconds)
                payload["data"]["asset"]     → str matching self._symbol
        """
        if payload.get("channel") != _CHANNEL:
            return None

        data = payload.get("data", {})
        if not data:
            return None

        # Asset filter — only process ticks for the configured symbol.
        if data.get("asset") and data["asset"] != self._symbol:
            return None

        try:
            price = float(data["price"])
            ts = float(data["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("[fast] Cannot parse tick: %s | payload=%s", exc, payload)
            return None

        return self._build_snapshot(price=price, ts=ts, raw=payload)
