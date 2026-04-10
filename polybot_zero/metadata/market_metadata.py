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

  Fee fields:
    Fee rate MUST come from the /fee-rate endpoint per token_id, NOT from the
    market object (market object feeRate/makerBaseFee fields are NOT the correct
    fee rate for fee curve computation).

    Endpoint: GET https://clob.polymarket.com/fee-rate?token_id={token_id}
    Response: {"feeRateBps": <int>}

    We fetch for both up_token_id and down_token_id separately.
    If feeRateBps differs between tokens, we log a warning and use the higher.
    If fetch fails for either token, fee provenance = UNRESOLVED → no-trade.
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
from metadata.fee_schedule import FeeSchedule, FeeProvenance

logger = logging.getLogger("polybot.market_metadata")


class MetadataFetcher:
    """
    Fetches market metadata from Polymarket CLOB REST API.

    Job: retrieve and normalize metadata for a given condition_id.
    Input: condition_id (str), up_token_id (str), down_token_id (str)
    Output: MarketMetadata dataclass
    Failure:
      - HTTP error → MarketMetadata with fetch_error set, all fields None
      - Missing field → field stays None (explicit, not defaulted)
      - fee_rate_bps missing → provenance=UNRESOLVED
    """

    def __init__(
        self,
        clob_api_url: str = "https://clob.polymarket.com",
        fee_rate_endpoint: str = "https://clob.polymarket.com/fee-rate",
    ):
        self._base = clob_api_url.rstrip("/")
        self._fee_rate_endpoint = fee_rate_endpoint.rstrip("/")

    async def fetch(
        self,
        condition_id: str,
        up_token_id: Optional[str] = None,
        down_token_id: Optional[str] = None,
        gamma_fee_rate: Optional[float] = None,
        gamma_fee_source: Optional[str] = None,
    ) -> MarketMetadata:
        """
        Fetch metadata for one market including per-token fee rates.
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
                # Fetch market object
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        meta.fetch_error = f"http_{resp.status}"
                        logger.warning("[%s] Metadata fetch HTTP %d", condition_id, resp.status)
                        return meta

                    data: Dict[str, Any] = await resp.json()
                    meta = self._parse_market(meta, data)

                # Fetch fee rates — Gamma data has precedence over /fee-rate endpoint
                await self._fetch_fee_rates(
                    session, meta, up_token_id, down_token_id,
                    gamma_fee_rate=gamma_fee_rate,
                    gamma_fee_source=gamma_fee_source,
                )

        except asyncio.TimeoutError:
            meta.fetch_error = "timeout"
            logger.warning("[%s] Metadata fetch timeout", condition_id)
        except Exception as exc:
            meta.fetch_error = str(exc)
            logger.error("[%s] Metadata fetch error: %s", condition_id, exc)

        return meta

    def _parse_market(self, meta: MarketMetadata, data: Dict[str, Any]) -> MarketMetadata:
        """
        Parse raw market API response into MarketMetadata.
        Does NOT parse fee rate here — that comes from /fee-rate endpoint.
        """
        meta.fetched_at = time.time()

        meta.active = data.get("active")
        meta.closed = data.get("closed")

        # Tick size
        tick_raw = data.get("minimum_tick_size") or data.get("tickSize")
        if tick_raw is not None:
            try:
                meta.tick_size = float(tick_raw)
            except (ValueError, TypeError):
                logger.warning("[%s] tick_size parse error: %r", meta.condition_id, tick_raw)
        else:
            logger.warning("[%s] tick_size missing from API response", meta.condition_id)

        # Min order size
        min_order_raw = data.get("minimum_order_size") or data.get("minOrderSize")
        if min_order_raw is not None:
            try:
                meta.min_order_size = float(min_order_raw)
            except (ValueError, TypeError):
                logger.warning("[%s] min_order_size parse error: %r", meta.condition_id, min_order_raw)
        else:
            logger.warning("[%s] min_order_size missing from API response", meta.condition_id)

        # feesEnabled flag from market object (informational only)
        fees_enabled_raw = data.get("feesEnabled") or data.get("fees_enabled")
        if fees_enabled_raw is not None:
            meta.fees_enabled = bool(fees_enabled_raw)

        return meta

    async def _fetch_fee_rates(
        self,
        session,
        meta: MarketMetadata,
        up_token_id: Optional[str],
        down_token_id: Optional[str],
        gamma_fee_rate: Optional[float] = None,
        gamma_fee_source: Optional[str] = None,
    ) -> None:
        """
        Resolve fee rate using precedence:
          a) gamma feeSchedule.rate  (decimal from discovery)
          b) gamma takerBaseFee / makerBaseFee  (bps/10000 from discovery)
          c) /fee-rate?token_id endpoint base_fee
          d) UNRESOLVED if all above missing
        """
        # ── Sources (a) and (b): pre-resolved from Gamma during discovery ────
        if gamma_fee_rate is not None:
            meta.fee_rate   = gamma_fee_rate
            meta.fee_source = gamma_fee_source or "gamma"
            logger.info(
                "[%s] FEE_RESOLVED source=%s fee_rate=%.4f (%.1f bps)",
                meta.condition_id, meta.fee_source,
                gamma_fee_rate, gamma_fee_rate * 10000,
            )
            return

        # ── Source (c): /fee-rate?token_id endpoint ──────────────────────────
        if up_token_id is None and down_token_id is None:
            logger.warning("[%s] No token IDs — cannot fetch fee rates", meta.condition_id)
            meta.fee_rate = None
            meta.fee_source = None
            return

        up_bps   = await self._fetch_one_fee_rate(session, up_token_id)   if up_token_id   else None
        down_bps = await self._fetch_one_fee_rate(session, down_token_id) if down_token_id else None

        if up_bps is None and down_bps is None:
            logger.warning("[%s] fee-rate fetch failed for both tokens — UNRESOLVED", meta.condition_id)
            meta.fee_rate = None
            meta.fee_source = None
            return

        if up_bps is not None and down_bps is not None and up_bps != down_bps:
            logger.warning(
                "[%s] Fee rate mismatch: up_bps=%d down_bps=%d — using higher",
                meta.condition_id, up_bps, down_bps,
            )
            bps = max(up_bps, down_bps)
        else:
            bps = up_bps if up_bps is not None else down_bps

        meta.fee_rate   = bps / 10000.0
        meta.fee_source = "clob:/fee-rate:base_fee"
        logger.info(
            "[%s] FEE_RESOLVED source=%s fee_rate=%.4f (%.1f bps)",
            meta.condition_id, meta.fee_source, meta.fee_rate, float(bps),
        )

    async def _fetch_one_fee_rate(self, session, token_id: str) -> Optional[int]:
        """
        GET /fee-rate?token_id={token_id}
        Runtime response shape: {"base_fee": 1000}
        Fallback field names: feeRateBps, fee_rate_bps
        Returns integer bps or None on failure.
        """
        try:
            async with session.get(
                self._fee_rate_endpoint,
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "fee-rate endpoint HTTP %d for token_id=%s",
                        resp.status, token_id,
                    )
                    return None
                data = await resp.json()
                # Accept runtime field first, then legacy names
                raw_bps = (
                    data.get("base_fee")
                    or data.get("feeRateBps")
                    or data.get("fee_rate_bps")
                )
                if raw_bps is None:
                    logger.warning(
                        "fee-rate: no recognised field in response for token_id=%s: %r",
                        token_id, data,
                    )
                    return None
                return int(raw_bps)
        except asyncio.TimeoutError:
            logger.warning("fee-rate fetch timeout for token_id=%s", token_id)
            return None
        except Exception as exc:
            logger.warning("fee-rate fetch error for token_id=%s: %s", token_id, exc)
            return None

    def build_fee_schedule(
        self,
        meta: MarketMetadata,
        token_id: Optional[str] = None,
    ) -> FeeSchedule:
        """Build a FeeSchedule from fetched metadata."""
        if meta.fee_rate is None:
            return FeeSchedule(
                fee_rate_bps=None,
                provenance=FeeProvenance.UNRESOLVED,
                token_id=token_id,
            )
        bps = int(round(meta.fee_rate * 10000))
        return FeeSchedule.from_bps(fee_rate_bps=bps, token_id=token_id)

    def is_metadata_complete(
        self,
        meta: MarketMetadata,
        require_fee: bool = True,
    ) -> tuple[bool, list]:
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
