"""
chainlink_rpc.py — Direct on-chain Chainlink BTC/USD price via Polygon RPC.

Design:
  This is the SECOND CANDIDATE canonical source (fallback).
  It reads directly from the Chainlink BTC/USD aggregator contract on Polygon
  Mainnet via JSON-RPC eth_call → latestRoundData().

  Chainlink BTC/USD aggregator on Polygon Mainnet:
    Contract: 0xc907E116054Ad103354f2D350FD2514433D57F6f
    Decimals: 8 (divide answer by 1e8 to get USD price)
    Function: latestRoundData() → (roundId, answer, startedAt, updatedAt, answeredInRound)

  This path is independent of the RTDS oracle feed path.
  It is used when RTDS is unavailable or returns stale data.
  The canonical source selector decides which path is active; this module
  only fetches and reports — it does not decide its own authority.

  Staleness:
    If updatedAt is older than staleness_threshold_secs, freshness = STALE.
    STALE prices must NOT be used as canonical truth.
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

try:
    from web3 import Web3
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False

from loggingx.schemas import ChainlinkPrice, FreshnessState
from truth.freshness import check_freshness

logger = logging.getLogger("polybot.chainlink_rpc")

BTCUSD_AGGREGATOR = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
BTCUSD_DECIMALS   = 8

AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId",         "type": "uint80"},
            {"name": "answer",          "type": "int256"},
            {"name": "startedAt",       "type": "uint256"},
            {"name": "updatedAt",       "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]


class ChainlinkRPCClient:
    """
    Polls Chainlink BTC/USD on Polygon Mainnet via direct JSON-RPC.

    Job: Maintain the latest ChainlinkPrice snapshot from the on-chain aggregator.
    Input: polygon_rpc_url, poll_interval_secs, staleness_threshold_secs
    Output: latest() → ChainlinkPrice | None
    Failure:
      - RPC error: last known price retained, freshness degrades to STALE/MISSING
      - web3 not installed: all reads return None
    """

    def __init__(
        self,
        polygon_rpc_url: str,
        poll_interval_secs: float = 10.0,
        staleness_threshold_secs: float = 45.0,
        aggregator_address: str = BTCUSD_AGGREGATOR,
    ):
        self._rpc_url             = polygon_rpc_url
        self._poll_interval       = poll_interval_secs
        self._staleness_threshold = staleness_threshold_secs
        self._aggregator_address  = aggregator_address

        self._latest:   Optional[ChainlinkPrice] = None
        self._w3:       Optional[object] = None
        self._contract: Optional[object] = None
        self._running = False

        if not WEB3_AVAILABLE:
            logger.error(
                "web3 not installed — chainlink_rpc feed UNAVAILABLE. "
                "Install web3>=6.0 to enable direct RPC canonical source."
            )
        else:
            self._init_web3()

    def _init_web3(self) -> None:
        try:
            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
            checksum_addr = Web3.to_checksum_address(self._aggregator_address)
            self._contract = self._w3.eth.contract(
                address=checksum_addr,
                abi=AGGREGATOR_ABI,
            )
            logger.info(
                "chainlink_rpc initialized: RPC=%s aggregator=%s",
                self._rpc_url, self._aggregator_address,
            )
        except Exception as exc:
            logger.error("chainlink_rpc init failed: %s", exc)
            self._w3       = None
            self._contract = None

    async def start(self) -> None:
        """Start polling loop. Run as asyncio task."""
        self._running = True
        logger.info("chainlink_rpc poller started (interval=%.1fs)", self._poll_interval)
        while self._running:
            await self._poll()
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        if self._contract is None:
            return
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._fetch_latest_round)
            if result is not None:
                self._latest = result
                logger.debug(
                    "chainlink_rpc: BTC/USD=%.2f roundId=%d age=%.1fs",
                    result.price_usd, result.round_id, result.age_secs(),
                )
        except Exception as exc:
            logger.warning("chainlink_rpc poll error: %s", exc)

    def _fetch_latest_round(self) -> Optional[ChainlinkPrice]:
        try:
            round_id, answer, _started, updated_at, _answered = (
                self._contract.functions.latestRoundData().call()
            )
            if answer <= 0:
                logger.warning("chainlink_rpc: non-positive answer=%d", answer)
                return None

            price_usd   = answer / (10 ** BTCUSD_DECIMALS)
            fetched_now = time.time()
            freshness   = check_freshness(
                last_updated_ts=float(updated_at),
                max_age_secs=self._staleness_threshold,
            )
            return ChainlinkPrice(
                price_usd=price_usd,
                round_id=int(round_id),
                updated_at=float(updated_at),
                fetched_at=fetched_now,
                freshness=freshness,
                source="chainlink_rpc_polygon",
            )
        except Exception as exc:
            logger.error("chainlink_rpc latestRoundData() failed: %s", exc)
            return None

    def latest(self) -> Optional[ChainlinkPrice]:
        """
        Return the latest snapshot with freshness recomputed at read time.
        Returns None if no data ever received.
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
            source="chainlink_rpc_polygon",
        )

    def is_fresh(self) -> bool:
        p = self.latest()
        return p is not None and p.freshness == FreshnessState.FRESH
