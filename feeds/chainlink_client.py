"""
feeds/chainlink_client.py — Chainlink BTC/USD price polling via Polygon RPC.

Canonical settlement truth source.
Polls latestRoundData() on the Chainlink aggregator contract.
Tracks oracle_updated_at (from contract) vs fetched_at (local clock).
Never uses this as fallback truth for another source.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from loggingx.schemas import ChainlinkErrorEvent, ChainlinkUpdateEvent

log = logging.getLogger(__name__)

# Minimal ABI for AggregatorV3Interface
_AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class ChainlinkSnapshot:
    price: Optional[float]
    oracle_updated_at: Optional[float]
    fetched_at: Optional[float]
    round_id: Optional[int]

    def age_seconds(self) -> Optional[float]:
        if self.oracle_updated_at is None:
            return None
        return time.time() - self.oracle_updated_at

    def is_fresh(self, max_age: float) -> bool:
        age = self.age_seconds()
        return age is not None and age <= max_age


class ChainlinkClient:
    """
    Polls Chainlink BTC/USD on Polygon.
    Run .start() to launch background polling thread.
    Use .snapshot() for a thread-safe read.
    """

    def __init__(self, config: dict, buffer=None, event_logger=None) -> None:
        cl_cfg = config["chainlink"]
        self._rpc_url: str = cl_cfg["polygon_rpc"]
        self._address: str = cl_cfg["btc_usd_address"]
        self._decimals: int = int(cl_cfg.get("decimals", 8))
        self._poll_interval: float = float(cl_cfg.get("poll_interval_seconds", 5))
        self._price_min: float = float(cl_cfg.get("price_min", 10000.0))
        self._price_max: float = float(cl_cfg.get("price_max", 500000.0))
        self._buffer = buffer      # ChainlinkBuffer — receives every valid observation
        self._logger = event_logger

        self._lock = threading.Lock()
        self._price: Optional[float] = None
        self._oracle_updated_at: Optional[float] = None
        self._fetched_at: Optional[float] = None
        self._round_id: Optional[int] = None
        self._last_error: Optional[str] = None

        self._stop_event = threading.Event()
        self._contract = None
        self._w3 = None
        # Rate-limit: only log polygon_rpc warnings/updates when they change or on interval
        self._last_warn_logged: float = 0.0        # last error/warning log time
        self._last_logged_price: Optional[float] = None  # last price emitted to event log
        self._last_logged_round: Optional[int] = None    # last round emitted
        _WARN_INTERVAL = 300.0   # re-log polygon_rpc warnings at most once per 5 min
        self._warn_interval = _WARN_INTERVAL

    def _log(self, event) -> None:
        if self._logger:
            self._logger.log(event)

    def _init_web3(self) -> bool:
        try:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
            self._contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(self._address),
                abi=_AGGREGATOR_ABI,
            )
            return True
        except Exception as exc:
            log.error("Web3 init failed: %s", exc)
            self._last_error = str(exc)
            return False

    def _fetch_once(self) -> None:
        now = time.time()
        try:
            if self._contract is None:
                if not self._init_web3():
                    return
            round_data = self._contract.functions.latestRoundData().call()
            # (roundId, answer, startedAt, updatedAt, answeredInRound)
            round_id = int(round_data[0])
            answer = int(round_data[1])
            updated_at = float(round_data[3])
            price = answer / (10 ** self._decimals)

            # Sanity check
            if not (self._price_min <= price <= self._price_max):
                log.warning("Chainlink price %f outside sanity range", price)
                self._log(ChainlinkErrorEvent(
                    error=f"price_out_of_range:{price}",
                ))
                return

            now = time.time()  # reassign for precise fetch timestamp
            with self._lock:
                self._price = price
                self._oracle_updated_at = updated_at
                self._fetched_at = now
                self._round_id = round_id
                self._last_error = None

            # Record in buffer so resolution can find close-capture observation
            if self._buffer is not None:
                self._buffer.record(updated_at, price, now, "polygon_rpc")

            # Only emit event log when round or price changes (avoid 5s spam)
            if round_id != self._last_logged_round or price != self._last_logged_price:
                self._log(ChainlinkUpdateEvent(
                    price=price,
                    oracle_updated_at=updated_at,
                    age_seconds=now - updated_at,
                    round_id=round_id,
                ))
                self._last_logged_round = round_id
                self._last_logged_price = price

        except Exception as exc:
            err = str(exc)
            # Log at DEBUG — polygon_rpc is the audit/fallback path.
            # When RTDS is primary and healthy, these errors are noise.
            # Rate-limit even at DEBUG to keep log files clean.
            if now - self._last_warn_logged >= self._warn_interval:
                log.debug("polygon_rpc fetch error (audit path): %s", err)
                self._last_warn_logged = now
            with self._lock:
                self._last_error = err

    def run(self) -> None:
        """Background polling loop. Run in a daemon thread."""
        if not self._init_web3():
            log.error("ChainlinkClient: web3 init failed, polling loop will retry")
        while not self._stop_event.is_set():
            self._fetch_once()
            self._stop_event.wait(timeout=self._poll_interval)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="chainlink-poll")
        t.start()
        return t

    def stop(self) -> None:
        self._stop_event.set()

    def snapshot(self) -> ChainlinkSnapshot:
        with self._lock:
            return ChainlinkSnapshot(
                price=self._price,
                oracle_updated_at=self._oracle_updated_at,
                fetched_at=self._fetched_at,
                round_id=self._round_id,
            )

    def inject_state(self, state) -> None:
        """Write current reading into SystemState under its lock."""
        snap = self.snapshot()
        with state._lock:
            state.chainlink.price = snap.price
            state.chainlink.oracle_updated_at = snap.oracle_updated_at
            state.chainlink.fetched_at = snap.fetched_at
            state.chainlink.round_id = snap.round_id
            state.chainlink.source = "polygon_rpc"
