"""
market_metadata.py — Fetches and validates per-market metadata from Polymarket CLOB.

Design:
  All metadata comes from the API. Nothing is hardcoded as canonical.
  If any required field is missing, the missing fields are recorded explicitly.
  Missing required fields → no-trade (enforced by no_trade_rules, not here).
  This module just fetches and normalizes. It does not make trade decisions.

  Required fields:
    - tick_size (cannot trade without knowing minimum price increment)
    - min_order_size (cannot trade without knowing minimum size)
    - active (market must be active)
    - closed (market must not be closed)

  Fee fields (from API, not hardcoded):
    - feeRate or equivalent — None if not found in response
    - feesEnabled — None if not found
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional, Dict, Any

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

from loggingx.schemas import MarketMetadata
from metadata.fee_schedule import FeeSchedule

logger = logging.getLogger("polybot.market_metadata")

CLOB_MARKET_URL = "https://clob.polymarket.com/markets/{condition_id}"


class MetadataFetcher:
    """
    Fetches market metadata from Polymarket CLOB REST API.

    Job: retrieve and normalize metadata for a given condition_id.
    Input: condition_id (str)
    Output: MarketMetadata dataclass
    Failure:
      - HTTP error → MarketMetadata with fetch_error set, all fields None
      - Missing field → field stays None (explicit, not defaulted)
      - fee_rate missing → fee_rate=None, fee_source=None
    """

    def __init__(self, clob_api_url: str = "https://clob.polymarket.com"):
        self._base = clob_api_url.rstrip("/")

    async def fetch(self, condition_id: str) -> MarketMetadata:
        """
        Fetch metadata for one market.
        Returns MarketMetadata with fetch_error set on any failure.
        Never raises — errors are explicit in the returned object.
        """
        meta = MarketMetadata(condition_id=condition_id, fetched_at=time.time())

        if not AIOHTTP_AVAILABLE:
            meta.fetch_error = "aiohttp_not_installed"
            logger.error("aiohttp not installed — cannot fetch metadata")
            return meta

        url = f"{self._base}/markets/{condition_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        meta.fetch_error = f"http_{resp.status}"
                        logger.warning("[%s] Metadata fetch HTTP %d", condition_id, resp.status)
                        return meta

                    data: Dict[str, Any] = await resp.json()
                    return self._parse(meta, data)

        except asyncio.TimeoutError:
            meta.fetch_error = "timeout"
            logger.warning("[%s] Metadata fetch timeout", condition_id)
        except Exception as exc:
            meta.fetch_error = str(exc)
            logger.error("[%s] Metadata fetch error: %s", condition_id, exc)

        return meta

    def _parse(self, meta: MarketMetadata, data: Dict[str, Any]) -> MarketMetadata:
        """
        Parse raw API response into MarketMetadata.
        Every field is Optional. Missing → None, logged.
        """
        meta.fetched_at = time.time()

        # Market status
        meta.active = data.get("active")
        meta.closed = data.get("closed")

        # Tick size — critical for order placement
        tick_raw = data.get("minimum_tick_size") or data.get("tickSize")
        if tick_raw is not None:
            try:
                meta.tick_size = float(tick_raw)
            except (ValueError, TypeError):
                logger.warning("[%s] tick_size parse error: %r", meta.condition_id, tick_raw)
        else:
            logger.warning("[%s] tick_size missing from API response", meta.condition_id)

        # Min order size — critical for order placement
        min_order_raw = data.get("minimum_order_size") or data.get("minOrderSize")
        if min_order_raw is not None:
            try:
                meta.min_order_size = float(min_order_raw)
            except (ValueError, TypeError):
                logger.warning("[%s] min_order_size parse error: %r", meta.condition_id, min_order_raw)
        else:
            logger.warning("[%s] min_order_size missing from API response", meta.condition_id)

        # Fee fields — must come from API, never hardcoded
        # Polymarket may expose this as "feeRate", "fee_rate", or not at all
        fee_rate_raw = (
            data.get("feeRate")
            or data.get("fee_rate")
            or data.get("makerBaseFee")   # alternative key observed in some APIs
        )
        fees_enabled_raw = data.get("feesEnabled") or data.get("fees_enabled")

        if fee_rate_raw is not None:
            try:
                meta.fee_rate = float(fee_rate_raw)
                meta.fee_source = f"api:clob/markets/{meta.condition_id}"
                meta.fees_enabled = bool(fees_enabled_raw) if fees_enabled_raw is not None else None
            except (ValueError, TypeError):
                logger.warning("[%s] fee_rate parse error: %r", meta.condition_id, fee_rate_raw)
                meta.fee_rate = None
                meta.fee_source = None
        else:
            # Fee rate not found in response
            meta.fee_rate = None
            meta.fee_source = None
            meta.fees_enabled = None
            logger.warning(
                "[%s] fee_rate NOT found in API response — "
                "fee_source=None, no-trade will be required if require_fee_from_api=true",
                meta.condition_id,
            )

        return meta

    def build_fee_schedule(self, meta: MarketMetadata) -> FeeSchedule:
        """Build a FeeSchedule from fetched metadata."""
        return FeeSchedule(
            fee_rate=meta.fee_rate,
            fee_source=meta.fee_source,
            fees_enabled=meta.fees_enabled,
        )

    def is_metadata_complete(self, meta: MarketMetadata, require_fee: bool = True) -> tuple[bool, list]:
        """
        Check if metadata has all required fields.
        Returns (is_complete: bool, missing_fields: list[str])
        """
        missing = []
        if meta.fetch_error is not None:
            return False, [f"fetch_error:{meta.fetch_error}"]
        if meta.tick_size is None:
            missing.append("tick_size")
        if meta.min_order_size is None:
            missing.append("min_order_size")
        if meta.active is None:
            missing.append("active")
        if require_fee and meta.fee_rate is None:
            missing.append("fee_rate")
        if require_fee and meta.fee_source is None:
            missing.append("fee_source")
        return len(missing) == 0, missing
