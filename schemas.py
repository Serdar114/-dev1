"""
schemas.py — Shared data types for the Polymarket BTC feasibility observation run.

All timestamps are epoch milliseconds UTC.
All price/size fields are float or None (never hardcoded defaults).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(d: Any) -> Any:
    """Recursively strip dataclass instances to plain dicts, preserving None."""
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_clean(i) for i in d]
    return d


def to_jsonl(obj: Any) -> str:
    """Serialize a dataclass (or plain dict) to a JSONL line."""
    if hasattr(obj, "__dataclass_fields__"):
        return json.dumps(_clean(asdict(obj)), separators=(",", ":"))
    return json.dumps(_clean(obj), separators=(",", ":"))


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

@dataclass
class TokenInfo:
    token_id: str
    outcome: str                     # e.g. "Yes"/"No" or "Up"/"Down"
    price: Optional[float] = None   # indicative from Gamma, not trading price


@dataclass
class MarketRecord:
    """Normalised representation of a single Polymarket market."""

    # Identity
    market_id: str                          # Gamma market id
    market_slug: Optional[str]
    event_slug: Optional[str]
    condition_id: Optional[str]
    question: Optional[str]

    # Token pair
    tokens: List[TokenInfo] = field(default_factory=list)

    # Timing (epoch ms)
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    resolution_time: Optional[int] = None

    # State flags
    active: Optional[bool] = None
    closed: Optional[bool] = None
    accepting_orders: Optional[bool] = None

    # Microstructure (discoverable, never assumed)
    minimum_tick_size: Optional[float] = None
    neg_risk: Optional[bool] = None
    fees_enabled: Optional[bool] = None
    fee_rate_bps: Optional[float] = None        # from market object if present
    min_order_size: Optional[float] = None      # from market object if present
    min_incentive_size: Optional[float] = None
    max_incentive_spread: Optional[float] = None

    # Raw preservation for unknown fields
    raw_fields: Dict[str, Any] = field(default_factory=dict)

    # Parse health
    parse_warnings: List[str] = field(default_factory=list)
    excluded: bool = False
    exclusion_reason: Optional[str] = None
    selected: bool = False


# ---------------------------------------------------------------------------
# Book snapshot
# ---------------------------------------------------------------------------

@dataclass
class BookSnapshot:
    ts_local: int                           # epoch ms, local receive time
    token_id: str
    market_slug: Optional[str]
    side_label: Optional[str]              # "up" | "down" | "yes" | "no"
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    spread_abs: Optional[float]
    spread_pct: Optional[float]
    book_timestamp: Optional[int]          # from exchange if available, epoch ms
    book_age_ms: Optional[int]             # ts_local - book_timestamp
    book_state_flags: List[str] = field(default_factory=list)
    # e.g. ["stale", "empty_ask", "empty_bid", "crossed", "malformed"]


# ---------------------------------------------------------------------------
# External reference price
# ---------------------------------------------------------------------------

@dataclass
class ExternalPriceSnapshot:
    ts_local: int           # epoch ms
    price: Optional[float]
    source_ts: Optional[int]    # exchange timestamp if available
    data_age_ms: Optional[int]  # ts_local - source_ts
    source: str                 # e.g. "binance_ws", "binance_rest"
    symbol: str = "BTCUSDT"


# ---------------------------------------------------------------------------
# Joined observation (2-second cadence output)
# ---------------------------------------------------------------------------

@dataclass
class JoinedObservation:
    ts_local: int

    # Market identity
    market_id: Optional[str]
    market_slug: Optional[str]
    window_start: Optional[int]     # epoch ms
    window_end: Optional[int]       # epoch ms

    # Token ids
    up_token_id: Optional[str]
    down_token_id: Optional[str]

    # Up side
    up_best_bid: Optional[float]
    up_best_ask: Optional[float]
    up_bid_size: Optional[float]
    up_ask_size: Optional[float]
    up_spread_pct: Optional[float]

    # Down side
    down_best_bid: Optional[float]
    down_best_ask: Optional[float]
    down_bid_size: Optional[float]
    down_ask_size: Optional[float]
    down_spread_pct: Optional[float]

    # Pair-level computed
    pair_best_ask_sum: Optional[float]   # up_best_ask + down_best_ask
    pair_best_bid_sum: Optional[float]   # up_best_bid + down_best_bid

    # External reference
    external_btc_price: Optional[float]
    external_data_age_ms: Optional[int]

    # Observational gate flags (never used to block Day 1-2; logged only)
    stale_external: bool = False
    stale_book_up: bool = False
    stale_book_down: bool = False
    empty_up_ask: bool = False
    empty_down_ask: bool = False
    crossed_up: bool = False
    crossed_down: bool = False
    market_not_ready: bool = False
    fee_unknown: bool = False
    tick_unknown: bool = False
    min_size_unknown: bool = False


# ---------------------------------------------------------------------------
# Fee context — split into theoretical (Day 1-2) and future execution (Day N+)
# ---------------------------------------------------------------------------

@dataclass
class FeeContext:
    # --- Theoretical fee context (populated in Day 1-2) ---
    fees_enabled: Optional[bool]
    fee_schedule_source: Optional[str]      # "gamma_market_object" | "clob_endpoint" | "unknown"
    fee_rate_lookup_value: Optional[float]  # raw value from lookup
    fee_rate_lookup_units: Optional[str]    # "bps" | "pct" | "unknown"
    fee_lookup_ts: Optional[int]            # epoch ms when lookup was done
    fee_lookup_status: str                  # "ok" | "unknown" | "error" | "not_attempted"
    fee_formula_version: Optional[str]      # if discoverable

    # --- Future execution fee context (always None in Day 1-2) ---
    actual_fee_rate_bps: None = None
    actual_fee_amount: None = None
    actual_fee_currency: None = None
    maker_or_taker: None = None
    order_response_fee_fields_raw: None = None
    order_id: None = None
    execution_ts: None = None


# ---------------------------------------------------------------------------
# Future execution schema stub — schema-ready, no values in Day 1-2
# ---------------------------------------------------------------------------

@dataclass
class ExecutionRecord:
    """
    Schema placeholder for future execution logging.
    All fields are None during Day 1-2 zero-order run.
    Emit to future_execution_schema.jsonl so later phases can extend without redesign.
    """
    ts_local: int
    record_type: str = "execution_schema_v1"

    # Order identity
    order_id: None = None
    client_order_id: None = None

    # Fill outcome
    fill_status: None = None           # "filled" | "partial" | "no_fill" | "rejected"
    filled_size: None = None
    filled_price: None = None
    execution_ts: None = None
    post_only_rejected: None = None

    # Pair completion tracking
    leg: None = None                   # "up" | "down"
    pair_id: None = None               # shared id to join legs
    pair_complete: None = None

    # Fee actuals
    actual_fee_rate_bps: None = None
    actual_fee_amount: None = None
    actual_fee_currency: None = None
    maker_or_taker: None = None
    order_response_fee_fields_raw: None = None

    # Adverse selection proxy
    mid_at_entry: None = None
    mid_at_exit: None = None

    # Cancel/cancel-all tracking
    cancel_reason: None = None
    cancel_all_triggered: None = None

    # Heartbeat miss count at time of order
    heartbeat_miss_count_at_order: None = None

    # Reconnect count at time of order
    reconnect_count_at_order: None = None
