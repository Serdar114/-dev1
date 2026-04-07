"""
rtds_client.py — RTDS oracle feed client (first candidate canonical source).

Design:
  RTDS (Real-Time Data Service) is an oracle feed API — a service endpoint
  that publishes Chainlink-sourced price data via HTTP polling.
  It is NOT the same as reading the on-chain aggregator contract directly.
  That path is handled by chainlink_rpc.py.

  This client polls the configured RTDS endpoint URL and parses the response
  for BTC/USD price data. If no URL is configured (rtds_url: null in config),
  the client is disabled and canonical_source.py goes straight to the RPC path.

  Whether this feed is exactly what the Polymarket resolution engine reads is
  NOT proven by runtime or official documentation. Runtime must probe this
  source, confirm it is available and stable, and log that decision explicitly.
  The canonical_source.py selector records this.

  Response format (configurable):
    The RTDS endpoint must return JSON. Field names are configurable.
    Default field names match common Chainlink feed API conventions:
      "answer"    → raw answer (int, unscaled)
      "updatedAt" → chain update timestamp (unix seconds)
      "roundId"   → round identifier

  Staleness:
    If updatedAt is older than staleness_threshold_secs, freshness = STALE.
    STALE prices must NOT be accepted as canonical truth.
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

from loggingx.schemas import ChainlinkPrice, FreshnessState
from truth.freshness import check_freshness

logger = logging.getLogger("polybot.rtds_client")

BTCUSD_DECIMALS = 8


class RTDSOracleClient:
    """
    Polls an RTDS oracle HTTP endpoint for BTC/USD price data.

    Job: Maintain the latest ChainlinkPrice snapshot from the RTDS oracle feed.
    Input: rtds_url (None = disabled), poll_interval_secs, staleness_threshold_secs
    Output: latest() → ChainlinkPrice | None
    Failure:
      - URL not configured: disabled, latest() always returns None
      - HTTP error / timeout: last known price retained, freshness degrades
      - aiohttp not installed: disabled
    """

    def __init__(
        self,
        rtds_url: Optional[str],
        poll_interval_secs: float = 10.0,
        staleness_threshold_secs: float = 45.0,
        price_field: str = "answer",
        updated_at_field: str = "updatedAt",
        round_id_field: str = "roundId",
        price_decimals: int = BTCUSD_DECIMALS,
    ):
        self._url                 = rtds_url
        self._poll_interval       = poll_interval_secs
        self._staleness_threshold = staleness_threshold_secs
        self._price_field         = price_field
        self._updated_at_field    = updated_at_field
        self._round_id_field      = round_id_field
        self._price_decimals      = price_decimals

        self._latest:  Optional[ChainlinkPrice] = None
        self._running  = False
        self._enabled  = (rtds_url is not None) and AIOHTTP_AVAILABLE

        if not AIOHTTP_AVAILABLE:
            logger.warning("aiohttp not installed — RTDS oracle feed DISABLED")
        elif rtds_url is None:
            logger.info("RTDS oracle feed: no URL configured — DISABLED (will use chainlink_rpc)")
        else:
            logger.info("RTDS oracle feed configured: url=%s", rtds_url)

    def is_enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        """Start polling loop. No-op if disabled."""
        if not self._enabled:
            return
        self._running = True
        logger.info("RTDS poller started (interval=%.1fs url=%s)", self._poll_interval, self._url)
        while self._running:
            await self._poll()
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        if not self._enabled or self._url is None:
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self._url,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("RTDS poll HTTP %d from %s", resp.status, self._url)
                        return
                    data = await resp.json()
                    result = self._parse(data)
                    if result is not None:
                        self._latest = result
                        logger.debug(
                            "RTDS: BTC/USD=%.2f roundId=%d age=%.1fs",
                            result.price_usd, result.round_id, result.age_secs(),
                        )
        except asyncio.TimeoutError:
            logger.warning("RTDS poll timeout (url=%s)", self._url)
        except Exception as exc:
            logger.warning("RTDS poll error: %s", exc)

    def _parse(self, data: dict) -> Optional[ChainlinkPrice]:
        try:
            raw_answer     = data.get(self._price_field)
            raw_updated_at = data.get(self._updated_at_field)
            raw_round_id   = data.get(self._round_id_field, 0)

            if raw_answer is None or raw_updated_at is None:
                logger.warning(
                    "RTDS response missing required fields (answer=%s updatedAt=%s)",
                    raw_answer, raw_updated_at,
                )
                return None

            answer     = int(raw_answer)
            updated_at = float(raw_updated_at)
            round_id   = int(raw_round_id)

            if answer <= 0:
                logger.warning("RTDS: non-positive answer=%d", answer)
                return None

            price_usd   = answer / (10 ** self._price_decimals)
            fetched_now = time.time()
            freshness   = check_freshness(
                last_updated_ts=updated_at,
                max_age_secs=self._staleness_threshold,
            )
            return ChainlinkPrice(
                price_usd=price_usd,
                round_id=round_id,
                updated_at=updated_at,
                fetched_at=fetched_now,
                freshness=freshness,
                source="rtds_oracle_feed",
            )
        except Exception as exc:
            logger.error("RTDS parse error: %s — data=%r", exc, data)
            return None

    def latest(self) -> Optional[ChainlinkPrice]:
        """
        Return latest snapshot with freshness recomputed at read time.
        Returns None if disabled or no data ever received.
        """
        if self._latest is None:
            return None
        freshness = check_freshness(
            last_updated_ts=self._latest.updated_at,
            max_age_secs=self._staleness_threshold,
        )
        return ChainlinkPrice(
            price_usd=self._latest.price_usd,
            round_id=self._latest.round_id,
            updated_at=self._latest.updated_at,
            fetched_at=self._latest.fetched_at,
            freshness=freshness,
            source="rtds_oracle_feed",
        )

    def is_fresh(self) -> bool:
        p = self.latest()
        return p is not None and p.freshness == FreshnessState.FRESH
