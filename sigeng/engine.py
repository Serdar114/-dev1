"""
sigeng/engine.py — Deterministic, auditable signal engine for BTC 5m windows.

Package renamed from 'signal' to 'sigeng' to avoid collision with Python
stdlib signal module (which is imported internally by asyncio and other libs).

Design principles
-----------------
1. Every input is explicit and logged — no hidden state.
2. Every feature flag that filters a signal is recorded in the output.
3. The engine is stateless per call; the caller passes in all data.
4. Unavailable data is marked explicitly; no silent fallback to NONE.

Bot modes
---------
STRICT (bot_mode="STRICT"):
    All gates require confirmed data. Missing data = hard gate failure.
    Produces mostly NONE in the current state (no CLOB subscription yet).
    Use for production readiness evaluation.

PROVISIONAL (bot_mode="PROVISIONAL"):
    Gates that fail due to MISSING DATA (not bad values) are soft-passed
    and logged as PROVISIONAL_SOFT_PASS. Gates that fail due to actual
    bad/out-of-range values are still hard failures.

    Soft-passable gates when data is unavailable:
        momentum_persistence  — when candles_available=False
        spread_quality        — when yes_book_available=False

    Hard gates (never soft-passed):
        feed_freshness        — stale feeds = reject always
        endcycle_timing       — too late to post = reject always
        extreme_zone          — no YES mid = reject always
        open_price_integrity  — feed divergence = reject always

CRITICAL: Extreme zone uses Polymarket YES probability (0–1 range)
-----------------------------------------------------------------
current_yes_mid is the CLOB YES-side mid price, a probability in [0, 1].
It is NOT the BTC/USD spot price. The thresholds 0.10 and 0.90 refer to
market probability (10%/90%), not dollar values.
BTC/USD spot price (e.g. 94000.0) must NEVER be compared to 0.10/0.90.

Price spaces in FeedWindow
--------------------------
BTC/USD prices (from RTDS feeds, always >> 1):
    open_fast_price, latest_fast_price       — fast feed BTC/USD
    open_chainlink_price, latest_chainlink   — chainlink BTC/USD

Polymarket YES probability prices (from CLOB, always in (0, 1)):
    current_yes_mid  — CLOB YES mid probability
    yes_bid, yes_ask — CLOB bid/ask for YES
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
    All price / feed observations for a single 5m window.

    PRICE SPACE SEPARATION — two distinct spaces, never mix:
    ========================================================
    BTC/USD RTDS prices (>> 1, e.g. 94000.0):
        open_fast_price, latest_fast_price
        open_chainlink_price, latest_chainlink_price

    Polymarket YES probability prices ((0, 1), e.g. 0.87):
        current_yes_mid  — CLOB YES mid
        yes_bid, yes_ask — CLOB order book quotes

    Data availability flags (caller must set honestly):
        yes_book_available  — True only when CLOB book data is actually present
        candles_available   — True only when 1m candle tracker is live
    """
    window_open_ts: int
    slug: str

    # BTC/USD RTDS prices (for direction + basis mismatch only)
    open_fast_price: Optional[float]
    latest_fast_price: Optional[float]
    open_chainlink_price: Optional[float]
    latest_chainlink_price: Optional[float]

    # Polymarket YES probability prices (for extreme zone + spread quality)
    current_yes_mid: Optional[float]    # YES probability (0, 1) or None
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
    candles_same_direction: int

    # Explicit availability flags (caller sets these honestly)
    yes_book_available: bool = False
    candles_available: bool = False


@dataclass
class WindowSignal:
    """
    Fully auditable output of the signal engine for one window.

    All gate decisions, feature values, and rejection reasons are recorded.
    Soft-pass entries in rejection_reasons are prefixed with PROVISIONAL_SOFT_PASS.
    """
    window_open_ts: int
    slug: str

    direction: SignalDirection = SignalDirection.NONE
    intended_price: Optional[float] = None

    # Feature values
    basis_bps: Optional[float] = None
    basis_mismatch: bool = False
    spread_bps: Optional[float] = None
    current_yes_mid: Optional[float] = None
    endcycle_seconds_remaining: float = 0.0
    candles_same_direction: int = 0
    fast_gap_seconds: Optional[float] = None
    chainlink_gap_seconds: Optional[float] = None

    # Gate pass/fail
    gates: dict = field(default_factory=dict)

    # Structured rejection reasons
    rejection_reasons: list = field(default_factory=list)

    # Derived flags
    quote_eligible: bool = False
    basis_mismatch_bps: Optional[float] = None
    is_provisional: bool = False    # True if any gate was soft-passed

    def gate_summary(self) -> str:
        return " ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in self.gates.items())

    def rejection_summary(self) -> str:
        return "; ".join(self.rejection_reasons) if self.rejection_reasons else "none"


class SignalEngine:
    """
    Deterministic signal engine.

    All thresholds from config. Mode (STRICT/PROVISIONAL) controls
    soft-gate behaviour for unavailable data.
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
        # PROVISIONAL mode: soft-pass gates that fail due to missing data.
        # STRICT mode (default): missing data = hard failure.
        self._provisional = config.get("bot_mode", "STRICT") == "PROVISIONAL"

    def evaluate(self, fw: FeedWindow) -> WindowSignal:
        """
        Evaluate all signal features and gates for one window.

        In PROVISIONAL mode, gates that fail due to missing data are soft-passed
        with PROVISIONAL_SOFT_PASS in rejection_reasons.
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
        # Feature 1 — Feed Freshness (HARD gate: never soft-passed)
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
            if fw.fast_feed_stale:
                sig.rejection_reasons.append("feed_freshness:fast_feed_stale")
            elif fw.fast_gap_seconds is None:
                sig.rejection_reasons.append("feed_freshness:fast_gap_unknown")
            else:
                sig.rejection_reasons.append(
                    f"feed_freshness:fast_gap_{fw.fast_gap_seconds:.1f}s"
                    f"_exceeds_{self._freshness_thresh}s"
                )
        if not chainlink_fresh:
            if fw.chainlink_feed_stale:
                sig.rejection_reasons.append("feed_freshness:chainlink_feed_stale")
            elif fw.chainlink_gap_seconds is None:
                sig.rejection_reasons.append("feed_freshness:chainlink_gap_unknown")
            else:
                sig.rejection_reasons.append(
                    f"feed_freshness:chainlink_gap_{fw.chainlink_gap_seconds:.1f}s"
                    f"_exceeds_{self._freshness_thresh}s"
                )

        # ----------------------------------------------------------------
        # Feature 2 — Basis Mismatch (informational, not a gate)
        # Uses BTC/USD prices from RTDS feeds only.
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
            sig.basis_mismatch = True
            sig.rejection_reasons.append("basis:feed_price_unavailable")

        # ----------------------------------------------------------------
        # Feature 3 — Endcycle Timing (HARD gate)
        # ----------------------------------------------------------------
        sig.gates["endcycle_timing"] = (
            fw.seconds_to_window_close >= self._endcycle_cutoff
        )
        if not sig.gates["endcycle_timing"]:
            sig.rejection_reasons.append(
                f"endcycle_timing:{fw.seconds_to_window_close:.1f}s_remaining"
                f"_below_{self._endcycle_cutoff}s_cutoff"
            )

        # ----------------------------------------------------------------
        # Feature 4 — Extreme Zone (HARD gate: uses YES probability only)
        # current_yes_mid MUST be in (0, 1). Never pass BTC/USD here.
        # ----------------------------------------------------------------
        if fw.current_yes_mid is None:
            sig.gates["extreme_zone"] = False
            sig.rejection_reasons.append(
                "extreme_zone:yes_mid_unavailable"
                " (TODO: connect CLOB book feed)"
            )
        else:
            in_zone = self._extreme_low < fw.current_yes_mid < self._extreme_high
            sig.gates["extreme_zone"] = in_zone
            if not in_zone:
                sig.rejection_reasons.append(
                    f"extreme_zone:yes_mid={fw.current_yes_mid:.4f}"
                    f"_outside_({self._extreme_low},{self._extreme_high})"
                )

        # ----------------------------------------------------------------
        # Feature 5 — Spread Quality
        # SOFT gate in PROVISIONAL mode when book unavailable.
        # HARD gate in STRICT mode.
        # ----------------------------------------------------------------
        if not fw.yes_book_available:
            if self._provisional:
                sig.gates["spread_quality"] = True
                sig.rejection_reasons.append(
                    "spread_quality:PROVISIONAL_SOFT_PASS"
                    ":clob_book_unavailable"
                    " (TODO: connect CLOB book)"
                )
                sig.is_provisional = True
            else:
                sig.gates["spread_quality"] = False
                sig.rejection_reasons.append(
                    "spread_quality:STRICT_FAIL"
                    ":clob_book_unavailable"
                )
        elif fw.yes_bid is not None and fw.yes_ask is not None and fw.yes_bid > 0:
            spread_bps = (fw.yes_ask - fw.yes_bid) / fw.yes_bid * 10_000.0
            sig.spread_bps = spread_bps
            sig.gates["spread_quality"] = spread_bps >= self._min_spread_bps
            if not sig.gates["spread_quality"]:
                sig.rejection_reasons.append(
                    f"spread_quality:spread={spread_bps:.1f}bps"
                    f"_below_{self._min_spread_bps}bps"
                )
        else:
            sig.gates["spread_quality"] = False
            sig.rejection_reasons.append("spread_quality:yes_bid_or_ask_missing")

        # ----------------------------------------------------------------
        # Feature 6 — Momentum Persistence
        # SOFT gate in PROVISIONAL mode when candle data unavailable.
        # HARD gate in STRICT mode.
        # ----------------------------------------------------------------
        if not fw.candles_available:
            if self._provisional:
                sig.gates["momentum_persistence"] = True
                sig.rejection_reasons.append(
                    "momentum_persistence:PROVISIONAL_SOFT_PASS"
                    ":candle_data_unavailable"
                    " (TODO: connect 1m candle tracker)"
                )
                sig.is_provisional = True
            else:
                sig.gates["momentum_persistence"] = False
                sig.rejection_reasons.append(
                    "momentum_persistence:STRICT_FAIL"
                    ":candle_data_unavailable"
                )
        else:
            passes = fw.candles_same_direction >= self._momentum_candles
            sig.gates["momentum_persistence"] = passes
            if not passes:
                sig.rejection_reasons.append(
                    f"momentum_persistence:{fw.candles_same_direction}_candles"
                    f"_below_{self._momentum_candles}_required"
                )

        # ----------------------------------------------------------------
        # Feature 7 — Open Price Integrity (HARD gate)
        # BTC/USD open prices from both feeds must agree.
        # ----------------------------------------------------------------
        if fw.open_fast_price is not None and fw.open_chainlink_price is not None:
            tolerance = self._basis_flag_thresh * 2
            open_diff_bps = abs(
                (fw.open_fast_price - fw.open_chainlink_price)
                / fw.open_chainlink_price
                * 10_000.0
            )
            sig.gates["open_price_integrity"] = open_diff_bps <= tolerance
            if not sig.gates["open_price_integrity"]:
                sig.rejection_reasons.append(
                    f"open_price_integrity:open_diff={open_diff_bps:.1f}bps"
                    f"_exceeds_{tolerance}bps"
                )
        else:
            sig.gates["open_price_integrity"] = fw.open_chainlink_price is not None
            if not sig.gates["open_price_integrity"]:
                sig.rejection_reasons.append(
                    "open_price_integrity:open_chainlink_price_unavailable"
                )

        # ----------------------------------------------------------------
        # Direction
        # ----------------------------------------------------------------
        raw_direction = self._compute_direction(fw)
        if raw_direction == SignalDirection.NONE:
            sig.rejection_reasons.append(
                "direction:no_price_divergence_or_missing_prices"
            )

        # ----------------------------------------------------------------
        # Gate aggregation
        # ----------------------------------------------------------------
        all_pass = all(sig.gates.values())
        sig.quote_eligible = all_pass and (raw_direction != SignalDirection.NONE)

        if sig.quote_eligible:
            sig.direction = raw_direction
            sig.intended_price = (
                fw.current_yes_mid if fw.current_yes_mid is not None else None
            )
        else:
            sig.direction = SignalDirection.NONE
            sig.intended_price = None

        prov_tag = "[PROVISIONAL] " if sig.is_provisional else ""
        logger.info(
            "%s[signal] window=%d slug=%s direction=%s eligible=%s "
            "gates=[%s] basis_bps=%.1f yes_mid=%s rejections=[%s]",
            prov_tag,
            fw.window_open_ts,
            fw.slug,
            sig.direction.value,
            sig.quote_eligible,
            sig.gate_summary(),
            sig.basis_bps or 0.0,
            f"{fw.current_yes_mid:.4f}" if fw.current_yes_mid is not None else "N/A",
            sig.rejection_summary(),
        )
        return sig

    def _compute_direction(self, fw: FeedWindow) -> SignalDirection:
        """
        Compute raw directional prediction from BTC/USD open vs. current.
        Uses fast feed latest vs. Chainlink open.
        """
        fast = fw.latest_fast_price
        open_cl = fw.open_chainlink_price
        if fast is None or open_cl is None:
            return SignalDirection.NONE
        if fast > open_cl:
            return SignalDirection.YES
        elif fast < open_cl:
            return SignalDirection.NO
        return SignalDirection.NONE
