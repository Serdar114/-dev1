"""
feeds/fast_feed.py — Fast price feed adapter (Binance-like).

RTDS topic : crypto_prices
RTDS host  : wss://ws-live-data.polymarket.com   (NOT the CLOB host)
Purpose    : High-frequency intra-window price updates for signal computation.

Channel semantics:
  - Analogous to a Binance spot mid-price stream.
  - Delivers the "live" market price that the signal engine uses as the
    primary input for direction assessment.
  - Expected tick rate: sub-second to a few seconds.

Subscription sent after connect:
  {
    "action": "subscribe",
    "subscriptions": [
      {
        "topic":   "crypto_prices",
        "type":    "update",
        "filters": "{\"symbol\":\"BTCUSDT\"}"
      }
    ]
  }

Incoming message structure:
  {
    "topic":     "crypto_prices",
    "type":      "update",
    "timestamp": <unix ms>,
    "payload": {
      "symbol":    "BTCUSDT",
      "value":     <float price>,
      "timestamp": <unix ms>
    }
  }

Symbol mapping:
  Bot-internal "BTC-USD" ↔ RTDS "BTCUSDT" (filter string for subscription).
  Only one symbol per RTDS connection is supported; subscribing to a new
  symbol replaces the current subscription.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .base import BaseFeedAdapter, FeedSnapshot

logger = logging.getLogger(__name__)

_CHANNEL = "crypto_prices"

# Mapping from bot-internal symbol names to RTDS symbol names.
_SYMBOL_MAP = {
    "BTC-USD": "BTCUSDT",
    "ETH-USD": "ETHUSDT",
    "SOL-USD": "SOLUSDT",
}


def _to_rtds_symbol(bot_symbol: str) -> str:
    """Convert bot-internal symbol (e.g. 'BTC-USD') to RTDS symbol ('BTCUSDT')."""
    return _SYMBOL_MAP.get(bot_symbol, bot_symbol.replace("-", "").upper())


class FastFeedAdapter(BaseFeedAdapter):
    """
    Binance-like fast price feed via Polymarket RTDS `crypto_prices`.

    Provides rapid intra-window price ticks used by:
      - Signal engine (primary price input)
      - Basis mismatch computation (fast vs. Chainlink reference)
      - Feed freshness gate
    """

    def _channel_name(self) -> str:
        return _CHANNEL

    def _feed_label(self) -> str:
        return "fast"

    def _subscribe_payload(self) -> dict:
        """
        RTDS subscribe action for crypto_prices filtered to the configured symbol.

        Filter string is JSON-encoded as required by the RTDS protocol:
          "filters": "{\"symbol\":\"BTCUSDT\"}"
        """
        rtds_sym = _to_rtds_symbol(self._symbol)
        filter_str = json.dumps({"symbol": rtds_sym})
        return {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": _CHANNEL,
                    "type": "update",
                    "filters": filter_str,
                }
            ],
        }

    def _parse_message(self, payload: dict) -> Optional[FeedSnapshot]:
        """
        Parse an incoming RTDS message from `crypto_prices`.

        Incoming envelope:
          {
            "topic":     "crypto_prices",
            "type":      "update",
            "timestamp": <unix ms>,
            "payload": {
              "symbol":    "BTCUSDT",
              "value":     <float price>,
              "timestamp": <unix ms>
            }
          }

        timestamp is in milliseconds; converted to seconds for FeedSnapshot.
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
            logger.debug("[fast] Cannot parse tick: %s | payload=%s", exc, payload)
            return None

        return self._build_snapshot(price=price, ts=ts, raw=payload)
