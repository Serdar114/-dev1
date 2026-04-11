"""
state.py — Central shared state for polybot_zero.

All mutable runtime state lives here.
Threads update fields under the lock.
The terminal UI takes snapshots.
No module should hold the lock for more than microseconds.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Feed state
# ---------------------------------------------------------------------------

@dataclass
class ChainlinkFeed:
    """Canonical BTC/USD truth from Chainlink on Polygon."""
    price: Optional[float] = None
    # Timestamp from the oracle contract (updatedAt from latestRoundData)
    oracle_updated_at: Optional[float] = None
    # When we last successfully polled the contract
    fetched_at: Optional[float] = None
    round_id: Optional[int] = None
    # Last error string for diagnostics
    last_error: Optional[str] = None
    # Which feed produced this reading: "rtds" | "rtds_msg_ts" | "polygon_rpc" | "none"
    # "rtds" = oracle timestamp from RTDS (canonical for close capture)
    # "rtds_msg_ts" = RTDS message receipt time used as ts (not canonical for close capture)
    # "polygon_rpc" = polled latestRoundData directly (audit / fallback)
    source: str = "none"

    def age_seconds(self) -> Optional[float]:
        """Age of oracle data (oracle_updated_at -> now). None if never fetched."""
        if self.oracle_updated_at is None:
            return None
        return time.time() - self.oracle_updated_at

    def is_fresh(self, max_age: float) -> bool:
        age = self.age_seconds()
        return age is not None and age <= max_age

    def poll_age_seconds(self) -> Optional[float]:
        """How long since we last polled (not oracle age)."""
        if self.fetched_at is None:
            return None
        return time.time() - self.fetched_at


@dataclass
class BinanceFeed:
    """Auxiliary BTC/USDT price from Binance. NEVER canonical settlement truth."""
    bid: Optional[float] = None
    ask: Optional[float] = None
    updated_at: Optional[float] = None
    last_error: Optional[str] = None

    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    def age_seconds(self) -> Optional[float]:
        if self.updated_at is None:
            return None
        return time.time() - self.updated_at

    def is_fresh(self, max_age: float) -> bool:
        age = self.age_seconds()
        return age is not None and age <= max_age


# ---------------------------------------------------------------------------
# Market record
# ---------------------------------------------------------------------------

@dataclass
class MarketRecord:
    """A discovered Polymarket BTC 5m up/down market."""
    slug: str = ""
    condition_id: str = ""
    up_token_id: str = ""
    down_token_id: str = ""
    up_outcome: str = "Up"
    down_outcome: str = "Down"
    window_start: int = 0       # unix seconds UTC, floor(ts/300)*300
    window_end: int = 0         # window_start + 300
    discovery_source: str = ""  # "gamma_slug_current" | "gamma_slug_prev" | "gamma_search"
    discovered_at: float = field(default_factory=time.time)
    raw_gamma_response: Optional[dict] = None   # raw Gamma API response for metadata extraction


# ---------------------------------------------------------------------------
# Market metadata
# ---------------------------------------------------------------------------

@dataclass
class MarketMetadata:
    """CLOB + Gamma metadata with explicit provenance for each critical field."""
    condition_id: str = ""
    tick_size: Optional[float] = None
    tick_size_provenance: str = "missing"       # "canonical" | "missing"
    min_order_size: Optional[float] = None
    min_order_size_provenance: str = "missing"  # "canonical" | "missing"
    taker_fee_rate: Optional[float] = None
    # fee_provenance: "canonical_market_object" > "fallback_config" > "missing"
    fee_provenance: str = "missing"
    fee_schedule_present: bool = False          # True only when feeSchedule object found
    fees_enabled: Optional[bool] = None         # from feesEnabled field in Gamma
    accepting_orders: Optional[bool] = None     # from acceptingOrders in Gamma
    ready: Optional[bool] = None               # from ready field in Gamma market object
    fetched_at: Optional[float] = None
    raw_clob_response: Optional[dict] = None    # stored for audit

    def is_complete(self) -> bool:
        return (
            self.tick_size is not None
            and self.min_order_size is not None
            and self.taker_fee_rate is not None
        )

    def readiness_label(self) -> str:
        if self.is_complete():
            if self.fee_provenance == "canonical_market_object":
                return "READY"
            return "READY_FEE_FALLBACK"
        missing = []
        if self.tick_size is None:
            missing.append("tick_size")
        if self.min_order_size is None:
            missing.append("min_order_size")
        if self.taker_fee_rate is None:
            missing.append("fee_rate")
        return f"INCOMPLETE:{','.join(missing)}"


# ---------------------------------------------------------------------------
# Orderbook side
# ---------------------------------------------------------------------------

@dataclass
class OrderbookSide:
    """One side of a Polymarket binary market (either Up or Down token)."""
    token_id: str = ""
    outcome: str = ""
    # bids sorted descending by price (best = index 0)
    bids: List[Tuple[float, float]] = field(default_factory=list)
    # asks sorted ascending by price (best = index 0)
    asks: List[Tuple[float, float]] = field(default_factory=list)
    updated_at: Optional[float] = None
    snapshot_received: bool = False

    def best_bid(self) -> Optional[Tuple[float, float]]:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> Optional[Tuple[float, float]]:
        return self.asks[0] if self.asks else None

    def spread(self) -> Optional[float]:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is None or ba is None:
            return None
        return ba[0] - bb[0]

    def has_asks(self) -> bool:
        return len(self.asks) > 0

    def has_bids(self) -> bool:
        return len(self.bids) > 0

    def age_seconds(self) -> Optional[float]:
        if self.updated_at is None:
            return None
        return time.time() - self.updated_at

    def apply_snapshot(self, bids: List[dict], asks: List[dict]) -> None:
        """Replace full orderbook from a 'book' event."""
        raw_bids = [(float(b["price"]), float(b["size"])) for b in bids]
        raw_asks = [(float(a["price"]), float(a["size"])) for a in asks]
        self.bids = sorted(raw_bids, key=lambda x: -x[0])
        self.asks = sorted(raw_asks, key=lambda x: x[0])
        self.updated_at = time.time()
        self.snapshot_received = True

    def apply_delta(self, changes: List[dict]) -> None:
        """Apply incremental price_change events."""
        bids_map: Dict[float, float] = dict(self.bids)
        asks_map: Dict[float, float] = dict(self.asks)
        for ch in changes:
            price = float(ch["price"])
            size = float(ch["size"])
            side = ch.get("side", "").lower()
            if side in ("buy", "bid"):
                if size == 0.0:
                    bids_map.pop(price, None)
                else:
                    bids_map[price] = size
            elif side in ("sell", "ask"):
                if size == 0.0:
                    asks_map.pop(price, None)
                else:
                    asks_map[price] = size
        self.bids = sorted(bids_map.items(), key=lambda x: -x[0])
        self.asks = sorted(asks_map.items(), key=lambda x: x[0])
        self.updated_at = time.time()


# ---------------------------------------------------------------------------
# Window state
# ---------------------------------------------------------------------------

@dataclass
class WindowState:
    start: int = 0   # unix seconds
    end: int = 0     # unix seconds

    def secs_to_expiry(self) -> int:
        return max(0, self.end - int(time.time()))

    def is_active(self) -> bool:
        now = int(time.time())
        return self.start > 0 and self.start <= now < self.end

    def label(self) -> str:
        if self.start == 0:
            return "NONE"
        import datetime
        s = datetime.datetime.utcfromtimestamp(self.start).strftime("%H:%M:%S")
        e = datetime.datetime.utcfromtimestamp(self.end).strftime("%H:%M:%S")
        return f"{s}→{e} UTC"


# ---------------------------------------------------------------------------
# System state (shared across all threads)
# ---------------------------------------------------------------------------

@dataclass
class SystemState:
    """
    Central shared mutable state.
    All writes must be done under self._lock.
    UI takes a shallow snapshot dict — never holds the lock during rendering.
    """
    # Discovery
    market: Optional[MarketRecord] = None
    metadata: Optional[MarketMetadata] = None

    # Window
    window: WindowState = field(default_factory=WindowState)

    # Feeds
    chainlink: ChainlinkFeed = field(default_factory=ChainlinkFeed)
    binance: BinanceFeed = field(default_factory=BinanceFeed)

    # Orderbook
    up_book: OrderbookSide = field(default_factory=lambda: OrderbookSide(outcome="Up"))
    down_book: OrderbookSide = field(default_factory=lambda: OrderbookSide(outcome="Down"))

    # Analysis results (computed each tick by runner)
    no_trade_reasons: List[str] = field(default_factory=list)

    # Lifecycle events for terminal display (ring buffer, 20 items)
    lifecycle_events: List[str] = field(default_factory=list)

    # Mode
    mode: str = "measurement"

    # Thread safety
    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )

    # ---------------------------------------------------------------------------
    # Derived views (called under lock or on snapshots)
    # ---------------------------------------------------------------------------

    def basis_pct(self) -> Optional[float]:
        cl = self.chainlink.price
        bn = self.binance.mid()
        if cl is None or bn is None or cl == 0.0:
            return None
        return (bn - cl) / cl * 100.0

    def pair_sum_ask(self) -> Optional[float]:
        up_a = self.up_book.best_ask()
        dn_a = self.down_book.best_ask()
        if up_a is None or dn_a is None:
            return None
        return up_a[0] + dn_a[0]

    def pair_sum_bid(self) -> Optional[float]:
        up_b = self.up_book.best_bid()
        dn_b = self.down_book.best_bid()
        if up_b is None or dn_b is None:
            return None
        return up_b[0] + dn_b[0]

    # ---------------------------------------------------------------------------
    # Lifecycle event helper
    # ---------------------------------------------------------------------------

    def push_lifecycle(self, msg: str) -> None:
        """Add a lifecycle event (call under lock)."""
        ts = time.strftime("%H:%M:%S")
        entry = f"{ts} {msg}"
        self.lifecycle_events.append(entry)
        if len(self.lifecycle_events) > 20:
            self.lifecycle_events.pop(0)

    def system_status_label(self, cl_max_age: float, buffer_depth: int = 0) -> tuple:
        """
        Returns (label, blocking_reasons).
        label: "TRUTH-TIGHT" | "DEGRADED" | "MEASUREMENT SCAFFOLD"

        TRUTH-TIGHT requires ALL of:
          - Chainlink source is "rtds" (not polygon_rpc or rtds_msg_ts)
          - Chainlink oracle is fresh
          - Close-capture buffer has >= 1 observation
          - fee_schedule_present (canonical feeSchedule found in Gamma response)
          - tick_size_provenance == "canonical"
          - min_order_size_provenance == "canonical"
        """
        blocking = []

        src = self.chainlink.source
        if src != "rtds":
            blocking.append(f"chainlink_src:{src}")

        cl_age = self.chainlink.age_seconds()
        if cl_age is None:
            blocking.append("chainlink_missing")
        elif cl_age > cl_max_age:
            blocking.append(f"chainlink_stale:{cl_age:.0f}s")

        if buffer_depth == 0:
            blocking.append("close_capture_buffer_empty")

        if self.metadata is None:
            blocking.append("metadata_missing")
        else:
            if not self.metadata.fee_schedule_present:
                blocking.append("fee_schedule_not_found")
            if self.metadata.tick_size_provenance != "canonical":
                blocking.append("tick_not_canonical")
            if self.metadata.min_order_size_provenance != "canonical":
                blocking.append("min_size_not_canonical")

        if not blocking:
            return "TRUTH-TIGHT", []
        if self.market is None:
            return "MEASUREMENT SCAFFOLD", blocking
        return "DEGRADED", blocking
