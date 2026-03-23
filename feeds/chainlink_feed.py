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
        Parse the ACTUAL crypto_prices_chainlink RTDS format observed from the
        live feed:

          {
            "connection_id": "...",
            "payload": {
              "symbol":              "btc/usd",   ← lowercase with slash
              "timestamp":           <unix ms>,
              "value":               <float>,
              "full_accuracy_value": "..."        ← optional, ignored
            },
            "timestamp": <unix ms>,
            "topic":     "crypto_prices_chainlink",
            "type":      "update"
          }

        Symbol filter: accept "btc/usd" only; discard all other symbols
        (eth/usd, sol/usd, etc.) silently — they are expected, NOT failures.

        Returns None (discarded) for non-BTC symbols.
        Raises ValueError/KeyError for unexpected shapes → base counts as parse_failed.
        """
        payload_obj = payload.get("payload")
        if not isinstance(payload_obj, dict):
            raise ValueError(
                f"Expected 'payload' dict, got {type(payload_obj).__name__}. "
                f"Keys: {list(payload.keys())}"
            )

        symbol = payload_obj.get("symbol", "")
        if symbol.lower() != "btc/usd":
            # Expected: eth/usd, sol/usd, doge/usd, bnb/usd, etc. — discard quietly.
            logger.debug("[chainlink] Discarded: symbol=%r (want 'btc/usd')", symbol)
            return None

        price = float(payload_obj["value"])
        ts = float(payload_obj["timestamp"]) / 1000.0  # ms → seconds

        snap = self._build_snapshot(price=price, ts=ts, raw=payload)

        # Log full_accuracy_value if present (Chainlink audit field).
        fav = payload_obj.get("full_accuracy_value")
        if fav is not None:
            logger.debug("[chainlink] btc/usd full_accuracy_value=%s price=%.6f", fav, price)

        return snap
