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
) -> float:
    """
    Composite confidence score [0, 1].

    Combines:
      - implied_divergence: how much our model disagrees with market price
        relative to the minimum edge threshold
      - regime multiplier: TRENDING > CHOP > EXTREME_ZONE > QUIET
      - pattern multiplier: SUSTAINED_MOVE > BURST > NOISE > FADE
      - delta boost: directional conviction from BTC price move

    A score of 0.20+ is required for a PAPER_TRADE in config.
    """
    implied_divergence = abs(fair_yes_prob - implied_yes_prob)
    base = implied_divergence / max(min_edge, 0.01)

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

    # Directional conviction from BTC delta (caps at 2x boost)
    delta_boost = min(abs(delta_pct) / 0.003, 2.0)
    raw = base * regime_mult * pattern_mult * (1.0 + delta_boost * 0.3)
    return min(raw, 1.0)


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

        # Edge computation
        yes_edge, no_edge = self._edge.compute(
            fair_yes_prob=fair_result.fair_yes_prob,
            fair_no_prob=fair_result.fair_no_prob,
            best_ask_yes=market_snap.best_ask_yes,
            best_ask_no=market_snap.best_ask_no,
        )

        best = self._edge.best_side(yes_edge, no_edge)

        if best is None:
            reject = yes_edge.reject_reason or no_edge.reject_reason or "no_edge"
            return self._make_decision(
                ts=ts, window_ts=window_ts, lane="selective_taker",
                action="NO_TRADE", chosen_side=None, reason=reject,
                price_snap=price_snap, market_snap=market_snap,
                window_open=window_open, fair_result=fair_result,
                yes_edge=yes_edge, no_edge=no_edge,
                bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                regime=regime, pattern=pattern, confidence=0.0,
            )

        # Compute confidence score
        confidence = _compute_confidence(
            fair_yes_prob=fair_result.fair_yes_prob,
            implied_yes_prob=market_snap.implied_yes_prob,
            delta_pct=fair_result.delta_pct,
            regime=regime,
            pattern=pattern,
            min_edge=self._edge._min_edge,
        )

        # --- Taker conviction guards ---

        # Guard: minimum directional move
        if abs(fair_result.delta_pct) < self._cfg.min_abs_delta_for_taker:
            return self._make_decision(
                ts=ts, window_ts=window_ts, lane="selective_taker",
                action="NO_TRADE", chosen_side=None,
                reason=f"delta_too_small({abs(fair_result.delta_pct):.5f})",
                price_snap=price_snap, market_snap=market_snap,
                window_open=window_open, fair_result=fair_result,
                yes_edge=yes_edge, no_edge=no_edge,
                bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                regime=regime, pattern=pattern, confidence=confidence,
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
            )

        # Guard: neutral probability band — require extra evidence near p=0.50
        fair_yes = fair_result.fair_yes_prob
        if self._cfg.neutral_prob_band_low <= fair_yes <= self._cfg.neutral_prob_band_high:
            if abs(fair_result.delta_pct) < self._cfg.neutral_band_min_delta:
                return self._make_decision(
                    ts=ts, window_ts=window_ts, lane="selective_taker",
                    action="NO_TRADE", chosen_side=None,
                    reason=f"neutral_band:delta_too_small({abs(fair_result.delta_pct):.5f})",
                    price_snap=price_snap, market_snap=market_snap,
                    window_open=window_open, fair_result=fair_result,
                    yes_edge=yes_edge, no_edge=no_edge,
                    bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                    regime=regime, pattern=pattern, confidence=confidence,
                )
            if confidence < self._cfg.neutral_band_min_confidence:
                return self._make_decision(
                    ts=ts, window_ts=window_ts, lane="selective_taker",
                    action="NO_TRADE", chosen_side=None,
                    reason=f"neutral_band:low_confidence({confidence:.2f})",
                    price_snap=price_snap, market_snap=market_snap,
                    window_open=window_open, fair_result=fair_result,
                    yes_edge=yes_edge, no_edge=no_edge,
                    bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                    regime=regime, pattern=pattern, confidence=confidence,
                )

        # Guard: minimum confidence
        if confidence < self._cfg.min_confidence_for_taker:
            return self._make_decision(
                ts=ts, window_ts=window_ts, lane="selective_taker",
                action="NO_TRADE", chosen_side=None,
                reason=f"low_confidence({confidence:.2f})",
                price_snap=price_snap, market_snap=market_snap,
                window_open=window_open, fair_result=fair_result,
                yes_edge=yes_edge, no_edge=no_edge,
                bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
                regime=regime, pattern=pattern, confidence=confidence,
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
            )

        return self._make_decision(
            ts=ts, window_ts=window_ts, lane="selective_taker",
            action="PAPER_TRADE", chosen_side=best.side, reason="edge_and_risk_ok",
            price_snap=price_snap, market_snap=market_snap,
            window_open=window_open, fair_result=fair_result,
            yes_edge=yes_edge, no_edge=no_edge,
            bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
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

        shadow_confidence = _compute_confidence(
            fair_yes_prob=fair_result.fair_yes_prob,
            implied_yes_prob=market_snap.implied_yes_prob,
            delta_pct=fair_result.delta_pct,
            regime=regime,
            pattern=pattern,
            min_edge=self._edge._min_edge,
        )
        return self._make_decision(
            ts=ts, window_ts=window_ts, lane="maker_shadow",
            action="SHADOW_QUOTE", chosen_side=chosen_side, reason="shadow_edge_identified",
            price_snap=price_snap, market_snap=market_snap,
            window_open=window_open, fair_result=fair_result,
            yes_edge=yes_edge, no_edge=no_edge,
            bankroll_state=bankroll_state, binance_age_ms=binance_age_ms,
            regime=regime, pattern=pattern, confidence=shadow_confidence,
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
        return SignalDecision(
            ts=ts, window_ts=window_ts,
            lane="selective_taker", action="NO_TRADE",
            chosen_side=None, reason=reason,
            seconds_to_expiry=ste,
            elapsed_from_window_start=elapsed,
            btc_mid=price_snap.btc_mid,
            window_open=window_open,
            bankroll=bankroll_state.bankroll,
            data_age_ms=binance_age_ms,
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
    ) -> SignalDecision:
        ste = market_snap.seconds_to_expiry
        elapsed = max(0.0, self._cfg.window_sec - ste)
        # Compute fee fields for logging
        fee_est = None
        if fair_result.fair_yes_prob > 0:
            from fee_engine import FeeEngine  # avoid circular at module level
            # Use the engine attached to edge_engine
            fee_est = self._edge._fee.taker_estimate(fair_result.fair_yes_prob)
        return SignalDecision(
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
            realized_vol_60s=price_snap.realized_vol_60s,
            fair_yes_prob=fair_result.fair_yes_prob,
            implied_yes_prob=market_snap.implied_yes_prob,
            raw_edge_yes=yes_edge.raw_edge,
            raw_edge_no=no_edge.raw_edge,
            after_fee_edge_yes=yes_edge.after_fee_edge,
            after_fee_edge_no=no_edge.after_fee_edge,
            fee_per_share=round(fee_est.fee_per_share, 8) if fee_est else 0.0,
            effective_rate=round(fee_est.effective_rate, 6) if fee_est else 0.0,
            confidence_score=round(confidence, 4),
            bankroll=bankroll_state.bankroll,
            data_age_ms=binance_age_ms,
            regime=regime,
            pattern=pattern,
        )
