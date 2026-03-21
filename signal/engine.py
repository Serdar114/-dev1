"""
signal/engine.py — Deterministic, auditable signal engine for BTC 5m windows.

Design principles
-----------------
1. Every input is explicit and logged — no hidden state.
2. Every feature flag that filters a signal is recorded in the output.
3. The engine is stateless per call; the caller passes in all data.

Signal features (prioritised for maker thesis, per spec §10):
    1.  endcycle_timing_quality   — Was the signal produced early enough to post?
    2.  basis_mismatch            — Fast vs. Chainlink price divergence (bps).
    3.  spread_quality            — YES bid-ask spread at quote time (bps).
    4.  extreme_zone_eligible     — Price outside extreme zones (too close to 0/1)?
    5.  momentum_persistence      — Candle-count confirmation of direction.
    6.  open_price_integrity      — Chainlink price consistent with open?
    7.  feed_freshness            — Both feeds within freshness threshold?

Direction logic
---------------
The engine computes a raw direction (UP or DOWN) from momentum and open-price
comparison.  If signal quality gates pass, the direction is emitted.  If any
hard gate fails, direction = NONE.

All gates and their pass/fail status are recorded in WindowSignal.gates.
This makes the signal engine fully auditable: every decision can be replayed
from logged data.

Basis mismatch
--------------
    basis_bps = (fast_price - chainlink_price) / chainlink_price * 10_000

A large positive basis means the fast feed is running ahead of Chainlink.
A large negative basis means the fast feed is lagging.
Both are flagged independently of direction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class SignalDirection(str, Enum):
    YES = "YES"   # Prediction: price goes UP (YES resolves 1)
    NO = "NO"     # Prediction: price goes DOWN (NO resolves 1)
    NONE = "NONE" # No tradeable signal


@dataclass
class FeedWindow:
    """
    All price / feed observations for a single 5m window, passed to the engine.

    Fields
    ------
    window_open_ts          Unix timestamp of window open.
    open_fast_price         Fast feed price at / near window open.
    latest_fast_price       Most recent fast feed price (pre-signal).
    open_chainlink_price    Chainlink price at / near window open.
    latest_chainlink_price  Most recent Chainlink price (pre-signal).
    fast_gap_seconds        Seconds since last fast feed tick.
    chainlink_gap_seconds   Seconds since last Chainlink tick.
    yes_bid                 Current YES bid in CLOB (None if unavailable).
    yes_ask                 Current YES ask in CLOB (None if unavailable).
    seconds_to_window_close Seconds remaining in the window.
    candles_same_direction  Number of 1m candles confirming the direction.
    fast_feed_stale         True if fast feed gap > stale threshold.
    chainlink_feed_stale    True if Chainlink gap > stale threshold.
    slug                    Market slug for this window.
    """
    window_open_ts: int
    open_fast_price: Optional[float]
    latest_fast_price: Optional[float]
    open_chainlink_price: Optional[float]
    latest_chainlink_price: Optional[float]
    fast_gap_seconds: Optional[float]
    chainlink_gap_seconds: Optional[float]
    yes_bid: Optional[float]
    yes_ask: Optional[float]
    seconds_to_window_close: float
    candles_same_direction: int
    fast_feed_stale: bool
    chainlink_feed_stale: bool
    slug: str


@dataclass
class WindowSignal:
    """
    Fully auditable output of the signal engine for one window.

    Every intermediate feature value and every gate decision is recorded
    so that the signal can be replayed from logs alone.
    """
    # Input identifiers
    window_open_ts: int
    slug: str

    # Direction output
    direction: SignalDirection = SignalDirection.NONE
    intended_price: Optional[float] = None    # Suggested quote / open price

    # --- Feature values (raw measurements) ---
    basis_bps: Optional[float] = None         # Signed basis (fast - chainlink) in bps
    basis_mismatch: bool = False              # True if |basis_bps| > threshold
    spread_bps: Optional[float] = None        # YES ask - YES bid in bps
    endcycle_seconds_remaining: float = 0.0
    candles_same_direction: int = 0
    fast_gap_seconds: Optional[float] = None
    chainlink_gap_seconds: Optional[float] = None

    # --- Gate pass/fail record ---
    gates: dict = field(default_factory=dict)
    # gate keys: feed_freshness | endcycle_timing | extreme_zone |
    #            spread_quality | momentum_persistence | open_price_integrity

    # --- Derived flags ---
    quote_eligible: bool = False   # True iff ALL gates pass
    basis_mismatch_bps: Optional[float] = None  # Absolute value for reporting

    def gate_summary(self) -> str:
        return " ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in self.gates.items())


class SignalEngine:
    """
    Deterministic signal engine.

    All thresholds come from config — no magic numbers in logic.
    """

    def __init__(self, config: dict) -> None:
        sig = config.get("signal", {})
        self._endcycle_cutoff = sig.get("endcycle_entry_cutoff_seconds", 45)
        self._freshness_thresh = sig.get("feed_freshness_threshold_seconds", 8.0)
        self._basis_flag_thresh = sig.get("basis_mismatch_flag_threshold_bps", 30.0)
        self._min_spread_bps = sig.get("min_spread_quality_bps", 5.0)
        self._extreme_low = sig.get("extreme_zone_low", 0.10)
        self._extreme_high = sig.get("extreme_zone_high", 0.90)
        self._momentum_candles = sig.get("momentum_persistence_candles", 2)

    def evaluate(self, fw: FeedWindow) -> WindowSignal:
        """
        Evaluate all signal features and gates for one window.

        Returns a fully populated WindowSignal.  The caller must log it.
        """
        sig = WindowSignal(
            window_open_ts=fw.window_open_ts,
            slug=fw.slug,
            endcycle_seconds_remaining=fw.seconds_to_window_close,
            candles_same_direction=fw.candles_same_direction,
            fast_gap_seconds=fw.fast_gap_seconds,
            chainlink_gap_seconds=fw.chainlink_gap_seconds,
        )

        # ----------------------------------------------------------------
        # Feature 1 — Feed Freshness
        # ----------------------------------------------------------------
        fast_fresh = (
            fw.fast_gap_seconds is not None
            and fw.fast_gap_seconds <= self._freshness_thresh
            and not fw.fast_feed_stale
        )
        chainlink_fresh = (
            fw.chainlink_gap_seconds is not None
            and fw.chainlink_gap_seconds <= self._freshness_thresh
            and not fw.chainlink_feed_stale
        )
        sig.gates["feed_freshness"] = fast_fresh and chainlink_fresh

        # ----------------------------------------------------------------
        # Feature 2 — Basis Mismatch  (fast vs. Chainlink)
        # ----------------------------------------------------------------
        if fw.latest_fast_price is not None and fw.latest_chainlink_price is not None:
            basis_bps = (
                (fw.latest_fast_price - fw.latest_chainlink_price)
                / fw.latest_chainlink_price
                * 10_000.0
            )
            sig.basis_bps = basis_bps
            sig.basis_mismatch_bps = abs(basis_bps)
            sig.basis_mismatch = abs(basis_bps) > self._basis_flag_thresh
        else:
            sig.basis_mismatch = True    # Treat unknown basis as mismatch

        # ----------------------------------------------------------------
        # Feature 3 — Endcycle Timing
        # ----------------------------------------------------------------
        sig.gates["endcycle_timing"] = fw.seconds_to_window_close >= self._endcycle_cutoff

        # ----------------------------------------------------------------
        # Feature 4 — Extreme Zone / Quote-Quality Eligibility
        # ----------------------------------------------------------------
        # We need at least one reference price to gate on extremes.
        ref = fw.latest_chainlink_price or fw.latest_fast_price
        if ref is not None:
            sig.gates["extreme_zone"] = self._extreme_low < ref < self._extreme_high
        else:
            sig.gates["extreme_zone"] = False

        # ----------------------------------------------------------------
        # Feature 5 — Spread Quality
        # ----------------------------------------------------------------
        if fw.yes_bid is not None and fw.yes_ask is not None and fw.yes_bid > 0:
            spread_bps = (fw.yes_ask - fw.yes_bid) / fw.yes_bid * 10_000.0
            sig.spread_bps = spread_bps
            sig.gates["spread_quality"] = spread_bps >= self._min_spread_bps
        else:
            sig.gates["spread_quality"] = False   # Unknown spread → fail gate

        # ----------------------------------------------------------------
        # Feature 6 — Momentum Persistence
        # ----------------------------------------------------------------
        sig.gates["momentum_persistence"] = fw.candles_same_direction >= self._momentum_candles

        # ----------------------------------------------------------------
        # Feature 7 — Open Price Integrity
        # ----------------------------------------------------------------
        # Chainlink open and fast open should be within a loose tolerance.
        if fw.open_fast_price is not None and fw.open_chainlink_price is not None:
            open_diff_bps = abs(
                (fw.open_fast_price - fw.open_chainlink_price)
                / fw.open_chainlink_price
                * 10_000.0
            )
            sig.gates["open_price_integrity"] = open_diff_bps <= (self._basis_flag_thresh * 2)
        else:
            sig.gates["open_price_integrity"] = fw.open_chainlink_price is not None

        # ----------------------------------------------------------------
        # Direction computation (raw)
        # ----------------------------------------------------------------
        raw_direction = self._compute_direction(fw)

        # ----------------------------------------------------------------
        # Gate aggregation
        # ----------------------------------------------------------------
        all_pass = all(sig.gates.values())
        sig.quote_eligible = all_pass and (raw_direction != SignalDirection.NONE)

        if sig.quote_eligible:
            sig.direction = raw_direction
            # Suggest intended price: use current fast feed price as proxy for
            # where we'd post a limit (callers may apply offsets).
            sig.intended_price = fw.latest_fast_price
        else:
            sig.direction = SignalDirection.NONE
            sig.intended_price = None

        logger.info(
            "[signal] window=%d slug=%s direction=%s eligible=%s gates=[%s] "
            "basis_bps=%.1f basis_flag=%s gap_fast=%.1fs gap_cl=%.1fs",
            fw.window_open_ts,
            fw.slug,
            sig.direction.value,
            sig.quote_eligible,
            sig.gate_summary(),
            sig.basis_bps or 0.0,
            sig.basis_mismatch,
            fw.fast_gap_seconds or 0.0,
            fw.chainlink_gap_seconds or 0.0,
        )
        return sig

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _compute_direction(self, fw: FeedWindow) -> SignalDirection:
        """
        Compute raw directional prediction from open vs. current price.

        Simple momentum: if current fast price > open chainlink price → UP.
        Candle confirmation is enforced via the momentum_persistence gate,
        not here.  Keep direction logic minimal and auditable.
        """
        fast = fw.latest_fast_price
        open_cl = fw.open_chainlink_price

        if fast is None or open_cl is None:
            return SignalDirection.NONE

        if fast > open_cl:
            return SignalDirection.YES
        elif fast < open_cl:
            return SignalDirection.NO
        else:
            return SignalDirection.NONE
