"""
Minimal Polymarket REST client for Phase 1.

Fetches:
  - Market metadata (condition_id, token_ids, end_date)
  - Current order book (best bid/ask for YES/NO)

No authentication needed for read-only market data.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_CLOB_BASE = "https://clob.polymarket.com"
_TIMEOUT = 8  # seconds


class PolymarketClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "polybot-v2/1.0"})

    # ------------------------------------------------------------------ #
    # Market discovery helpers
    # ------------------------------------------------------------------ #

    def get_markets(
        self,
        tag: str = "btc-5m",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Fetch recent markets from Gamma API filtered by tag/slug prefix.
        Returns list of raw market dicts.
        """
        try:
            resp = self._session.get(
                f"{_GAMMA_BASE}/markets",
                params={"tag": tag, "closed": "false", "limit": limit},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.warning("get_markets failed: %s", exc)
            return []

    def get_market_by_condition_id(self, condition_id: str) -> Optional[dict[str, Any]]:
        try:
            resp = self._session.get(
                f"{_CLOB_BASE}/markets/{condition_id}",
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.warning("get_market_by_condition_id(%s) failed: %s", condition_id, exc)
            return None

    def search_markets(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search Gamma API for markets matching a text query."""
        try:
            resp = self._session.get(
                f"{_GAMMA_BASE}/markets",
                params={"q": query, "closed": "false", "limit": limit},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.warning("search_markets(%r) failed: %s", query, exc)
            return []

    # ------------------------------------------------------------------ #
    # Order book
    # ------------------------------------------------------------------ #

    def get_order_book(self, token_id: str) -> Optional[dict[str, Any]]:
        """
        Fetch best bid/ask for a token from CLOB.
        Returns raw dict with 'bids' and 'asks' arrays or None on failure.
        """
        try:
            resp = self._session.get(
                f"{_CLOB_BASE}/book",
                params={"token_id": token_id},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.debug("get_order_book(%s) failed: %s", token_id, exc)
            return None

    def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """Fetch last trade price for a token."""
        try:
            resp = self._session.get(
                f"{_CLOB_BASE}/last-trade-price",
                params={"token_id": token_id},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            price = data.get("price")
            return float(price) if price is not None else None
        except (requests.RequestException, ValueError, TypeError) as exc:
            log.debug("get_last_trade_price(%s) failed: %s", token_id, exc)
            return None

    @staticmethod
    def parse_best_bid_ask(book: dict[str, Any]) -> tuple[float, float]:
        """
        Extract best bid and best ask from CLOB book response.
        Returns (best_bid, best_ask); 0.0 if empty side.
        """
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = max((float(b["price"]) for b in bids if "price" in b), default=0.0)
        best_ask = min((float(a["price"]) for a in asks if "price" in a), default=1.0)
        return best_bid, best_ask
