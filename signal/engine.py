"""
signal/engine.py — Deterministic, auditable signal engine for BTC 5m windows.

Design principles
-----------------
1. Every input is explicit and logged — no hidden state.
2. Every feature flag that filters a signal is recorded in the output.
3. The engine is stateless per call; the caller passes in all data.
4. Unavailable data is marked explicitly; no silent fallback to NONE.

Signal features (prioritised for maker thesis, per spec §10):
    1.  endcycle_timing_quality   — Was the signal produced early enough to post?
    2.  basis_mismatch            — Fast vs. Chainlink price divergence (bps).
    3.  spread_quality            — YES bid-ask spread at quote time (bps).
    4.  extreme_zone_eligible     — YES probability outside extreme zones?
    5.  momentum_persistence      — Candle-count confirmation of direction.
    6.  open_price_integrity      — Chainlink price consistent with open?
    7.  feed_freshness            — Both feeds within freshness threshold?

CRITICAL: Extreme zone uses Polymarket YES probability (0–1 range)
-----------------------------------------------------------------
`current_yes_mid` in FeedWindow is the CLOB YES-side mid price, a
probability in [0, 1].  It is NOT the BTC/USD spot price.  The thresholds
0.10 and 0.90 refer to market probability (10% / 90%), not dollar values.
The BTC/USD spot price (e.g. 94000.0) must NEVER be compared to 0.10/0.90.

Data availability degradation
------------------------------
When CLOB order book data (yes_bid, yes_ask) is unavailable, the
spread_quality gate fails with rejection_reason "spread_quality:clob_book_unavailable"
rather than silently emitting NONE.  Same for momentum (candle data) and
extreme zone (yes_mid unavailable).  Every NONE output has a documented reason.

Basis mismatch
--------------
    basis_bps = (fast_price - chainlink_price) / chainlink_price * 10_000

NOTE: fast_price and chainlink_price here are the RTDS BTC/USD prices used
only for basis computation between the two feeds.  They are NOT used for
the extreme_zone gate.
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

    IMPORTANT DISTINCTION — two separate price spaces:
    ===================================================
    BTC/USD prices (from RTDS feeds):
        open_fast_price, latest_fast_price          — fast feed BTC/USD
        open_chainlink_price, latest_chainlink_price — chainlink BTC/USD

    Polymarket YES probability prices (from CLOB order book):
        current_yes_mid   — mid-price of YES contract, in [0, 1]
        yes_bid, yes_ask  — CLOB bid/ask for YES contract, in [0, 1]

    The extreme_zone gate operates ONLY on current_yes_mid (probability).
    The basis_mismatch computation operates on BTC/USD feed prices.
    NEVER mix these two price spaces.

    Data availability flags:
        yes_book_available   — True if yes_bid/yes_ask were populated from CLOB
        candles_available    — True if candles_same_direction came from a live source
    """
    window_open_ts: int
    slug: str

    # BTC/USD RTDS prices (for signal direction + basis mismatch only)
    open_fast_price: Optional[float]
    latest_fast_price: Optional[float]
    open_chainlink_price: Optional[float]
    latest_chainlink_price: Optional[float]

    # Polymarket YES probability prices (for extreme zone + spread quality)
    current_yes_mid: Optional[float]    # CLOB YES mid probability  — None if unavailable
    yes_bid: Optional[float]            # CLOB YES bid probability
    yes_ask: Optional[float]            # CLOB YES ask probability

    # Feed gaps
    fast_gap_seconds: Optional[float]
    chainlink_gap_seconds: Optional[float]
    fast_feed_stale: bool
    chainlink_feed_stale: bool

    # Window timing
    seconds_to_window_close: float

    # Candle data
    candles_same_direction: int         # 0 if unavailable

    # Explicit data availability flags  (caller sets these honestly)
    yes_book_available: bool = False    # True only when CLOB book is actually polled
    candles_available: bool = False     # True only when 1m candle tracker is live


@dataclass
class WindowSignal:
    """
    Fully auditable output of the signal engine for one window.

    Every intermediate feature value and every gate decision is recorded
    so that the signal can be replayed from logs alone.

    rejection_reasons is a structured list — every NONE has a documented cause.
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
    spread_bps: Optional[float] = None        # YES ask - YES bid in bps (probability space)
    current_yes_mid: Optional[float] = None   # Polymarket YES probability mid
    endcycle_seconds_remaining: float = 0.0
    candles_same_direction: int = 0
    fast_gap_seconds: Optional[float] = None
    chainlink_gap_seconds: Optional[float] = None

    # --- Gate pass/fail record ---
    gates: dict = field(default_factory=dict)
    # gate keys: feed_freshness | endcycle_timing | extreme_zone |
    #            spread_quality | momentum_persistence | open_price_integrity

    # --- Structured rejection reasons (one entry per failed gate or missing data) ---
    rejection_reasons: list = field(default_factory=list)

    # --- Derived flags ---
    quote_eligible: bool = False   # True iff ALL gates pass
    basis_mismatch_bps: Optional[float] = None  # Absolute value for reporting

    def gate_summary(self) -> str:
        return " ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in self.gates.items())

    def rejection_summary(self) -> str:
        return "; ".join(self.rejection_reasons) if self.rejection_reasons else "none"


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
        self._extreme_low = sig.get("extreme_zone_low", 0.10)   # YES probability
        self._extreme_high = sig.get("extreme_zone_high", 0.90) # YES probability
        self._momentum_candles = sig.get("momentum_persistence_candles", 2)

    def evaluate(self, fw: FeedWindow) -> WindowSignal:
        """
        Evaluate all signal features and gates for one window.

        Returns a fully populated WindowSignal.  The caller must log it.
        Every failed gate appends to rejection_reasons for full auditability.
        """
        sig = WindowSignal(
            window_open_ts=fw.window_open_ts,
            slug=fw.slug,
            endcycle_seconds_remaining=fw.seconds_to_window_close,
            candles_same_direction=fw.candles_same_direction,
            fast_gap_seconds=fw.fast_gap_seconds,
            chainlink_gap_seconds=fw.chainlink_gap_seconds,
            current_yes_mid=fw.current_yes_mid,
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
        if not fast_fresh:
            reason = (
                "feed_freshness:fast_feed_stale"
                if fw.fast_feed_stale
                else f"feed_freshness:fast_gap_{fw.fast_gap_seconds}s_exceeds_{self._freshness_thresh}s"
                if fw.fast_gap_seconds is not None
                else "feed_freshness:fast_gap_unknown"
            )
            sig.rejection_reasons.append(reason)
        if not chainlink_fresh:
            reason = (
                "feed_freshness:chainlink_feed_stale"
                if fw.chainlink_feed_stale
                else f"feed_freshness:chainlink_gap_{fw.chainlink_gap_seconds}s_exceeds_{self._freshness_thresh}s"
                if fw.chainlink_gap_seconds is not None
                else "feed_freshness:chainlink_gap_unknown"
            )
            sig.rejection_reasons.append(reason)

        # ----------------------------------------------------------------
        # Feature 2 — Basis Mismatch  (BTC/USD fast vs. Chainlink)
        # NOTE: these are BTC/USD prices — used only for feed health, not
        #       for extreme zone comparison.
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
            sig.rejection_reasons.append("basis:feed_price_unavailable")

        # ----------------------------------------------------------------
        # Feature 3 — Endcycle Timing
        # ----------------------------------------------------------------
        sig.gates["endcycle_timing"] = fw.seconds_to_window_close >= self._endcycle_cutoff
        if not sig.gates["endcycle_timing"]:
            sig.rejection_reasons.append(
                f"endcycle_timing:{fw.seconds_to_window_close:.1f}s_remaining_"
                f"below_{self._endcycle_cutoff}s_cutoff"
            )

        # ----------------------------------------------------------------
        # Feature 4 — Extreme Zone / Quote-Quality Eligibility
        # CRITICAL: must use current_yes_mid (Polymarket YES probability, 0-1),
        # NOT the BTC/USD spot price from RTDS feeds.
        # ----------------------------------------------------------------
        if fw.current_yes_mid is None:
            sig.gates["extreme_zone"] = False
            sig.rejection_reasons.append(
                "extreme_zone:yes_mid_unavailable"
                " (TODO: connect CLOB book feed to populate current_yes_mid)"
            )
        else:
            in_zone = self._extreme_low < fw.current_yes_mid < self._extreme_high
            sig.gates["extreme_zone"] = in_zone
            if not in_zone:
                sig.rejection_reasons.append(
                    f"extreme_zone:yes_mid={fw.current_yes_mid:.4f}_outside_"
                    f"({self._extreme_low},{self._extreme_high})"
                )

        # ----------------------------------------------------------------
        # Feature 5 — Spread Quality (YES probability space, 0-1)
        # Degrades gracefully when CLOB book is not connected.
        # ----------------------------------------------------------------
        if not fw.yes_book_available:
            # CLOB order book not yet connected — explicit unavailability.
            sig.gates["spread_quality"] = False
            sig.rejection_reasons.append(
                "spread_quality:clob_book_unavailable"
                " (TODO: connect CLOB order book to populate yes_bid/yes_ask)"
            )
        elif fw.yes_bid is not None and fw.yes_ask is not None and fw.yes_bid > 0:
            spread_bps = (fw.yes_ask - fw.yes_bid) / fw.yes_bid * 10_000.0
            sig.spread_bps = spread_bps
            sig.gates["spread_quality"] = spread_bps >= self._min_spread_bps
            if not sig.gates["spread_quality"]:
                sig.rejection_reasons.append(
                    f"spread_quality:spread={spread_bps:.1f}bps_below_{self._min_spread_bps}bps"
                )
        else:
            sig.gates["spread_quality"] = False
            sig.rejection_reasons.append("spread_quality:yes_bid_or_ask_missing")

        # ----------------------------------------------------------------
        # Feature 6 — Momentum Persistence
        # Degrades gracefully when candle tracker is not connected.
        # ----------------------------------------------------------------
        if not fw.candles_available:
            sig.gates["momentum_persistence"] = False
            sig.rejection_reasons.append(
                "momentum_persistence:candle_data_unavailable"
                " (TODO: connect 1m candle tracker to populate candles_same_direction)"
            )
        else:
            sig.gates["momentum_persistence"] = (
                fw.candles_same_direction >= self._momentum_candles
            )
            if not sig.gates["momentum_persistence"]:
                sig.rejection_reasons.append(
                    f"momentum_persistence:{fw.candles_same_direction}_candles_"
                    f"below_{self._momentum_candles}_required"
                )

        # ----------------------------------------------------------------
        # Feature 7 — Open Price Integrity
        # BTC/USD open prices from both feeds should agree within tolerance.
        # ----------------------------------------------------------------
        if fw.open_fast_price is not None and fw.open_chainlink_price is not None:
            open_diff_bps = abs(
                (fw.open_fast_price - fw.open_chainlink_price)
                / fw.open_chainlink_price
                * 10_000.0
            )
            tolerance = self._basis_flag_thresh * 2
            sig.gates["open_price_integrity"] = open_diff_bps <= tolerance
            if not sig.gates["open_price_integrity"]:
                sig.rejection_reasons.append(
                    f"open_price_integrity:open_diff={open_diff_bps:.1f}bps_"
                    f"exceeds_{tolerance}bps"
                )
        else:
            sig.gates["open_price_integrity"] = fw.open_chainlink_price is not None
            if not sig.gates["open_price_integrity"]:
                sig.rejection_reasons.append(
                    "open_price_integrity:open_chainlink_price_unavailable"
                )

        # ----------------------------------------------------------------
        # Direction computation (raw)
        # ----------------------------------------------------------------
        raw_direction = self._compute_direction(fw)
        if raw_direction == SignalDirection.NONE:
            sig.rejection_reasons.append("direction:no_price_divergence_or_missing_prices")

        # ----------------------------------------------------------------
        # Gate aggregation
        # ----------------------------------------------------------------
        all_pass = all(sig.gates.values())
        sig.quote_eligible = all_pass and (raw_direction != SignalDirection.NONE)

        if sig.quote_eligible:
            sig.direction = raw_direction
            # Suggested price: current YES mid probability (if available) or fast feed proxy.
            # Callers should substitute with actual CLOB best-ask/bid when available.
            sig.intended_price = fw.current_yes_mid if fw.current_yes_mid is not None else None
        else:
            sig.direction = SignalDirection.NONE
            sig.intended_price = None

        logger.info(
            "[signal] window=%d slug=%s direction=%s eligible=%s gates=[%s] "
            "basis_bps=%.1f basis_flag=%s gap_fast=%.1fs gap_cl=%.1fs "
            "yes_mid=%s rejections=[%s]",
            fw.window_open_ts,
            fw.slug,
            sig.direction.value,
            sig.quote_eligible,
            sig.gate_summary(),
            sig.basis_bps or 0.0,
            sig.basis_mismatch,
            fw.fast_gap_seconds or 0.0,
            fw.chainlink_gap_seconds or 0.0,
            f"{fw.current_yes_mid:.4f}" if fw.current_yes_mid is not None else "N/A",
            sig.rejection_summary(),
        )
        return sig

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _compute_direction(self, fw: FeedWindow) -> SignalDirection:
        """
        Compute raw directional prediction from BTC/USD open vs. current price.

        Uses fast feed latest vs. Chainlink open as the comparison.
        Candle confirmation is enforced via the momentum_persistence gate.
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
