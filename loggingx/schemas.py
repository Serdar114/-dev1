"""
loggingx/schemas.py — Typed event schemas for the JSONL log.

Every event written to the log must use one of these dataclasses.
Serialisation: call event.to_dict() then json.dumps.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


def _now() -> float:
    return time.time()


@dataclass
class BaseEvent:
    event_type: str
    ts: float = field(default_factory=_now)
    window_id: int = 0  # window_start unix seconds; 0 = not yet known

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Discovery events
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryAttemptEvent(BaseEvent):
    event_type: str = "discovery_attempt"
    slug_tried: str = ""
    source: str = ""  # "gamma_slug_current" | "gamma_slug_prev" | "gamma_search"
    found: bool = False
    error: Optional[str] = None


@dataclass
class MarketFoundEvent(BaseEvent):
    event_type: str = "market_found"
    slug: str = ""
    condition_id: str = ""
    up_token_id: str = ""
    down_token_id: str = ""
    source: str = ""


@dataclass
class MarketLostEvent(BaseEvent):
    event_type: str = "market_lost"
    reason: str = ""


# ---------------------------------------------------------------------------
# Metadata events
# ---------------------------------------------------------------------------

@dataclass
class MetadataReadyEvent(BaseEvent):
    event_type: str = "metadata_ready"
    condition_id: str = ""
    tick_size: Optional[float] = None
    min_order_size: Optional[float] = None
    taker_fee_rate: Optional[float] = None
    fee_provenance: str = ""
    readiness: str = ""


@dataclass
class MetadataFailedEvent(BaseEvent):
    event_type: str = "metadata_failed"
    condition_id: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Feed events
# ---------------------------------------------------------------------------

@dataclass
class ChainlinkUpdateEvent(BaseEvent):
    event_type: str = "chainlink_update"
    price: float = 0.0
    oracle_updated_at: float = 0.0
    age_seconds: float = 0.0
    round_id: int = 0


@dataclass
class ChainlinkStaleEvent(BaseEvent):
    event_type: str = "chainlink_stale"
    age_seconds: float = 0.0
    max_age: float = 0.0


@dataclass
class ChainlinkErrorEvent(BaseEvent):
    event_type: str = "chainlink_error"
    error: str = ""


@dataclass
class BinanceConnectedEvent(BaseEvent):
    event_type: str = "binance_connected"


@dataclass
class BinanceDisconnectedEvent(BaseEvent):
    event_type: str = "binance_disconnected"
    error: Optional[str] = None


@dataclass
class MarketWsConnectedEvent(BaseEvent):
    event_type: str = "market_ws_connected"
    up_token_id: str = ""
    down_token_id: str = ""


@dataclass
class MarketWsDisconnectedEvent(BaseEvent):
    event_type: str = "market_ws_disconnected"
    error: Optional[str] = None


@dataclass
class OrderbookSnapshotEvent(BaseEvent):
    event_type: str = "orderbook_snapshot"
    token_id: str = ""
    outcome: str = ""
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    bid_levels: int = 0
    ask_levels: int = 0


# ---------------------------------------------------------------------------
# Window events
# ---------------------------------------------------------------------------

@dataclass
class WindowStartEvent(BaseEvent):
    event_type: str = "window_start"
    window_start: int = 0
    window_end: int = 0


@dataclass
class WindowEndEvent(BaseEvent):
    event_type: str = "window_end"
    window_start: int = 0
    chainlink_price_at_start: Optional[float] = None
    chainlink_price_at_end: Optional[float] = None
    resolution_status: str = ""  # "resolved" | "stale" | "missing" | "unresolved"
    outcome: Optional[str] = None  # "Up" | "Down" | None


# ---------------------------------------------------------------------------
# No-trade events
# ---------------------------------------------------------------------------

@dataclass
class NoTradeEvent(BaseEvent):
    event_type: str = "no_trade"
    reasons: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Hypothetical entry events
# ---------------------------------------------------------------------------

@dataclass
class HypotheticalEntryEvent(BaseEvent):
    event_type: str = "hypothetical_entry"
    side: str = ""               # "Up" | "Down"
    entry_price: float = 0.0     # best ask for that side (taker)
    entry_size_usdc: float = 1.0
    fee_rate: float = 0.0
    fee_provenance: str = ""
    net_payoff_if_win: float = 0.0   # (1 - entry_price - fee_rate) per unit
    net_payoff_if_lose: float = 0.0  # (-entry_price) per unit
    chainlink_price: Optional[float] = None
    binance_mid: Optional[float] = None
    basis_pct: Optional[float] = None
    pair_sum_ask: Optional[float] = None
    bucket: str = ""


# ---------------------------------------------------------------------------
# Resolution events
# ---------------------------------------------------------------------------

@dataclass
class ResolutionEvent(BaseEvent):
    event_type: str = "resolution"
    resolved_window_start: int = 0
    price_at_start: Optional[float] = None
    price_at_end: Optional[float] = None
    chainlink_age_at_resolution: Optional[float] = None
    outcome: Optional[str] = None   # "Up" | "Down" | None
    status: str = ""                # "resolved_canonical" | "stale" | "missing"


# ---------------------------------------------------------------------------
# System events
# ---------------------------------------------------------------------------

@dataclass
class SystemStartEvent(BaseEvent):
    event_type: str = "system_start"
    mode: str = ""
    config_summary: str = ""


@dataclass
class SystemErrorEvent(BaseEvent):
    event_type: str = "system_error"
    module: str = ""
    error: str = ""
