"""
discovery/market.py — Deterministic market discovery for BTC 5m windows.

Slug generation
---------------
Every 5-minute BTC up/down market has a slug of the form:

    btc-updown-5m-{window_boundary}

where {window_boundary} is the Unix timestamp (integer seconds) of the
window's OPEN (i.e., the previous 5-minute grid boundary).

Example:
    Window 14:05–14:10 UTC on 2025-01-15 →
        boundary = 1736946300
        slug     = "btc-updown-5m-1736946300"

Dynamic discovery
-----------------
Token IDs are NOT hardcoded.  Discovery works as follows:

1. Compute the slug for the desired window boundary.
2. Query the Gamma events API to look up the market by slug.
3. Extract the YES/NO token IDs from the returned event/market object.
4. Cache the result for the window's lifetime.

TODO: Confirm the exact Gamma API endpoint for slug-based market lookup.
      Current best-known endpoint pattern:
        GET {gamma_base_url}/markets?slug={slug}
      Returns a list; we take the first matching item.
      Update _fetch_by_slug() if the actual path differs.

TODO: Confirm response schema.  Expected (based on Polymarket Gamma docs):
        [{
          "id": "<market_id>",
          "slug": "<slug>",
          "conditionId": "0x...",
          "tokens": [
            {"outcome": "Yes", "token_id": "<yes_token_id>"},
            {"outcome": "No",  "token_id": "<no_token_id>"}
          ],
          "endDateIso": "<ISO8601>",
          ...
        }]
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 300  # 5 minutes


def slug_for_window(window_boundary_ts: int) -> str:
    """
    Deterministically generate the market slug for a given window open
    timestamp.

    Parameters
    ----------
    window_boundary_ts : Unix timestamp (integer seconds) of the window OPEN.

    Returns
    -------
    Slug string, e.g. "btc-updown-5m-1736946300".
    """
    return f"btc-updown-5m-{window_boundary_ts}"


def current_window_boundary(now: Optional[float] = None) -> int:
    """
    Return the Unix timestamp of the most recently completed 5m grid boundary.

    The boundary is the OPEN of the current window (i.e., floor to 5m).
    """
    t = now if now is not None else time.time()
    return int(math.floor(t / _WINDOW_SECONDS) * _WINDOW_SECONDS)


def next_window_boundary(now: Optional[float] = None) -> int:
    """Return Unix timestamp of the next 5m window open."""
    return current_window_boundary(now) + _WINDOW_SECONDS


@dataclass
class WindowMarket:
    """
    Discovered market for a single BTC 5m window.

    Attributes
    ----------
    slug            Market slug (deterministic, human-readable).
    window_open_ts  Unix timestamp of window open.
    window_close_ts Unix timestamp of window close.
    market_id       Gamma market ID string.
    yes_token_id    CLOB token ID for YES outcome (dynamic, not hardcoded).
    no_token_id     CLOB token ID for NO outcome  (dynamic, not hardcoded).
    condition_id    On-chain condition ID (for audit).
    end_date_iso    ISO8601 settlement date from Gamma.
    """
    slug: str
    window_open_ts: int
    window_close_ts: int
    market_id: str
    yes_token_id: str
    no_token_id: str
    condition_id: str
    end_date_iso: str


class MarketDiscovery:
    """
    Async market discovery client.

    Usage
    -----
        discovery = MarketDiscovery(config["discovery"])
        market = await discovery.get_market_for_window(boundary_ts)
    """

    def __init__(self, config: dict) -> None:
        self._base_url = config["gamma_base_url"].rstrip("/")
        self._timeout = config.get("discovery_timeout_seconds", 5.0)
        self._cache: dict[int, WindowMarket] = {}

    async def get_market_for_window(
        self, window_boundary_ts: int
    ) -> Optional[WindowMarket]:
        """
        Return a WindowMarket for the given boundary timestamp, using the
        local cache if already populated.

        Returns None (and logs an error) on any failure: API error,
        timeout, malformed response, or slug not found.
        """
        if window_boundary_ts in self._cache:
            return self._cache[window_boundary_ts]

        slug = slug_for_window(window_boundary_ts)
        market = await self._fetch_by_slug(slug, window_boundary_ts)
        if market is not None:
            self._cache[window_boundary_ts] = market
        return market

    async def get_current_window_market(self) -> Optional[WindowMarket]:
        """Convenience wrapper: discover market for the current 5m window."""
        boundary = current_window_boundary()
        return await self.get_market_for_window(boundary)

    def evict_stale_cache(self, lookback_windows: int = 3) -> None:
        """Remove cache entries older than lookback_windows * 5m."""
        cutoff = current_window_boundary() - lookback_windows * _WINDOW_SECONDS
        stale = [k for k in self._cache if k < cutoff]
        for k in stale:
            del self._cache[k]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch_by_slug(
        self, slug: str, window_boundary_ts: int
    ) -> Optional[WindowMarket]:
        """
        Query Gamma API for a market matching `slug`.

        TODO: Update URL path and response parsing when Gamma API endpoint
              is confirmed.  Current pattern assumes:
                GET {base}/markets?slug={slug}
              Returns a JSON array of market objects.
        """
        try:
            import aiohttp  # type: ignore
        except ImportError:
            logger.error("[discovery] aiohttp not installed — cannot fetch markets")
            return None

        url = f"{self._base_url}/markets"
        params = {"slug": slug}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "[discovery] Gamma returned HTTP %d for slug=%s",
                            resp.status, slug
                        )
                        return None
                    data = await resp.json()
        except asyncio.TimeoutError:
            logger.warning("[discovery] Timeout fetching slug=%s", slug)
            return None
        except Exception as exc:
            logger.error("[discovery] Error fetching slug=%s: %s", slug, exc)
            return None

        return self._parse_market(data, slug, window_boundary_ts)

    def _parse_market(
        self, data: object, slug: str, window_boundary_ts: int
    ) -> Optional[WindowMarket]:
        """
        Parse Gamma API response into a WindowMarket.

        TODO: Confirm exact field names against live Gamma API response.
              Current parsing is based on documented Gamma schema.
        """
        if not isinstance(data, list) or len(data) == 0:
            logger.warning("[discovery] No market found for slug=%s", slug)
            return None

        item = data[0]

        # Extract token IDs — NOT hardcoded, always pulled from API response.
        tokens = item.get("tokens", [])
        yes_token_id = None
        no_token_id = None
        for tok in tokens:
            outcome = tok.get("outcome", "").lower()
            if outcome == "yes":
                yes_token_id = tok.get("token_id") or tok.get("tokenId")
            elif outcome == "no":
                no_token_id = tok.get("token_id") or tok.get("tokenId")

        if yes_token_id is None or no_token_id is None:
            logger.warning(
                "[discovery] Could not extract YES/NO token IDs for slug=%s "
                "tokens=%s",
                slug, tokens
            )
            return None

        market = WindowMarket(
            slug=slug,
            window_open_ts=window_boundary_ts,
            window_close_ts=window_boundary_ts + _WINDOW_SECONDS,
            market_id=str(item.get("id", "")),
            yes_token_id=str(yes_token_id),
            no_token_id=str(no_token_id),
            condition_id=str(item.get("conditionId", "")),
            end_date_iso=str(item.get("endDateIso", "")),
        )
        logger.info(
            "[discovery] Resolved slug=%s market_id=%s yes=%s no=%s",
            slug, market.market_id, market.yes_token_id, market.no_token_id
        )
        return market
