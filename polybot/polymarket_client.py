"""
polymarket_client.py — Polymarket market discovery and implied price fetching.

Two responsibilities:
  1. Market discovery: find the active BTC 5-min Up/Down market for a given
     5-minute window timestamp.
  2. Implied price: read YES token price from CLOB API with fallbacks.

All HTTP calls are wrapped in try/except. On failure, logs the error and
returns None so main loop can skip the window gracefully.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Hardcoded fee params — matches config["fee"] defaults.
_FEE_NOTE_LOGGED = False


def calc_fee(price: float, fee_rate: float = 0.25, exponent: int = 2) -> float:
    """
    Polymarket crypto market taker fee formula.
    Source: Polymarket official docs (hardcoded for first iteration).
    """
    global _FEE_NOTE_LOGGED
    if not _FEE_NOTE_LOGGED:
        logger.info(
            "calc_fee: using hardcoded fee params fee_rate=%.2f exponent=%d",
            fee_rate,
            exponent,
        )
        _FEE_NOTE_LOGGED = True
    return price * fee_rate * (price * (1 - price)) ** exponent


@dataclass
class MarketInfo:
    condition_id: str
    yes_token_id: str
    no_token_id: str
    slug: str
    min_order_size: float  # defaults to 1 if API doesn't provide it


class PolymarketClient:
    def __init__(self, config: dict):
        self._gamma_base: str = config["polymarket"]["gamma_base"].rstrip("/")
        self._clob_base: str = config["polymarket"]["clob_base"].rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._timeout = 8  # seconds per request

    # ------------------------------------------------------------------ #
    # Market discovery
    # ------------------------------------------------------------------ #

    def get_market(self, window_ts: int) -> Optional[MarketInfo]:
        """
        Find the BTC 5-min Up/Down market for the given window timestamp.
        Tries slug-based search first, falls back to tag/keyword filter.
        Returns None if discovery fails.
        """
        slug = f"btc-updown-5m-{window_ts}"

        # Attempt 1: slug-based lookup
        market = self._fetch_by_slug(slug)
        if market:
            return market

        logger.warning(
            "Slug-based discovery failed for %s. Trying tag/keyword fallback.", slug
        )

        # Attempt 2: tag filter + keyword match
        market = self._fetch_by_tag_filter()
        if market:
            return market

        logger.error(
            "Market discovery failed for window_ts=%d. Skipping window.", window_ts
        )
        return None

    def _fetch_by_slug(self, slug: str) -> Optional[MarketInfo]:
        url = f"{self._gamma_base}/markets"
        params = {"slug": slug}
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("markets", [])
            if markets:
                return self._parse_market(markets[0])
            logger.debug("Slug search returned empty list for slug=%s", slug)
            return None
        except requests.exceptions.Timeout:
            logger.warning("Timeout fetching market by slug=%s", slug)
            return None
        except requests.exceptions.HTTPError as exc:
            logger.warning("HTTP error fetching market by slug=%s: %s", slug, exc)
            return None
        except Exception as exc:
            logger.warning("Unexpected error fetching market by slug=%s: %s", slug, exc)
            return None

    def _fetch_by_tag_filter(self) -> Optional[MarketInfo]:
        """
        Fetch open crypto markets and filter for BTC 5-min Up/Down markets.
        Returns the most recently created matching market or None.
        """
        url = f"{self._gamma_base}/markets"
        params = {"tag": "crypto", "closed": "false", "limit": 100}
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("markets", [])

            candidates = []
            for m in markets:
                question = (m.get("question") or m.get("title") or "").lower()
                slug = (m.get("slug") or "").lower()
                combined = question + " " + slug
                # Must mention btc + 5 + minute (or min) context
                if (
                    "btc" in combined
                    and ("5" in combined)
                    and ("minute" in combined or "min" in combined or "5m" in combined)
                ):
                    candidates.append(m)

            if not candidates:
                logger.warning("Tag/keyword fallback: no BTC 5-min markets found.")
                return None

            logger.info(
                "Tag/keyword fallback found %d candidate(s). Using first.", len(candidates)
            )
            return self._parse_market(candidates[0])

        except requests.exceptions.Timeout:
            logger.warning("Timeout in tag-filter market discovery.")
            return None
        except requests.exceptions.HTTPError as exc:
            logger.warning("HTTP error in tag-filter market discovery: %s", exc)
            return None
        except Exception as exc:
            logger.warning("Unexpected error in tag-filter market discovery: %s", exc)
            return None

    def _parse_market(self, m: dict) -> Optional[MarketInfo]:
        """Extract fields from a raw Gamma API market object."""
        try:
            condition_id = m.get("conditionId") or m.get("condition_id") or ""
            slug = m.get("slug") or ""

            # Token IDs can live in different shapes depending on API version
            tokens = m.get("tokens") or []
            yes_token_id = ""
            no_token_id = ""

            if tokens:
                for t in tokens:
                    outcome = (t.get("outcome") or "").upper()
                    if outcome == "YES":
                        yes_token_id = t.get("token_id") or t.get("tokenId") or ""
                    elif outcome == "NO":
                        no_token_id = t.get("token_id") or t.get("tokenId") or ""
            else:
                # Some API responses embed token IDs directly.
                # clobTokenIds may be a JSON-encoded string e.g. '["id1","id2"]'
                # — parse it before indexing to avoid getting '[' as the token.
                clob_ids = m.get("clobTokenIds") or []
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = json.loads(clob_ids)
                    except (json.JSONDecodeError, ValueError):
                        clob_ids = []
                yes_token_id = (
                    m.get("yes_token_id")
                    or m.get("yesTokenId")
                    or (clob_ids[0] if len(clob_ids) > 0 else "")
                    or ""
                )
                no_token_id = (
                    m.get("no_token_id")
                    or m.get("noTokenId")
                    or (clob_ids[1] if len(clob_ids) > 1 else "")
                    or ""
                )

            if not yes_token_id or not no_token_id:
                logger.warning(
                    "Could not extract token IDs from market %s. "
                    "Keys available: %s",
                    slug,
                    list(m.keys()),
                )
                return None

            min_order_size = float(m.get("minOrderSize") or m.get("min_order_size") or 1)
            if not m.get("minOrderSize") and not m.get("min_order_size"):
                logger.info(
                    "min_order_size not in API response for market %s. "
                    "Using default 1 share.",
                    slug,
                )

            return MarketInfo(
                condition_id=condition_id,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                slug=slug,
                min_order_size=min_order_size,
            )
        except Exception as exc:
            logger.warning("Failed to parse market object: %s | data=%s", exc, str(m)[:200])
            return None

    # ------------------------------------------------------------------ #
    # Implied price
    # ------------------------------------------------------------------ #

    def get_implied_price(self, yes_token_id: str) -> Optional[float]:
        """
        Fetch implied YES token price.
        Tries /midpoint first, then /price, then computes from /book.
        Returns float in [0, 1] or None on failure.
        """
        price = self._fetch_midpoint(yes_token_id)
        if price is not None:
            return price

        logger.warning(
            "Midpoint endpoint failed for token %s. Trying /price.", yes_token_id[:16]
        )
        price = self._fetch_price(yes_token_id)
        if price is not None:
            return price

        logger.warning(
            "/price endpoint failed for token %s. Trying /book.", yes_token_id[:16]
        )
        price = self._fetch_from_book(yes_token_id)
        if price is not None:
            return price

        logger.error(
            "All implied price endpoints failed for token %s.", yes_token_id[:16]
        )
        return None

    def _fetch_midpoint(self, token_id: str) -> Optional[float]:
        url = f"{self._clob_base}/midpoint"
        try:
            resp = self._session.get(
                url, params={"token_id": token_id}, timeout=self._timeout
            )
            resp.raise_for_status()
            data = resp.json()
            val = data.get("mid") or data.get("midpoint") or data.get("price")
            if val is not None:
                return float(val)
            logger.debug("/midpoint response missing expected key. Keys: %s", list(data.keys()))
            return None
        except requests.exceptions.Timeout:
            logger.warning("/midpoint timeout for token %s", token_id[:16])
            return None
        except requests.exceptions.HTTPError as exc:
            logger.warning("/midpoint HTTP error for token %s: %s", token_id[:16], exc)
            return None
        except Exception as exc:
            logger.warning("/midpoint unexpected error: %s", exc)
            return None

    def _fetch_price(self, token_id: str) -> Optional[float]:
        url = f"{self._clob_base}/price"
        try:
            resp = self._session.get(
                url,
                params={"token_id": token_id, "side": "BUY"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            val = data.get("price") or data.get("mid")
            if val is not None:
                return float(val)
            logger.debug("/price response missing expected key. Keys: %s", list(data.keys()))
            return None
        except requests.exceptions.Timeout:
            logger.warning("/price timeout for token %s", token_id[:16])
            return None
        except requests.exceptions.HTTPError as exc:
            logger.warning("/price HTTP error for token %s: %s", token_id[:16], exc)
            return None
        except Exception as exc:
            logger.warning("/price unexpected error: %s", exc)
            return None

    def _fetch_from_book(self, token_id: str) -> Optional[float]:
        url = f"{self._clob_base}/book"
        try:
            resp = self._session.get(
                url, params={"token_id": token_id}, timeout=self._timeout
            )
            resp.raise_for_status()
            data = resp.json()
            bids = data.get("bids") or []
            asks = data.get("asks") or []
            if bids and asks:
                best_bid = float(bids[0]["price"])
                best_ask = float(asks[0]["price"])
                mid = (best_bid + best_ask) / 2.0
                logger.info(
                    "/book fallback: bid=%.4f ask=%.4f mid=%.4f for token %s",
                    best_bid,
                    best_ask,
                    mid,
                    token_id[:16],
                )
                return mid
            logger.warning("/book returned empty bids or asks for token %s", token_id[:16])
            return None
        except requests.exceptions.Timeout:
            logger.warning("/book timeout for token %s", token_id[:16])
            return None
        except requests.exceptions.HTTPError as exc:
            logger.warning("/book HTTP error for token %s: %s", token_id[:16], exc)
            return None
        except Exception as exc:
            logger.warning("/book unexpected error: %s", exc)
            return None
