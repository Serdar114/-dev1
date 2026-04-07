"""
rtds_client.py — Chainlink BTC/USD canonical price feed via Polygon RPC.

Design:
  This is the FIRST OFFICIAL CANDIDATE for canonical settlement truth.
  We read directly from the Chainlink BTC/USD aggregator on Polygon Mainnet.
  Whether this is the exact source the Polymarket resolution engine reads is
  NOT proven from runtime or official documentation and must not be stated as
  fact. Runtime must probe this source, confirm it is available and stable,
  and log that decision. Direct Chainlink RPC is the second candidate source.
  If neither source is confirmed available and fresh, outcome is UNRESOLVED.

  Method: polling via JSON-RPC (eth_call to latestRoundData).
  Why polling not WebSocket: simpler, more reliable, verifiable.
  Poll interval: configurable (default 10s).

  Chainlink BTC/USD on Polygon Mainnet:
    Contract: 0xc907E116054Ad103354f2D350FD2514433D57F6f
    Decimals: 8 (divide answer by 1e8 to get USD price)
    Function: latestRoundData() → (roundId, answer, startedAt, updatedAt, answeredInRound)

  Staleness:
    If updated_at is older than staleness_threshold_secs, the price is STALE.
    STALE prices must NOT be used as canonical truth.
    Missing prices must NOT be used as canonical truth.

  This module holds the latest ChainlinkPrice snapshot.
  Callers read the snapshot — they are responsible for checking freshness.
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

try:
    from web3 import Web3
    from web3.exceptions import ContractLogicError
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False

from loggingx.schemas import ChainlinkPrice, FreshnessState
from truth.freshness import check_freshness

logger = logging.getLogger("polybot.rtds_client")

# Chainlink BTC/USD aggregator on Polygon Mainnet
BTCUSD_AGGREGATOR = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
BTCUSD_DECIMALS = 8

# Minimal ABI for latestRoundData
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


class ChainlinkRTDSClient:
    """
    Polls Chainlink BTC/USD on Polygon Mainnet.

    Job: Maintain the latest fresh ChainlinkPrice snapshot.
    Input: polygon_rpc_url, poll_interval_secs, staleness_threshold_secs
    Output: latest_price (ChainlinkPrice | None), freshness state
    Failure:
      - RPC error: last known price remains, freshness becomes STALE/MISSING
      - web3 not installed: logs error, all reads return None with MISSING state
    """

    def __init__(
        self,
        polygon_rpc_url: str,
        poll_interval_secs: float = 10.0,
        staleness_threshold_secs: float = 45.0,
        aggregator_address: str = BTCUSD_AGGREGATOR,
    ):
        self._rpc_url = polygon_rpc_url
        self._poll_interval = poll_interval_secs
        self._staleness_threshold = staleness_threshold_secs
        self._aggregator_address = aggregator_address

        self._latest: Optional[ChainlinkPrice] = None
        self._w3: Optional[Any] = None
        self._contract: Optional[Any] = None
        self._running = False

        if not WEB3_AVAILABLE:
            logger.error(
                "web3 not installed — Chainlink feed UNAVAILABLE. "
                "Install web3>=6.0 to enable canonical truth source."
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
                "Chainlink client initialized: RPC=%s aggregator=%s",
                self._rpc_url, self._aggregator_address,
            )
        except Exception as exc:
            logger.error("Chainlink client init failed: %s", exc)
            self._w3 = None
            self._contract = None

    async def start(self) -> None:
        """Start polling loop. Run as asyncio task."""
        self._running = True
        logger.info("Chainlink poller started (interval=%.1fs)", self._poll_interval)
        while self._running:
            await self._poll()
            await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        """Fetch latest round data from chain. Update self._latest."""
        if self._contract is None:
            return

        try:
            # Run blocking web3 call in executor to avoid blocking event loop
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._fetch_latest_round)
            if result is not None:
                self._latest = result
                logger.debug(
                    "Chainlink: BTC/USD=%.2f roundId=%d updatedAt=%.0f age=%.1fs",
                    result.price_usd,
                    result.round_id,
                    result.updated_at,
                    result.age_secs(),
                )
        except Exception as exc:
            logger.warning("Chainlink poll error: %s", exc)

    def _fetch_latest_round(self) -> Optional[ChainlinkPrice]:
        """
        Blocking call — runs in executor.
        Returns ChainlinkPrice or None on error.
        """
        try:
            round_id, answer, started_at, updated_at, answered_in_round = (
                self._contract.functions.latestRoundData().call()
            )
            if answer <= 0:
                logger.warning("Chainlink returned non-positive answer: %d", answer)
                return None

            price_usd = answer / (10 ** BTCUSD_DECIMALS)
            fetched_now = time.time()

            freshness = check_freshness(
                last_updated_ts=float(updated_at),
                max_age_secs=self._staleness_threshold,
            )

            return ChainlinkPrice(
                price_usd=price_usd,
                round_id=int(round_id),
                updated_at=float(updated_at),
                fetched_at=fetched_now,
                freshness=freshness,
            )
        except Exception as exc:
            logger.error("latestRoundData() call failed: %s", exc)
            return None

    def latest(self) -> Optional[ChainlinkPrice]:
        """
        Return the latest ChainlinkPrice snapshot with current freshness.
        Freshness is recomputed on read so it degrades as time passes.
        Returns None if we have never received data.
        """
        if self._latest is None:
            return None

        # Recompute freshness based on when the chain last updated the price
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
        )

    def is_fresh(self) -> bool:
        p = self.latest()
        return p is not None and p.freshness == FreshnessState.FRESH

    def price_or_none(self) -> Optional[float]:
        p = self.latest()
        if p and p.freshness == FreshnessState.FRESH:
            return p.price_usd
        return None


# Type hint workaround for web3 optional
try:
    from web3 import Web3 as _W3
    Any = _W3
except ImportError:
    Any = object
