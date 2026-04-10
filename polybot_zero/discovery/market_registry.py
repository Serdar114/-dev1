"""
market_registry.py — In-memory registry of active BTC 5m markets.

Design:
  Single source of truth for which markets are currently tracked.
  Markets are added on discovery, transitioned through states, removed on resolution.
  State transitions are explicit and logged.
  Registry does not fetch data — it stores and serves.
  All lookups return Optional — never raise on missing market.
"""

from __future__ import annotations
import logging
import time
from typing import Dict, List, Optional, Set

from loggingx.schemas import MarketIdentity, MarketMetadata, WindowTruth, MarketStatus

logger = logging.getLogger("polybot.market_registry")


class MarketEntry:
    """Full tracking state for one market."""

    def __init__(self, identity: MarketIdentity):
        self.identity = identity
        self.status = MarketStatus.PENDING
        self.metadata: Optional[MarketMetadata] = None
        self.truth: Optional[WindowTruth] = None
        self.added_at: float = time.time()
        self.status_updated_at: float = time.time()

    def set_status(self, new_status: str) -> None:
        if self.status == new_status:
            return  # no-op: already in this state; suppress duplicate log
        old = self.status
        self.status = new_status
        self.status_updated_at = time.time()
        logger.info("[%s] Status: %s → %s", self.identity.condition_id, old, new_status)

    @property
    def condition_id(self) -> str:
        return self.identity.condition_id

    @property
    def up_token_id(self) -> str:
        return self.identity.up_token_id

    @property
    def down_token_id(self) -> str:
        return self.identity.down_token_id


class MarketRegistry:
    """
    In-memory registry of all actively tracked BTC 5m markets.

    Job: store, update, and serve market state for all tracked markets.
    Input: MarketIdentity from discovery layer, updates from all other layers.
    Output: MarketEntry (or None), lists of markets by status.
    Failure: missing market → None returned, never raised.
    """

    def __init__(self):
        self._markets: Dict[str, MarketEntry] = {}   # condition_id → MarketEntry
        self._token_index: Dict[str, str] = {}        # token_id → condition_id

    def register(self, identity: MarketIdentity) -> bool:
        """
        Register a new market. Returns False if already registered.
        """
        cid = identity.condition_id
        if cid in self._markets:
            return False

        entry = MarketEntry(identity)
        self._markets[cid] = entry
        self._token_index[identity.up_token_id] = cid
        self._token_index[identity.down_token_id] = cid

        logger.info(
            "Registered market %s: %r [%s → %s]",
            cid,
            identity.question[:60],
            _fmt_ts(identity.window_start_ts),
            _fmt_ts(identity.window_end_ts),
        )
        return True

    def get(self, condition_id: str) -> Optional[MarketEntry]:
        return self._markets.get(condition_id)

    def get_by_token(self, token_id: str) -> Optional[MarketEntry]:
        cid = self._token_index.get(token_id)
        if cid:
            return self._markets.get(cid)
        return None

    def set_metadata(self, condition_id: str, metadata: MarketMetadata) -> None:
        entry = self._markets.get(condition_id)
        if entry:
            entry.metadata = metadata

    def set_truth(self, condition_id: str, truth: WindowTruth) -> None:
        entry = self._markets.get(condition_id)
        if entry:
            entry.truth = truth

    def set_status(self, condition_id: str, status: str) -> None:
        entry = self._markets.get(condition_id)
        if entry:
            entry.set_status(status)

    def mark_live(self, condition_id: str) -> None:
        self.set_status(condition_id, MarketStatus.LIVE)

    def mark_expiring(self, condition_id: str) -> None:
        self.set_status(condition_id, MarketStatus.EXPIRING)

    def mark_closed(self, condition_id: str) -> None:
        self.set_status(condition_id, MarketStatus.CLOSED)

    def mark_resolved(self, condition_id: str) -> None:
        self.set_status(condition_id, MarketStatus.RESOLVED)

    def all_markets(self) -> List[MarketEntry]:
        return list(self._markets.values())

    def live_markets(self) -> List[MarketEntry]:
        return [m for m in self._markets.values() if m.status == MarketStatus.LIVE]

    def pending_markets(self) -> List[MarketEntry]:
        return [m for m in self._markets.values() if m.status == MarketStatus.PENDING]

    def active_token_ids(self) -> Set[str]:
        """All token IDs for currently LIVE markets (for WebSocket subscription)."""
        token_ids: Set[str] = set()
        for entry in self.live_markets():
            token_ids.add(entry.up_token_id)
            token_ids.add(entry.down_token_id)
        return token_ids

    def remove(self, condition_id: str) -> None:
        entry = self._markets.pop(condition_id, None)
        if entry:
            self._token_index.pop(entry.up_token_id, None)
            self._token_index.pop(entry.down_token_id, None)
            logger.info("Removed market %s from registry", condition_id)

    def size(self) -> int:
        return len(self._markets)

    def condition_ids(self) -> List[str]:
        return list(self._markets.keys())


def _fmt_ts(ts: float) -> str:
    import datetime
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")
