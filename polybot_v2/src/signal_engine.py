"""
Signal engine for polybot_v2.

Orchestrates both lanes:
  1. selective_taker: produces PAPER_TRADE or NO_TRADE
  2. maker_shadow:    produces SHADOW_QUOTE or NO_QUOTE

Regime and pattern classification done inline (no separate module):
  Regimes: QUIET | TRENDING | CHOP | EXTREME_ZONE
  Patterns: SUSTAINED_MOVE | BURST | FADE | NOISE
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from edge_engine import EdgeEngine
from fair_prob_engine import FairProbEngine
from models import (
    BankrollState,
    MarketSnapshot,
    PriceSnapshot,
    SignalDecision,
)
from risk_manager import RiskDecision, RiskManager, RiskState
from settings import Settings
from stake_policy import StakePolicy

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Regime / Pattern heuristics (inline helpers)
# ------------------------------------------------------------------ #

def _classify_regime(
    delta_pct: float,
    fair_yes: float,
    extreme_upper: float,
    extreme_lower: float,
    sigma_eff: float,
    sigma_floor: float,
    trending_abs_delta: float,
    quiet_abs_delta: float,
    high_vol_sigma_multiple: float,
) -> str:
    if fair_yes >= extreme_upper or fair_yes <= extreme_lower:
        return "EXTREME_ZONE"
    abs_delta = abs(delta_pct)
    high_vol = sigma_eff > sigma_floor * high_vol_sigma_multiple
    if abs_delta > trending_abs_delta and high_vol:
        return "TRENDING"
    if abs_delta < quiet_abs_delta and not high_vol:
        return "QUIET"
    return "CHOP"


def _compute_confidence(
    fair_yes_prob: float,
    implied_yes_prob: float,
    delta_pct: float,
    regime: str,
    pattern: str,
    min_edge: float,
    min_abs_delta: float = 0.0005,
) -> tuple[float, str]:
    """
    Composite confidence score [0, 1] with debug component string.

    Changes from v1:
    - base is capped at 2.0 so huge model-market divergence can't dominate alone
    - delta is a MULTIPLIER [0.15, 1.0], not just a boost — tiny delta forces low confidence
      regardless of how much the model disagrees with market price
    - QUIET+NOISE+tiny-delta → low confidence guaranteed

    Returns:
        (score: float, components: str)  — components for logging/debug
    """
    implied_divergence = abs(fair_yes_prob - implied_yes_prob)
    # Cap base at 2.0 to prevent single huge divergence from overwhelming everything
    base = min(implied_divergence / max(min_edge, 0.01), 2.0)

    regime_mult = {
        "TRENDING": 1.5,
        "CHOP": 1.0,
        "QUIET": 0.4,
        "EXTREME_ZONE": 0.7,
    }.get(regime, 1.0)

    pattern_mult = {
        "SUSTAINED_MOVE": 1.5,
        "BURST": 1.2,
        "NOISE": 0.5,
        "FADE": 0.4,
    }.get(pattern, 1.0)

    # Delta quality: small BTC move → low confidence (model may diverge for noise reasons)
    # At 0 delta → 0.15 floor; at 3× min_abs_delta → 1.0
    abs_d = abs(delta_pct)
    ref_delta = max(min_abs_delta * 3.0, 0.0015)
    delta_quality = min(abs_d / ref_delta, 1.0)
    delta_mult = 0.15 + 0.85 * delta_quality   # range [0.15, 1.0]

    raw = base * regime_mult * pattern_mult * delta_mult
    score = min(raw, 1.0)

    components = (
        f"div={implied_divergence:.4f};base={base:.3f};"
        f"reg={regime_mult:.2f};pat={pattern_mult:.2f};"
        f"dq={delta_quality:.3f};dm={delta_mult:.3f};raw={raw:.4f}"
    )
    return score, components


def _classify_pattern(
    delta_pct: float,
    realized_vol_60s: float,
    sigma_floor: float,
    sustained_move_abs_delta: float,
    sustained_move_vol_ratio: float,
    burst_abs_delta: float,
    burst_vol_ratio: float,
    fade_abs_delta: float,
    fade_vol_ratio_max: float,
) -> str:
    abs_delta = abs(delta_pct)
    vol_ratio = realized_vol_60s / sigma_floor if sigma_floor > 0 else 1.0
    if abs_delta > sustained_move_abs_delta and vol_ratio > sustained_move_vol_ratio:
        return "SUSTAINED_MOVE"
    if abs_delta > burst_abs_delta and vol_ratio > burst_vol_ratio:
        return "BURST"
    if abs_delta > fade_abs_delta and vol_ratio < fade_vol_ratio_max:
        return "FADE"
    return "NOISE"


# ------------------------------------------------------------------ #
# Signal Engine
# ------------------------------------------------------------------ #

class SignalEngine:
    def __init__(
        self,
        settings: Settings,
        fair_prob_engine: FairProbEngine,
        edge_engine: EdgeEngine,
        stake_policy: StakePolicy,
        risk_manager: RiskManager,
    ) -> None:
        self._cfg = settings
        self._fair = fair_prob_engine
        self._edge = edge_engine
        self._stake = stake_policy
        self._risk = risk_manager
        # Cache last valid fair/regime/pattern for diagnostic NO_TRADE paths
        self._last_fair_result = None
        self._last_regime: str = "UNKNOWN"
        self._last_pattern: str = "UNKNOWN"
        # Cache last decision's analytical payload (edge, confidence, fees) so that
        # subsequent _no_trade() calls can carry real values instead of default 0.0
        self._last_decision_context: Optional[dict] = None

    def evaluate_taker(
        self,
        price_snap: PriceSnapshot,
        market_snap: MarketSnapshot,
        window_open: float,
        bankroll_state: BankrollState,
        risk_state: RiskState,
        binance_age_ms: float,
        polymarket_age_ms: float,
    ) -> SignalDecision:
        """
        Evaluate selective taker lane.
        Returns SignalDecision with action PAPER_TRADE or NO_TRADE.
        """
        ts = time.time()
        window_ts = market_snap.window_end_ts
        ste = market_snap.seconds_to_expiry

        # Guard: entry window
        elapsed = self._cfg.window_sec - ste
        if elapsed < self._cfg.entry_start_sec or elapsed > self._cfg.entry_end_sec:
            return self._no_trade(
                ts, window_ts, price_snap, market_snap, window_open,
                bankroll_state, binance_age_ms, "outside_entry_window"
            )

        # Guard: implied prob in valid zone
        impl_yes = market_snap.implied_yes_prob
        if impl_yes < self._cfg.taker_min_prob or impl_yes > self._cfg.taker_max_prob:
            return self._no_trade(
                ts, window_ts, price_snap, market_snap, window_open,
                bankroll_state, binance_age_ms, f"implied_prob_out_of_zone({impl_yes:.3f})"
            )

        # Early risk check: stale feeds and cooldown (fail fast before expensive compute)
        early_risk = self._risk.check_taker(
            state=risk_state,
            binance_age_ms=binance_age_ms,
            polymarket_age_ms=polymarket_age_ms,
            proposed_notional=0.0,
        )
        if not early_risk.allow:
            return self._no_trade(
                ts, window_ts, price_snap, market_snap, window_open,
                bankroll_state, binance_age_ms, f"risk_reject:{early_risk.reason}"
            )

        # Compute fair probability
        try:
            fair_result = self._fair.compute(
                btc_mid=price_snap.btc_mid,
                window_open=window_open,
                seconds_to_expiry=ste,
                realized_vol_60s=price_snap.realized_vol_60s,
            )
        except ValueError as exc:
            return self._no_trade(
                ts, window_ts, price_snap, market_snap, window_open,
                bankroll_state, binance_age_ms, f"fair_prob_error({exc})"
            )

        # Classify regime and pattern using config-driven thresholds
        regime = _classify_regime(
            fair_result.delta_pct, fair_result.fair_yes_prob,
            self._cfg.extreme_upper, self._cfg.extreme_lower,
            fair_result.sigma_eff, self._cfg.sigma_floor,
            self._cfg.trending_abs_delta, self._cfg.quiet_abs_delta,
            self._cfg.high_vol_sigma_multiple,
        )
        pattern = _classify_pattern(
            fair_result.delta_pct, price_snap.realized_vol_60s, self._cfg.sigma_floor,
            self._cfg.sustained_move_abs_delta, self._cfg.sustained_move_vol_ratio,
            self._cfg.burst_abs_delta, self._cfg.burst_vol_ratio,
            self._cfg.fade_abs_delta, self._cfg.fade_vol_ratio_max,
        )
        # Update cache so downstream NO_TRADE paths carry diagnostic context
        self._last_fair_result = fair_result
        self._last_regime = regime
        self._last_pattern = pattern

        # Edge computation
        yes_edge, no_edge = self._edge.compute(
            fair_yes_prob=fair_result.fair_yes_prob,
            fair_no_prob=fair_result.fair_no_prob,
            best_ask_yes=market_snap.best_ask_yes,
            best_ask_no=market_snap.best_ask_no,
        )

        best = self._edge.best_side(yes_edge, no_edge)

        # Compute confidence BEFORE the no_edge early-return so NO_TRADE(no_edge) carries
        # a real confidence score instead of the default 0.0
        confidence, conf_components = _compute_confidence(
            fair_yes_prob=fair_result.fair_yes_prob,
            implied_yes_prob=market_snap.implied_yes_prob,
            delta_pct=fair_result.delta_pct,
            regime=regime,
            pattern=pattern,
            min_edge=self._edge._min_edge,
            min_abs_delta=self._cfg.min_abs_delta_for_taker,
        )

        if best is None:
            reject = yes_edge.reject_reason or no_edge.reject_reason or "no_edge"
            return self._make_decision(
                ts=ts, window_ts=window_ts, lane="selective_taker",
                action="NO_TRADE", chosen_side=None, reason=reject,
                price_snap=price_snap, market_snap=market_snap,
                window_open=window_open, fair_result=fair_result,
                yes_edge=yes_edge, no_edge=no_edge,
                bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                regime=regime, pattern=pattern, confidence=confidence,
                confidence_components=conf_components,
            )

        # --- Taker conviction guards ---

        # Guard: minimum directional move
        if abs(fair_result.delta_pct) < self._cfg.min_abs_delta_for_taker:
            return self._make_decision(
                ts=ts, window_ts=window_ts, lane="selective_taker",
                action="NO_TRADE", chosen_side=None,
                reason=(
                    f"delta_too_small(raw={abs(fair_result.delta_pct):.5f}"
                    f"|{abs(fair_result.delta_pct_display):.3f}%"
                    f" < thr={self._cfg.min_abs_delta_for_taker:.5f}"
                    f"|{self._cfg.min_abs_delta_for_taker*100:.4f}%)"
                ),
                price_snap=price_snap, market_snap=market_snap,
                window_open=window_open, fair_result=fair_result,
                yes_edge=yes_edge, no_edge=no_edge,
                bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                regime=regime, pattern=pattern, confidence=confidence,
                confidence_components=conf_components,
            )

        # Guard: no QUIET+NOISE trades
        if not self._cfg.allow_quiet_noise_trades and regime == "QUIET" and pattern == "NOISE":
            return self._make_decision(
                ts=ts, window_ts=window_ts, lane="selective_taker",
                action="NO_TRADE", chosen_side=None, reason="quiet_noise_rejected",
                price_snap=price_snap, market_snap=market_snap,
                window_open=window_open, fair_result=fair_result,
                yes_edge=yes_edge, no_edge=no_edge,
                bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                regime=regime, pattern=pattern, confidence=confidence,
                confidence_components=conf_components,
            )

        # Guard: neutral probability band — require extra evidence near p=0.50
        fair_yes = fair_result.fair_yes_prob
        if self._cfg.neutral_prob_band_low <= fair_yes <= self._cfg.neutral_prob_band_high:
            if abs(fair_result.delta_pct) < self._cfg.neutral_band_min_delta:
                return self._make_decision(
                    ts=ts, window_ts=window_ts, lane="selective_taker",
                    action="NO_TRADE", chosen_side=None,
                    reason=(
                        f"neutral_band:delta_too_small(raw={abs(fair_result.delta_pct):.5f}"
                        f"|{abs(fair_result.delta_pct_display):.3f}%"
                        f" < thr={self._cfg.neutral_band_min_delta:.5f})"
                    ),
                    price_snap=price_snap, market_snap=market_snap,
                    window_open=window_open, fair_result=fair_result,
                    yes_edge=yes_edge, no_edge=no_edge,
                    bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                    regime=regime, pattern=pattern, confidence=confidence,
                    confidence_components=conf_components,
                )
            if confidence < self._cfg.neutral_band_min_confidence:
                return self._make_decision(
                    ts=ts, window_ts=window_ts, lane="selective_taker",
                    action="NO_TRADE", chosen_side=None,
                    reason=f"neutral_band:low_confidence({confidence:.3f})",
                    price_snap=price_snap, market_snap=market_snap,
                    window_open=window_open, fair_result=fair_result,
                    yes_edge=yes_edge, no_edge=no_edge,
                    bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                    regime=regime, pattern=pattern, confidence=confidence,
                    confidence_components=conf_components,
                )

        # Guard: minimum confidence
        if confidence < self._cfg.min_confidence_for_taker:
            return self._make_decision(
                ts=ts, window_ts=window_ts, lane="selective_taker",
                action="NO_TRADE", chosen_side=None,
                reason=f"low_confidence({confidence:.3f})",
                price_snap=price_snap, market_snap=market_snap,
                window_open=window_open, fair_result=fair_result,
                yes_edge=yes_edge, no_edge=no_edge,
                bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                regime=regime, pattern=pattern, confidence=confidence,
                confidence_components=conf_components,
            )

        # Stake sizing
        entry_price = market_snap.best_ask_yes if best.side == "yes" else market_snap.best_ask_no
        stake = self._stake.compute(
            bankroll=bankroll_state.bankroll,
            entry_price=entry_price,
            last_drawdown=bankroll_state.drawdown,
            lane="selective_taker",
        )

        # Risk check
        risk_dec = self._risk.check_taker(
            state=risk_state,
            binance_age_ms=binance_age_ms,
            polymarket_age_ms=polymarket_age_ms,
            proposed_notional=stake.suggested_notional,
        )
        if not risk_dec.allow:
            return self._make_decision(
                ts=ts, window_ts=window_ts, lane="selective_taker",
                action="NO_TRADE", chosen_side=None, reason=f"risk_reject:{risk_dec.reason}",
                price_snap=price_snap, market_snap=market_snap,
                window_open=window_open, fair_result=fair_result,
                yes_edge=yes_edge, no_edge=no_edge,
                bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                regime=regime, pattern=pattern, confidence=confidence,
                confidence_components=conf_components,
            )

        return self._make_decision(
            ts=ts, window_ts=window_ts, lane="selective_taker",
            action="PAPER_TRADE", chosen_side=best.side, reason="edge_and_risk_ok",
            price_snap=price_snap, market_snap=market_snap,
            window_open=window_open, fair_result=fair_result,
            yes_edge=yes_edge, no_edge=no_edge,
            bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
            confidence_components=conf_components,
            regime=regime, pattern=pattern, confidence=confidence,
        )

    def evaluate_shadow(
        self,
        price_snap: PriceSnapshot,
        market_snap: MarketSnapshot,
        window_open: float,
        bankroll_state: BankrollState,
        risk_state: RiskState,
        binance_age_ms: float,
        polymarket_age_ms: float,
    ) -> SignalDecision:
        """
        Evaluate maker shadow probe lane.
        Returns SignalDecision with action SHADOW_QUOTE or NO_QUOTE.
        """
        ts = time.time()
        window_ts = market_snap.window_end_ts
        ste = market_snap.seconds_to_expiry

        elapsed = self._cfg.window_sec - ste
        if elapsed < self._cfg.shadow_start_sec or elapsed > self._cfg.shadow_end_sec:
            return self._make_simple(ts, window_ts, "maker_shadow", "NO_QUOTE", "outside_shadow_window")

        risk_dec = self._risk.check_shadow(
            state=risk_state,
            binance_age_ms=binance_age_ms,
            polymarket_age_ms=polymarket_age_ms,
        )
        if not risk_dec.allow:
            return self._make_simple(ts, window_ts, "maker_shadow", "NO_QUOTE", f"risk_reject:{risk_dec.reason}")

        try:
            fair_result = self._fair.compute(
                btc_mid=price_snap.btc_mid,
                window_open=window_open,
                seconds_to_expiry=ste,
                realized_vol_60s=price_snap.realized_vol_60s,
            )
        except ValueError as exc:
            return self._make_simple(ts, window_ts, "maker_shadow", "NO_QUOTE", f"fair_prob_error({exc})")

        # Shadow: we want to be a maker on the side where we have edge
        yes_edge, no_edge = self._edge.compute(
            fair_yes_prob=fair_result.fair_yes_prob,
            fair_no_prob=fair_result.fair_no_prob,
            best_ask_yes=market_snap.best_ask_yes,
            best_ask_no=market_snap.best_ask_no,
        )

        # For maker shadow, pick the side where fair_prob gives us theoretical edge
        # as a passive liquidity provider (buying below fair value)
        if fair_result.fair_yes_prob > market_snap.implied_yes_prob:
            chosen_side = "yes"
        elif fair_result.fair_no_prob > (1 - market_snap.implied_yes_prob):
            chosen_side = "no"
        else:
            return self._make_simple(ts, window_ts, "maker_shadow", "NO_QUOTE", "no_maker_edge")

        regime = _classify_regime(
            fair_result.delta_pct, fair_result.fair_yes_prob,
            self._cfg.extreme_upper, self._cfg.extreme_lower,
            fair_result.sigma_eff, self._cfg.sigma_floor,
            self._cfg.trending_abs_delta, self._cfg.quiet_abs_delta,
            self._cfg.high_vol_sigma_multiple,
        )
        pattern = _classify_pattern(
            fair_result.delta_pct, price_snap.realized_vol_60s, self._cfg.sigma_floor,
            self._cfg.sustained_move_abs_delta, self._cfg.sustained_move_vol_ratio,
            self._cfg.burst_abs_delta, self._cfg.burst_vol_ratio,
            self._cfg.fade_abs_delta, self._cfg.fade_vol_ratio_max,
        )

        shadow_confidence, shadow_conf_components = _compute_confidence(
            fair_yes_prob=fair_result.fair_yes_prob,
            implied_yes_prob=market_snap.implied_yes_prob,
            delta_pct=fair_result.delta_pct,
            regime=regime,
            pattern=pattern,
            min_edge=self._edge._min_edge,
            min_abs_delta=self._cfg.min_abs_delta_for_taker,
        )
        return self._make_decision(
            ts=ts, window_ts=window_ts, lane="maker_shadow",
            action="SHADOW_QUOTE", chosen_side=chosen_side, reason="shadow_edge_identified",
            price_snap=price_snap, market_snap=market_snap,
            window_open=window_open, fair_result=fair_result,
            yes_edge=yes_edge, no_edge=no_edge,
            bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
            regime=regime, pattern=pattern, confidence=shadow_confidence,
            confidence_components=shadow_conf_components,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _no_trade(
        self, ts, window_ts, price_snap, market_snap, window_open,
        bankroll_state, binance_age_ms, reason,
    ) -> SignalDecision:
        ste = market_snap.seconds_to_expiry
        elapsed = max(0.0, self._cfg.window_sec - ste)
        # Use last-known fair/regime/pattern so NO_TRADE log lines carry full context
        lf = self._last_fair_result
        lc = self._last_decision_context  # edge, confidence, fees from last analytical tick
        # delta can always be computed if window_open is set
        wo = window_open or 0.0
        if wo > 0 and price_snap.btc_mid > 0:
            raw_d = (price_snap.btc_mid - wo) / wo
        else:
            raw_d = lf.delta_pct if lf else 0.0
        return SignalDecision(
            ts=ts, window_ts=window_ts,
            lane="selective_taker", action="NO_TRADE",
            chosen_side=None, reason=reason,
            seconds_to_expiry=ste,
            elapsed_from_window_start=elapsed,
            btc_mid=price_snap.btc_mid,
            window_open=wo,
            delta_pct=raw_d,
            delta_raw_fraction=raw_d,
            delta_pct_display=raw_d * 100.0,
            realized_vol_60s=price_snap.realized_vol_60s,
            fair_yes_prob=lf.fair_yes_prob if lf else 0.0,
            implied_yes_prob=market_snap.implied_yes_prob,
            # Populate analytical payload from last decision context so cache path
            # never emits fake 0.0 defaults when lc is available
            raw_edge_yes=lc["raw_edge_yes"] if lc else 0.0,
            raw_edge_no=lc["raw_edge_no"] if lc else 0.0,
            after_fee_edge_yes=lc["after_fee_edge_yes"] if lc else 0.0,
            after_fee_edge_no=lc["after_fee_edge_no"] if lc else 0.0,
            confidence_score=lc["confidence_score"] if lc else 0.0,
            confidence_components=lc["confidence_components"] if lc else "",
            fee_per_share_yes=lc["fee_per_share_yes"] if lc else 0.0,
            fee_per_share_no=lc["fee_per_share_no"] if lc else 0.0,
            effective_fee_rate_yes=lc["effective_fee_rate_yes"] if lc else 0.0,
            effective_fee_rate_no=lc["effective_fee_rate_no"] if lc else 0.0,
            regime=self._last_regime,
            pattern=self._last_pattern,
            bankroll=bankroll_state.bankroll,
            data_age_ms=max(0.0, binance_age_ms),
            fair_computed=(lf is not None),
            fair_computed_fresh=False,
            context_from_cache=(lf is not None),
        )

    def _make_simple(self, ts, window_ts, lane, action, reason) -> SignalDecision:
        return SignalDecision(
            ts=ts, window_ts=window_ts,
            lane=lane, action=action,
            chosen_side=None, reason=reason,
        )

    def _make_decision(
        self, ts, window_ts, lane, action, chosen_side, reason,
        price_snap, market_snap, window_open, fair_result,
        yes_edge, no_edge, bankroll_state, binance_age_ms,
        regime, pattern, confidence: float = 0.0,
        confidence_components: str = "",
    ) -> SignalDecision:
        ste = market_snap.seconds_to_expiry
        elapsed = max(0.0, self._cfg.window_sec - ste)
        # Side-specific fees at actual market ask prices
        fee_yes = self._edge._fee.taker_estimate(market_snap.best_ask_yes)
        fee_no = self._edge._fee.taker_estimate(market_snap.best_ask_no)
        # Generic fee = fee for the chosen execution side; 0.0 (null in logs) for NO_TRADE
        if chosen_side == "yes":
            fee_generic = fee_yes
        elif chosen_side == "no":
            fee_generic = fee_no
        else:
            fee_generic = None  # NO_TRADE: no execution side
        decision = SignalDecision(
            ts=ts,
            window_ts=window_ts,
            lane=lane,
            action=action,
            chosen_side=chosen_side,
            reason=reason,
            seconds_to_expiry=ste,
            elapsed_from_window_start=elapsed,
            btc_mid=price_snap.btc_mid,
            window_open=window_open,
            delta_pct=fair_result.delta_pct,
            delta_raw_fraction=fair_result.delta_pct,
            delta_pct_display=fair_result.delta_pct_display,
            realized_vol_60s=price_snap.realized_vol_60s,
            fair_yes_prob=fair_result.fair_yes_prob,
            implied_yes_prob=market_snap.implied_yes_prob,
            raw_edge_yes=yes_edge.raw_edge,
            raw_edge_no=no_edge.raw_edge,
            after_fee_edge_yes=yes_edge.after_fee_edge,
            after_fee_edge_no=no_edge.after_fee_edge,
            fee_per_share=round(fee_generic.fee_per_share, 8) if fee_generic else 0.0,
            effective_rate=round(fee_generic.effective_rate, 6) if fee_generic else 0.0,
            fee_per_share_yes=round(fee_yes.fee_per_share, 8),
            fee_per_share_no=round(fee_no.fee_per_share, 8),
            effective_fee_rate_yes=round(fee_yes.effective_rate, 6),
            effective_fee_rate_no=round(fee_no.effective_rate, 6),
            confidence_score=round(confidence, 4),
            bankroll=bankroll_state.bankroll,
            data_age_ms=max(0.0, binance_age_ms),
            regime=regime,
            pattern=pattern,
            fair_computed=True,
            fair_computed_fresh=True,   # engine ran this tick
            context_from_cache=False,
            confidence_components=confidence_components,
        )
        # Persist analytical payload for subsequent _no_trade() calls in the same tick
        self._last_decision_context = {
            "raw_edge_yes": yes_edge.raw_edge,
            "raw_edge_no": no_edge.raw_edge,
            "after_fee_edge_yes": yes_edge.after_fee_edge,
            "after_fee_edge_no": no_edge.after_fee_edge,
            "confidence_score": round(confidence, 4),
            "confidence_components": confidence_components,
            "fee_per_share_yes": round(fee_yes.fee_per_share, 8),
            "fee_per_share_no": round(fee_no.fee_per_share, 8),
            "effective_fee_rate_yes": round(fee_yes.effective_rate, 6),
            "effective_fee_rate_no": round(fee_no.effective_rate, 6),
        }
        return decision
