"""
schemas.py — All structured data types for polybot_zero.

Design principle:
  Every field that could be None must be explicitly Optional.
  No field may silently default to a canonical value.
  Provenance fields say WHERE data came from, not just WHAT it is.
  Stale/missing data must be named explicitly.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any
import time


# ─────────────────────────────────────────────
# ENUMS (as string constants — no enum import needed, explicit is fine)
# ─────────────────────────────────────────────

class MarketStatus:
    PENDING   = "PENDING"     # window not yet open
    LIVE      = "LIVE"        # window is open, inside trading period
    EXPIRING  = "EXPIRING"    # < 30s to close
    CLOSED    = "CLOSED"      # window closed, awaiting resolution
    RESOLVED  = "RESOLVED"    # resolution confirmed
    UNKNOWN   = "UNKNOWN"     # status cannot be determined


class FreshnessState:
    FRESH   = "FRESH"
    STALE   = "STALE"
    MISSING = "MISSING"


class FeeProvenance:
    CONFIRMED  = "CONFIRMED"   # feeRateBps from /fee-rate endpoint, non-zero
    ZERO       = "ZERO"        # feeRateBps=0 from API (suspicious, accepted)
    UNRESOLVED = "UNRESOLVED"  # endpoint failed or no data — no-trade required


class ResolutionOutcome:
    UP         = "UP"
    DOWN       = "DOWN"
    UNRESOLVED = "UNRESOLVED"   # truth could not be captured


class NoTradeReasonCode:
    # Chainlink/truth issues
    CHAINLINK_STALE          = "CHAINLINK_STALE"
    CHAINLINK_MISSING        = "CHAINLINK_MISSING"
    WINDOW_OPEN_NOT_CAPTURED = "WINDOW_OPEN_NOT_CAPTURED"
    WINDOW_CLOSE_NOT_CAPTURABLE = "WINDOW_CLOSE_NOT_CAPTURABLE"

    # Metadata issues
    METADATA_INCOMPLETE      = "METADATA_INCOMPLETE"
    FEE_PROVENANCE_UNCLEAR   = "FEE_PROVENANCE_UNCLEAR"
    TICK_SIZE_MISSING        = "TICK_SIZE_MISSING"
    MIN_ORDER_MISSING        = "MIN_ORDER_MISSING"

    # Book issues
    BOOK_INSUFFICIENT        = "BOOK_INSUFFICIENT"
    SPREAD_TOO_WIDE          = "SPREAD_TOO_WIDE"
    PAIR_SUM_SUSPICIOUS      = "PAIR_SUM_SUSPICIOUS"

    # Timing issues
    TOO_CLOSE_TO_EXPIRY      = "TOO_CLOSE_TO_EXPIRY"
    MARKET_NOT_LIVE          = "MARKET_NOT_LIVE"

    # Strategy issues
    BUCKET_NOT_PROVEN        = "BUCKET_NOT_PROVEN"
    NO_QUALIFYING_SIDE       = "NO_QUALIFYING_SIDE"
    POSITION_LIMIT_REACHED   = "POSITION_LIMIT_REACHED"

    # System issues
    INTERNAL_ERROR           = "INTERNAL_ERROR"


class EventType:
    MARKET_DISCOVERED    = "MARKET_DISCOVERED"
    MARKET_EXPIRED       = "MARKET_EXPIRED"
    METADATA_FETCHED     = "METADATA_FETCHED"
    METADATA_FAILED      = "METADATA_FAILED"
    WINDOW_OPEN          = "WINDOW_OPEN"
    WINDOW_CLOSE         = "WINDOW_CLOSE"
    CHAINLINK_UPDATE     = "CHAINLINK_UPDATE"
    CHAINLINK_STALE      = "CHAINLINK_STALE"
    BINANCE_UPDATE       = "BINANCE_UPDATE"
    BOOK_UPDATE          = "BOOK_UPDATE"
    FEATURE_BUILT        = "FEATURE_BUILT"
    NO_TRADE             = "NO_TRADE"
    HYPOTHETICAL_ENTRY   = "HYPOTHETICAL_ENTRY"
    RESOLUTION           = "RESOLUTION"
    BUCKET_ASSIGNED      = "BUCKET_ASSIGNED"
    PAPER_TRADE_OPEN     = "PAPER_TRADE_OPEN"
    PAPER_TRADE_CLOSE    = "PAPER_TRADE_CLOSE"
    SYSTEM_START         = "SYSTEM_START"
    SYSTEM_STOP          = "SYSTEM_STOP"
    ERROR                = "ERROR"


# ─────────────────────────────────────────────
# MARKET CORE
# ─────────────────────────────────────────────

@dataclass
class MarketIdentity:
    """Canonical identity for a BTC 5m market. Never guessed; always from API."""
    condition_id:   str
    question:       str
    up_token_id:    str   # "Yes" outcome token
    down_token_id:  str   # "No" outcome token
    window_start_ts: float  # UTC unix
    window_end_ts:   float  # UTC unix
    slug:           Optional[str] = None
    raw_end_date:   Optional[str] = None  # original string from API, for audit


@dataclass
class MarketMetadata:
    """
    Metadata required for safe interpretation of this market.
    All fields are Optional with explicit provenance.
    Missing fields must trigger no-trade for the relevant rule.
    """
    condition_id:        str
    fees_enabled:        Optional[bool]   = None
    fee_rate:            Optional[float]  = None   # e.g. 0.02 = 2%
    fee_source:          Optional[str]    = None   # e.g. "api:clob/markets"
    tick_size:           Optional[float]  = None
    min_order_size:      Optional[float]  = None
    active:              Optional[bool]   = None
    closed:              Optional[bool]   = None
    fetched_at:          Optional[float]  = None   # UTC unix
    fetch_error:         Optional[str]    = None   # non-None = fetch failed


# ─────────────────────────────────────────────
# PRICE FEEDS
# ─────────────────────────────────────────────

@dataclass
class ChainlinkPrice:
    """Canonical settlement truth source. Never fallback, never guessed."""
    price_usd:    float
    round_id:     int
    updated_at:   float   # UTC unix from chain
    fetched_at:   float   # UTC unix when we got it
    freshness:    str     # FreshnessState
    source:       str = "rtds_chainlink_btcusd"

    def age_secs(self) -> float:
        return time.time() - self.updated_at


@dataclass
class BinancePrice:
    """Auxiliary observation. NOT canonical settlement truth."""
    bid:        float
    ask:        float
    fetched_at: float   # UTC unix
    freshness:  str     # FreshnessState
    source:     str = "binance_ws_btcusdt_bookticker"

    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def age_secs(self) -> float:
        return time.time() - self.fetched_at


# ─────────────────────────────────────────────
# ORDER BOOK
# ─────────────────────────────────────────────

@dataclass
class PriceLevel:
    price: float
    size:  float


@dataclass
class OrderBookSnapshot:
    token_id:   str
    bids:       List[PriceLevel]   # sorted best-first (highest price first)
    asks:       List[PriceLevel]   # sorted best-first (lowest price first)
    updated_at: float              # UTC unix
    source:     str = "polymarket_ws_clob"

    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    def spread(self) -> Optional[float]:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb is None or ba is None:
            return None
        return ba - bb

    def is_valid(self, min_bid_levels: int = 1, min_ask_levels: int = 1) -> bool:
        return len(self.bids) >= min_bid_levels and len(self.asks) >= min_ask_levels


# ─────────────────────────────────────────────
# WINDOW TRUTH
# ─────────────────────────────────────────────

@dataclass
class WindowTruth:
    """
    Exact open/close Chainlink captures for a market window.
    If capture failed, the field is None and the flag says why.
    UNRESOLVED is explicit — never guessed.
    """
    condition_id:        str
    window_start_ts:     float
    window_end_ts:       float

    chainlink_open:      Optional[float] = None   # price at open
    chainlink_open_at:   Optional[float] = None   # actual capture time
    chainlink_open_ok:   bool = False             # True = captured within tolerance

    chainlink_close:     Optional[float] = None   # price at close
    chainlink_close_at:  Optional[float] = None
    chainlink_close_ok:  bool = False

    outcome:             str = ResolutionOutcome.UNRESOLVED
    outcome_captured_at: Optional[float] = None

    open_capture_error:  Optional[str] = None
    close_capture_error: Optional[str] = None


# ─────────────────────────────────────────────
# FEATURE VECTOR
# ─────────────────────────────────────────────

@dataclass
class FeatureVector:
    """
    Per-tick feature state for one market.
    All fields must be populated from real data or explicitly None.
    No field may be silently defaulted from a prior window.
    """
    # Identity
    condition_id:    str
    up_token_id:     str
    down_token_id:   str
    window_start_ts: float
    window_end_ts:   float
    built_at:        float   # when this vector was assembled

    # Timing
    secs_to_expiry:  Optional[float] = None

    # Chainlink (canonical)
    chainlink_open:       Optional[float] = None
    chainlink_now:        Optional[float] = None
    chainlink_delta_bps:  Optional[float] = None   # (now-open)/open * 10000
    chainlink_freshness:  str = FreshnessState.MISSING

    # Binance (auxiliary)
    binance_bid:          Optional[float] = None
    binance_ask:          Optional[float] = None
    binance_delta_bps:    Optional[float] = None   # vs chainlink_open
    binance_freshness:    str = FreshnessState.MISSING

    # Basis
    basis_bps:            Optional[float] = None   # chainlink_now vs binance_mid

    # Book — Up token
    up_best_bid:    Optional[float] = None
    up_best_ask:    Optional[float] = None
    up_spread:      Optional[float] = None

    # Book — Down token
    down_best_bid:  Optional[float] = None
    down_best_ask:  Optional[float] = None
    down_spread:    Optional[float] = None

    # Pair structure
    pair_sum_best_ask: Optional[float] = None   # up_ask + down_ask

    # Canonical price source (always "rtds_chainlink_btcusd" in this system)
    canonical_price_source: Optional[str] = None

    # Fees
    fees_enabled:    Optional[bool]  = None
    fee_rate:        Optional[float] = None
    fee_source:      Optional[str]   = None
    fee_provenance:  str = FeeProvenance.UNRESOLVED
    effective_fee:   Optional[float] = None   # curve fee at entry price

    # Metadata flags
    tick_size:       Optional[float] = None
    min_order_size:  Optional[float] = None

    # Completeness flag
    is_tradeable:    bool = False   # True only if all canonical fields are present+fresh


# ─────────────────────────────────────────────
# HYPOTHETICAL ENTRY
# ─────────────────────────────────────────────

@dataclass
class HypotheticalEntry:
    """
    Records what WOULD have happened if we entered a position.
    Fill assumption is explicit and conservative (taker at best ask).
    This is measurement data, not trading data.
    """
    condition_id:    str
    window_start_ts: float
    window_end_ts:   float
    recorded_at:     float   # when entry was computed

    # What side and at what price
    side:                str            # "UP" or "DOWN"
    entry_token_id:      str
    entry_price:         float          # best ask at time of hypothetical entry
    fill_assumption:     str            # e.g. "taker_best_ask"
    fill_is_optimistic:  bool           # True if fill may be unrealistically good

    # Costs
    stake_usdc:          float
    fee_rate:            float
    fee_provenance:      str = FeeProvenance.UNRESOLVED
    effective_fee_usdc:  float = 0.0   # curve: stake * feeRate * p * (1-p)
    effective_cost_usdc: float = 0.0   # stake + fee

    # Fee curve inputs (for audit)
    fee_curve_inputs:    Optional[Dict[str, Any]] = None  # {feeRate, p, stake}
    max_payout_usdc:     float = 0.0    # 1.0 * quantity (= stake / entry_price)

    # Outcome (filled after resolution)
    outcome_known:       bool = False
    actual_outcome:      Optional[str]  = None   # ResolutionOutcome
    hypothetical_correct: Optional[bool] = None
    gross_pnl_usdc:      Optional[float] = None
    net_pnl_usdc:        Optional[float] = None

    # Feature snapshot at entry time
    feature_snapshot:    Optional[Dict[str, Any]] = None
    bucket_id:           Optional[str] = None
    no_trade_reason:     Optional[str] = None   # None = trade would be taken


# ─────────────────────────────────────────────
# BUCKET
# ─────────────────────────────────────────────

@dataclass
class BucketObservation:
    bucket_id:       str
    condition_id:    str
    window_start_ts: float
    features:        Dict[str, Any]
    outcome:         Optional[str]    # ResolutionOutcome once known
    hypothetical:    Optional[HypotheticalEntry] = None


@dataclass
class BucketStats:
    bucket_id:         str
    n_observations:    int = 0
    n_up_correct:      int = 0
    n_down_correct:    int = 0
    n_no_trade:        int = 0
    total_net_pnl:     float = 0.0
    avg_net_pnl:       float = 0.0
    win_rate:          float = 0.0
    has_edge:          bool = False    # True only if above net_edge threshold
    edge_confidence:   str = "INSUFFICIENT_DATA"


# ─────────────────────────────────────────────
# LOG EVENTS
# ─────────────────────────────────────────────

@dataclass
class LogEvent:
    event_type:  str
    ts:          float   # UTC unix
    data:        Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "ts": self.ts,
            "data": self.data,
        }


@dataclass
class NoTradeEvent:
    condition_id:    str
    window_start_ts: float
    reason_code:     str
    is_canonical:    bool    # True = canonical truth failure. False = auxiliary failure.
    details:         Dict[str, Any]
    ts:              float = field(default_factory=time.time)


# ─────────────────────────────────────────────
# PAPER TRADE (Phase 2)
# ─────────────────────────────────────────────

@dataclass
class PaperTrade:
    trade_id:        str
    condition_id:    str
    window_start_ts: float
    window_end_ts:   float
    side:            str
    entry_token_id:  str
    entry_price:     float
    stake_usdc:      float
    fee_usdc:        float
    opened_at:       float

    closed_at:       Optional[float] = None
    outcome:         Optional[str]   = None
    gross_pnl_usdc:  Optional[float] = None
    net_pnl_usdc:    Optional[float] = None
    is_open:         bool = True
    bucket_id:       Optional[str]   = None
    entry_rationale: Optional[str]   = None


def dataclass_to_dict(obj) -> dict:
    """Safely convert any dataclass to dict, handling nested dataclasses."""
    if hasattr(obj, '__dataclass_fields__'):
        return asdict(obj)
    return obj
