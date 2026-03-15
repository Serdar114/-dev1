"""
Core data models for polybot_v2 Phase 1.
All domain objects live here; nothing imports from each other here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class MarketSnapshot:
    """Current state of the Polymarket 5m BTC market."""
    condition_id: str
    token_id_yes: str
    token_id_no: str
    best_bid_yes: float          # best bid for YES token
    best_ask_yes: float          # best ask for YES token
    best_bid_no: float
    best_ask_no: float
    last_trade_price_yes: Optional[float]
    window_end_ts: float         # unix ts when this window resolves
    fetched_at: float = field(default_factory=time.time)

    @property
    def implied_yes_prob(self) -> float:
        """Mid of yes ask/bid as implied probability."""
        if self.best_bid_yes <= 0 or self.best_ask_yes <= 0:
            return 0.5
        return (self.best_bid_yes + self.best_ask_yes) / 2.0

    @property
    def seconds_to_expiry(self) -> float:
        return max(0.0, self.window_end_ts - time.time())


@dataclass
class PriceSnapshot:
    """Binance mid-price snapshot."""
    btc_mid: float
    timestamp: float             # unix ts
    realized_vol_60s: float      # rolling 60s annualised-ish vol


@dataclass
class FairProbResult:
    """Output of fair_prob_engine."""
    fair_yes_prob: float
    fair_no_prob: float
    # debug fields
    delta_pct: float
    sigma_eff: float
    tau_eff: float
    z_score: float


@dataclass
class EdgeResult:
    """Output of edge_engine for one side."""
    side: str                    # "yes" or "no"
    raw_edge: float
    after_fee_edge: float
    tradable: bool
    reject_reason: Optional[str] = None


@dataclass
class SignalDecision:
    """Top-level decision from signal_engine."""
    ts: float
    window_ts: float
    lane: str                    # "selective_taker" | "maker_shadow"
    action: str                  # "PAPER_TRADE" | "NO_TRADE" | "SHADOW_QUOTE" | "NO_QUOTE"
    chosen_side: Optional[str]   # "yes" | "no" | None
    reason: str
    # enrichment fields stored for logging
    seconds_to_expiry: float = 0.0
    elapsed_from_window_start: float = 0.0   # seconds since window open
    btc_mid: float = 0.0
    window_open: float = 0.0
    delta_pct: float = 0.0
    realized_vol_60s: float = 0.0
    fair_yes_prob: float = 0.0
    implied_yes_prob: float = 0.0
    raw_edge_yes: float = 0.0
    raw_edge_no: float = 0.0
    after_fee_edge_yes: float = 0.0
    after_fee_edge_no: float = 0.0
    fee_per_share: float = 0.0
    effective_rate: float = 0.0
    confidence_score: float = 0.0
    bankroll: float = 0.0
    data_age_ms: float = 0.0
    regime: str = "UNKNOWN"
    pattern: str = "UNKNOWN"


@dataclass
class PaperTrade:
    """A simulated taker trade (paper only)."""
    trade_id: str
    ts_open: float
    side: str                    # "yes" | "no"
    entry_price: float
    shares: float
    notional: float
    window_ts: float
    resolved: bool = False
    ts_resolve: Optional[float] = None
    resolve_price: Optional[float] = None  # 1.0 or 0.0
    pnl: float = 0.0
    fair_yes_at_entry: float = 0.0
    after_fee_edge_at_entry: float = 0.0


@dataclass
class ShadowQuote:
    """A theoretical maker quote (no real order)."""
    ts: float
    window_ts: float
    seconds_to_expiry: float
    side: str                    # "yes" | "no"
    quote_price: float
    best_bid: float
    best_ask: float
    tick_size: float
    crossed: bool                # would this quote cross the book?
    fill_would_happen: bool      # was best bid/ask at or better than quote? (deprecated; use fill_status)
    # Forward simulation result states:
    #   pending           — quote is alive, waiting for fill or expiry
    #   filled            — best_ask touched quote_price within TTL
    #   expired           — TTL elapsed without a fill
    #   crossed           — quote crossed the book at placement (would be taker)
    #   adverse_fill      — filled and adverse move measured
    fill_status: str = "pending"
    fill_ts: Optional[float] = None              # unix ts when fill condition was met
    adverse_move_after_fill: Optional[float] = None   # price move post-fill (negative = adverse for buyer)


@dataclass
class BankrollState:
    """Tracks bankroll over time."""
    bankroll: float
    peak_bankroll: float
    total_pnl: float
    drawdown: float              # current drawdown fraction from peak
    last_updated: float = field(default_factory=time.time)

    def update(self, pnl_delta: float) -> None:
        self.bankroll += pnl_delta
        self.total_pnl += pnl_delta
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll
        if self.peak_bankroll > 0:
            self.drawdown = max(0.0, (self.peak_bankroll - self.bankroll) / self.peak_bankroll)
        self.last_updated = time.time()


@dataclass
class MetricsSnapshot:
    """Aggregated session metrics."""
    total_signals: int = 0
    taker_trade_count: int = 0
    taker_no_trade_count: int = 0
    no_trade_reasons: dict = field(default_factory=dict)
    # Shadow quote state counters
    shadow_quote_count: int = 0
    shadow_pending_count: int = 0
    shadow_filled_count: int = 0
    shadow_expired_count: int = 0
    shadow_adverse_fill_count: int = 0
    shadow_crossed_count: int = 0
    shadow_fillable_count: int = 0   # legacy: kept for compatibility
    # Reject counters
    stale_reject_count: int = 0
    spread_reject_count: int = 0
    skew_reject_count: int = 0
    # PnL / bankroll
    paper_pnl: float = 0.0
    bankroll_path: list = field(default_factory=list)
    edge_distribution: list = field(default_factory=list)
    confidence_distribution: list = field(default_factory=list)
    fair_prob_distribution: list = field(default_factory=list)
    taker_win_count: int = 0
    taker_loss_count: int = 0
