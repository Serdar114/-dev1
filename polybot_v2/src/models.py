"""
Core data models for polybot_v2.
Phase 2: maker-first evaluation data model with full quote lifecycle tracing.
All domain objects live here; nothing imports from each other here.
"""

from __future__ import annotations

import uuid
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
    slug: str = ""               # market slug for display (btc-updown-5m-<ts>)

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
    # delta_pct is the RAW FRACTION: (btc_mid - window_open) / window_open
    # e.g. 0.00100 means +0.10% move. All threshold comparisons use this unit.
    delta_pct: float
    sigma_eff: float
    tau_eff: float
    z_score: float
    # delta_pct_display = delta_pct * 100  — percentage form for UI/logs only
    delta_pct_display: float = 0.0


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
    # delta_pct = raw fraction (same unit as FairProbResult.delta_pct); used for all guard comparisons
    delta_pct: float = 0.0
    # delta_raw_fraction / delta_pct_display = unambiguous dual representation for logs/UI
    delta_raw_fraction: float = 0.0   # (btc_mid - window_open) / window_open
    delta_pct_display: float = 0.0    # delta_raw_fraction * 100, percentage for display
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
    # Diagnostic flags — set False when fields could not be computed this tick
    # (e.g. early guard before fair_prob engine ran)
    fair_computed: bool = False           # True iff analytical fields are available (fresh or cached)
    fair_computed_fresh: bool = False     # True iff fair engine ran THIS tick (not from cache)
    context_from_cache: bool = False      # True iff analytical fields come from previous-tick cache
    # Compact confidence breakdown for debugging: "div=X;base=X;reg=X;pat=X;dq=X;raw=X"
    confidence_components: str = ""
    # Side-specific fee breakdown (fee curve evaluated at actual market ask prices)
    fee_per_share_yes: float = 0.0        # fee at best_ask_yes
    fee_per_share_no: float = 0.0         # fee at best_ask_no
    effective_fee_rate_yes: float = 0.0   # fee_per_share_yes / best_ask_yes
    effective_fee_rate_no: float = 0.0    # fee_per_share_no / best_ask_no


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
    """
    A theoretical maker quote (no real order). Phase 2: full evaluation lifecycle.

    fill_status states:
      pending           - quote is alive, waiting for fill or expiry
      crossed_rejected  - quote crossed the book at placement (post-only rejected)
      expired_unfilled  - TTL elapsed without a fill
      filled            - best_ask touched quote_price within TTL (interim; pending adverse measure)
      filled_adverse    - filled and next-tick mid moved against us
      filled_favorable  - filled and next-tick mid moved in our favor
      boundary_resolved - window ended while quote was still pending or in flight

    Fields that cannot be computed until a later event are None (never default 0.0 to pollute data).
    """
    ts: float
    window_ts: float
    seconds_to_expiry: float
    side: str                    # "yes" | "no"
    quote_price: float
    best_bid: float
    best_ask: float
    tick_size: float
    crossed: bool                # would this quote cross the book? (retained for compat)
    # fill_status is authoritative for lifecycle state
    fill_status: str = "pending"
    fill_ts: Optional[float] = None              # unix ts when fill condition was met
    adverse_move_after_fill: Optional[float] = None   # price move post-fill (negative = adverse for YES buyer)

    # ── Phase 2: quote identity ──────────────────────────────────────────
    quote_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    market_slug: str = ""

    # ── Phase 2: decision context (copied from SignalDecision at quote time) ──
    regime: str = "UNKNOWN"
    pattern: str = "UNKNOWN"
    elapsed_from_window_start: float = 0.0
    implied_yes_prob: float = 0.0
    fair_yes_prob: float = 0.0
    delta_raw_fraction: float = 0.0
    delta_pct_display: float = 0.0
    confidence_score: float = 0.0
    confidence_components: str = ""

    # ── Phase 2: maker economics at quote time ───────────────────────────
    intended_passive_edge: Optional[float] = None   # fair_for_side - quote_price at placement
    intended_notional: float = 1.0                  # reference notional USDC for PnL calc

    # ── Phase 2: fill quality ────────────────────────────────────────────
    fill_mid: Optional[float] = None                # mid at time of fill
    next_mid_after_fill: Optional[float] = None     # mid on next tick after fill
    favorable_move_after_fill: Optional[float] = None   # positive = favorable for our side

    # ── Phase 2: boundary resolution ────────────────────────────────────
    boundary_outcome_yes: Optional[float] = None    # 1.0 if BTC up, 0.0 if BTC down
    boundary_outcome_for_side: Optional[float] = None   # outcome from our side's perspective
    maker_pnl_if_held: Optional[float] = None       # per-share PnL if held to resolution
    maker_edge_realized_vs_expected: Optional[float] = None  # realized - intended_passive_edge

    # ── Phase 2: reject reason (when placement was rejected) ─────────────
    reject_reason: Optional[str] = None             # why quote was rejected at build time


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
    """Aggregated session metrics. Phase 2: maker-first evaluation metrics."""
    total_signals: int = 0
    taker_trade_count: int = 0
    taker_no_trade_count: int = 0
    no_trade_reasons: dict = field(default_factory=dict)
    # Taker shadow/shadow quote legacy counters (kept for compat)
    shadow_quote_count: int = 0
    shadow_pending_count: int = 0
    shadow_filled_count: int = 0
    shadow_expired_count: int = 0
    shadow_adverse_fill_count: int = 0
    shadow_crossed_count: int = 0
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
    # ── Phase 2: Maker first-class metrics ──────────────────────────────
    # maker_quote_count = quotes placed (pending + crossed_rejected)
    maker_quote_count: int = 0
    # maker_no_quote_count = signals where maker lane decided NO_QUOTE
    maker_no_quote_count: int = 0
    # placement lifecycle
    maker_crossed_reject_count: int = 0
    maker_pending_count: int = 0
    # fill lifecycle
    maker_fill_count: int = 0           # total fills (filled_adverse + filled_favorable)
    maker_adverse_fill_count: int = 0
    maker_favorable_fill_count: int = 0
    # expiry
    maker_expired_count: int = 0
    # boundary resolution
    maker_boundary_resolved_count: int = 0
    maker_boundary_win_count: int = 0   # boundary_outcome_for_side == 1.0
    maker_boundary_loss_count: int = 0  # boundary_outcome_for_side == 0.0
    # PnL if held
    maker_pnl_if_held_list: list = field(default_factory=list)
    # Edge tracking (per-quote values for mean computation)
    maker_expected_edge_list: list = field(default_factory=list)  # intended_passive_edge
    maker_realized_edge_list: list = field(default_factory=list)  # maker_edge_realized_vs_expected
    # Breakdown accumulators (side/regime/pattern -> fill counts, boundary wins)
    maker_by_side: dict = field(default_factory=dict)
    maker_by_regime: dict = field(default_factory=dict)
    maker_by_pattern: dict = field(default_factory=dict)
