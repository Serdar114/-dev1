"""
market_inspector.py — Debug utility: raw JSON dump of first N markets from CLOB.

Design:
  Debug-mode only. Dumps raw API market objects to a JSONL file so that:
    - Token outcome labels can be verified ("Up"/"Down" vs "Yes"/"No")
    - Fee fields visible in the market object can be audited
    - Window time field names can be confirmed

  Not used in production path. Enable via config: debug.market_inspector=true
  Output: logs/discovery_dump.jsonl
"""

from __future__ import annotations
import json
import logging
import time
from typing import List, Dict, Any, Optional

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

logger = logging.getLogger("polybot.market_inspector")


class MarketInspector:
    """
    Fetches and dumps raw market JSON for debugging.

    Job: write first N markets matching keyword filter to JSONL for manual inspection.
    Input: clob_api_url, output_path, max_markets
    Output: JSONL file with one raw market object per line
    """

    def __init__(
        self,
        clob_api_url: str = "https://clob.polymarket.com",
        output_path: str = "logs/discovery_dump.jsonl",
        max_markets: int = 20,
        title_keywords: Optional[List[str]] = None,
    ):
        self._base = clob_api_url.rstrip("/")
        self._output_path = output_path
        self._max_markets = max_markets
        self._keywords = [k.lower() for k in (title_keywords or ["btc", "bitcoin"])]

    async def dump(self) -> int:
        """
        Fetch raw markets and write to JSONL.
        Returns count of markets written.
        """
        if not AIOHTTP_AVAILABLE:
            logger.error("aiohttp not installed — market inspector unavailable")
            return 0

        markets = await self._fetch_markets()
        count = 0

        try:
            with open(self._output_path, "a", encoding="utf-8") as f:
                for raw in markets:
                    question = (raw.get("question") or raw.get("title") or "").lower()
                    if not any(kw in question for kw in self._keywords):
                        continue
                    line = json.dumps({
                        "ts": time.time(),
                        "event": "raw_market_dump",
                        "market": raw,
                    })
                    f.write(line + "\n")
                    count += 1
                    if count >= self._max_markets:
                        break
        except IOError as exc:
            logger.error("Failed to write discovery dump: %s", exc)
            return 0

        logger.info("MarketInspector: wrote %d markets to %s", count, self._output_path)
        return count

    async def _fetch_markets(self) -> List[Dict[str, Any]]:
        markets: List[Dict[str, Any]] = []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base}/markets",
                    params={"active": "true", "closed": "false", "limit": 50},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.error("CLOB markets HTTP %d", resp.status)
                        return []
                    data = await resp.json()
                    markets = data.get("data", [])
        except Exception as exc:
            logger.error("MarketInspector fetch error: %s", exc)
        return markets
