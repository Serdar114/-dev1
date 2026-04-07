"""
market_discovery.py — Discovers active BTC 5-minute markets on Polymarket.

Design:
  Queries Polymarket CLOB REST API for active, non-closed markets.
  Filters by:
    - question/title contains BTC/Bitcoin keyword
    - window duration is approximately 5 minutes (240–360 seconds)
    - market is active and not closed
  Returns only MarketIdentity objects with all fields populated.
  Partial markets (missing token IDs, missing times) are logged and dropped.
  This module NEVER invents or defaults market fields.

  Token role assignment:
    Polymarket binary markets have "Yes" and "No" outcome tokens.
    For BTC 5m Up/Down:
      "Yes" = price went Up → up_token_id
      "No"  = price went Down → down_token_id
    We verify this from the "outcomes" or "tokens[].outcome" field.
    If token roles are ambiguous, the market is dropped with a log.
"""

from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

try:
    from dateutil import parser as dateutil_parser
    DATEUTIL_AVAILABLE = True
except ImportError:
    DATEUTIL_AVAILABLE = False

from loggingx.schemas import MarketIdentity

logger = logging.getLogger("polybot.market_discovery")

CLOB_MARKETS_URL = "https://clob.polymarket.com/markets"
PAGE_LIMIT = 100


class MarketDiscovery:
    """
    Discovers BTC 5m markets on Polymarket CLOB.

    Job: Return list of MarketIdentity for active BTC 5m markets only.
    Input: config (keywords, min/max window secs)
    Output: List[MarketIdentity] — only complete, validated entries
    Failure:
      - API failure → empty list + log (no crash, no guessing)
      - Partial market → skipped + logged individually
      - Ambiguous token roles → skipped + logged
    """

    def __init__(
        self,
        clob_api_url: str = "https://clob.polymarket.com",
        title_keywords: Optional[List[str]] = None,
        min_window_secs: float = 240.0,
        max_window_secs: float = 360.0,
    ):
        self._base = clob_api_url.rstrip("/")
        self._keywords = [k.lower() for k in (title_keywords or ["btc", "bitcoin"])]
        self._min_window = min_window_secs
        self._max_window = max_window_secs

    async def discover(self) -> List[MarketIdentity]:
        """
        Fetch and filter active BTC 5m markets.
        Returns empty list on any API failure — never crashes caller.
        """
        if not AIOHTTP_AVAILABLE:
            logger.error("aiohttp not installed — cannot discover markets")
            return []

        raw_markets = await self._fetch_all_active_markets()
        logger.info("Fetched %d raw active markets from CLOB", len(raw_markets))

        results: List[MarketIdentity] = []
        for raw in raw_markets:
            identity = self._parse_market(raw)
            if identity is not None:
                results.append(identity)

        logger.info("Filtered to %d BTC 5m markets", len(results))
        return results

    async def _fetch_all_active_markets(self) -> List[Dict[str, Any]]:
        """Paginate through CLOB markets endpoint, collecting all active markets."""
        all_markets: List[Dict[str, Any]] = []
        next_cursor: Optional[str] = None
        pages_fetched = 0

        try:
            async with aiohttp.ClientSession() as session:
                while True:
                    params: Dict[str, Any] = {
                        "active": "true",
                        "closed": "false",
                        "limit": PAGE_LIMIT,
                    }
                    if next_cursor:
                        params["next_cursor"] = next_cursor

                    try:
                        async with session.get(
                            f"{self._base}/markets",
                            params=params,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            if resp.status != 200:
                                logger.error("CLOB markets API returned HTTP %d", resp.status)
                                break

                            data = await resp.json()
                            page_markets = data.get("data", [])
                            all_markets.extend(page_markets)
                            pages_fetched += 1

                            next_cursor = data.get("next_cursor")
                            # Cursor = "" or None or "LTE=" means no more pages
                            if not next_cursor or next_cursor in ("", "LTE="):
                                break

                            # Safety: stop after 20 pages
                            if pages_fetched >= 20:
                                logger.warning("Hit 20-page limit during market discovery")
                                break

                    except asyncio.TimeoutError:
                        logger.error("Timeout fetching CLOB markets page %d", pages_fetched + 1)
                        break
                    except Exception as exc:
                        logger.error("Error fetching CLOB markets: %s", exc)
                        break

        except Exception as exc:
            logger.error("Session error in market discovery: %s", exc)

        return all_markets

    def _parse_market(self, raw: Dict[str, Any]) -> Optional[MarketIdentity]:
        """
        Parse a raw CLOB market dict into MarketIdentity.
        Returns None if the market is incomplete, ambiguous, or not a BTC 5m market.
        Logs every rejection with reason.
        """
        condition_id = raw.get("condition_id") or raw.get("conditionId")
        if not condition_id:
            logger.debug("Skipping market: no condition_id")
            return None

        question = raw.get("question") or raw.get("title") or ""

        # Keyword filter
        if not any(kw in question.lower() for kw in self._keywords):
            return None

        # Window time parsing
        start_ts = self._parse_timestamp(
            raw.get("game_start_time") or raw.get("gameStartTime") or raw.get("start_date_iso")
        )
        end_ts = self._parse_timestamp(
            raw.get("end_date_iso") or raw.get("endDateIso") or raw.get("end_date")
        )

        if start_ts is None:
            logger.debug("[%s] Skipping: cannot parse window start time", condition_id)
            return None
        if end_ts is None:
            logger.debug("[%s] Skipping: cannot parse window end time", condition_id)
            return None

        # Window duration filter
        duration = end_ts - start_ts
        if not (self._min_window <= duration <= self._max_window):
            logger.debug(
                "[%s] Skipping: window duration %.0fs outside [%.0f, %.0f]",
                condition_id, duration, self._min_window, self._max_window,
            )
            return None

        # Token ID extraction
        tokens = raw.get("tokens", [])
        if len(tokens) < 2:
            logger.warning("[%s] Skipping: fewer than 2 tokens", condition_id)
            return None

        up_token_id, down_token_id = self._assign_token_roles(tokens, condition_id)
        if up_token_id is None or down_token_id is None:
            logger.warning("[%s] Skipping: cannot assign token roles", condition_id)
            return None

        slug = raw.get("market_slug") or raw.get("slug")

        return MarketIdentity(
            condition_id=condition_id,
            question=question,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            window_start_ts=start_ts,
            window_end_ts=end_ts,
            slug=slug,
            raw_end_date=str(raw.get("end_date_iso") or raw.get("end_date")),
        )

    def _assign_token_roles(
        self, tokens: List[Dict[str, Any]], condition_id: str
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Assign up_token_id and down_token_id from token list.
        Expects outcome labels "Yes"/"Up" → up, "No"/"Down" → down.
        Returns (None, None) if ambiguous.
        """
        up_token = None
        down_token = None

        for tok in tokens:
            token_id = tok.get("token_id") or tok.get("tokenId")
            outcome = (tok.get("outcome") or "").strip()

            if not token_id:
                logger.debug("[%s] Token missing token_id", condition_id)
                continue

            if outcome.lower() in ("yes", "up"):
                up_token = token_id
            elif outcome.lower() in ("no", "down"):
                down_token = token_id
            else:
                logger.debug("[%s] Unrecognized outcome label: %r", condition_id, outcome)

        return up_token, down_token

    def _parse_timestamp(self, raw: Any) -> Optional[float]:
        """
        Parse a timestamp string or epoch number to UTC unix float.
        Returns None on any parse failure.
        """
        if raw is None:
            return None

        # Already a number
        if isinstance(raw, (int, float)):
            return float(raw)

        # String — try ISO 8601 via dateutil
        if isinstance(raw, str):
            if DATEUTIL_AVAILABLE:
                try:
                    dt = dateutil_parser.parse(raw)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except Exception:
                    pass

            # Manual fallback for common formats
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except ValueError:
                    continue

        logger.debug("Cannot parse timestamp: %r", raw)
        return None
