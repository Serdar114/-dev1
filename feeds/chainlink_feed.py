"""
feeds/chainlink_feed.py — Chainlink-like settlement / reference feed adapter.

RTDS topic : crypto_prices_chainlink
RTDS host  : wss://ws-live-data.polymarket.com   (NOT the CLOB host)
Purpose    : Slower, authoritative price reference used for:
             - Settlement verification
             - Basis mismatch computation
             - Open-price integrity checks

Channel semantics:
  - Analogous to a Chainlink oracle price feed.
  - Updates less frequently than the fast feed (typically every few
    seconds to tens of seconds, mirroring on-chain oracle cadence).

Subscription sent after connect:
  {
    "action": "subscribe",
    "subscriptions": [
      {
        "topic": "crypto_prices_chainlink",
        "type":  "update"
      }
    ]
  }

Incoming message structure:
  {
    "topic":     "crypto_prices_chainlink",
    "type":      "update",
    "timestamp": <unix ms>,
    "payload": {
      "symbol":    "BTCUSDT",
      "value":     <float price>,
      "timestamp": <unix ms>,
      "round_id":  <optional str/int>   # Chainlink round identifier
    }
  }

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
from .fast_feed import _to_rtds_symbol  # reuse shared symbol mapping

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
    """

    def _channel_name(self) -> str:
        return _CHANNEL

    def _feed_label(self) -> str:
        return "chainlink"

    def _subscribe_payload(self) -> dict:
        """
        RTDS subscribe action for crypto_prices_chainlink.

        No symbol filter is included by default; the Chainlink topic
        delivers fewer assets and the adapter filters by symbol in
        _parse_message.  Add a filter here if the feed proves noisy.
        """
        return {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": _CHANNEL,
                    "type": "update",
                }
            ],
        }

    def _parse_message(self, payload: dict) -> Optional[FeedSnapshot]:
        """
        Parse an incoming RTDS message from `crypto_prices_chainlink`.

        Incoming envelope:
          {
            "topic":     "crypto_prices_chainlink",
            "type":      "update",
            "timestamp": <unix ms>,
            "payload": {
              "symbol":    "BTCUSDT",
              "value":     <float price>,
              "timestamp": <unix ms>,
              "round_id":  <optional>
            }
          }

        timestamp is in milliseconds; converted to seconds for FeedSnapshot.
        round_id is logged for audit purposes if present; not used in
        current signal logic.
        """
        if payload.get("topic") != _CHANNEL:
            return None

        data = payload.get("payload", {})
        if not data:
            return None

        # Symbol filter — only process ticks for the configured symbol.
        rtds_sym = _to_rtds_symbol(self._symbol)
        incoming_sym = data.get("symbol", "")
        if incoming_sym and incoming_sym != rtds_sym:
            return None

        try:
            price = float(data["value"])
            ts = float(data["timestamp"]) / 1000.0  # ms → seconds
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("[chainlink] Cannot parse tick: %s | payload=%s", exc, payload)
            return None

        snap = self._build_snapshot(price=price, ts=ts, raw=payload)

        # Log round_id if present (Chainlink-specific audit field).
        round_id = data.get("round_id")
        if round_id is not None:
            logger.debug("[chainlink] round_id=%s price=%.6f", round_id, price)

        return snap
