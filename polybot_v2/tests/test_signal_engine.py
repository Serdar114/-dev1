"""Tests for signal_engine.py – no-trade vs trade decisions."""

import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from signal_engine import SignalEngine, _classify_regime, _classify_pattern
from fair_prob_engine import FairProbEngine
from edge_engine import EdgeEngine
from fee_engine import FeeEngine
from stake_policy import StakePolicy
from risk_manager import RiskManager, RiskState
from models import BankrollState, MarketSnapshot, PriceSnapshot
from settings import Settings


CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def make_settings() -> Settings:
    return Settings(CONFIG_PATH)


def make_components(cfg: Settings):
    fee_engine = FeeEngine(
        taker_fee_rate=cfg.taker_fee_rate,
        maker_rebate_rate=cfg.maker_rebate_rate,
    )
    fair_engine = FairProbEngine(
        sigma_floor=cfg.sigma_floor,
        tau_floor=cfg.tau_floor,
        zscore_clip=cfg.zscore_clip,
        prob_clip_min=cfg.prob_clip_min,
        prob_clip_max=cfg.prob_clip_max,
    )
    edge_engine = EdgeEngine(fee_engine, cfg.min_after_fee_edge)
    stake_policy = StakePolicy(
        min_notional=cfg.min_notional,
        max_risk_fraction=cfg.max_risk_fraction,
        drawdown_reduce_factor=cfg.drawdown_reduce_factor,
        scale_tiers=cfg.scale_tiers,
    )
    risk_mgr = RiskManager(
        max_actions_per_window=cfg.max_actions_per_window,
        max_consecutive_losses=cfg.max_consecutive_losses,
        cooldown_windows=cfg.cooldown_windows,
        max_shadow_quotes_per_window=cfg.max_shadow_quotes_per_window,
        max_notional_per_trade=30.0 * cfg.max_risk_fraction,
        stale_binance_ms=cfg.stale_binance_ms,
        stale_polymarket_ms=cfg.stale_polymarket_ms,
    )
    engine = SignalEngine(cfg, fair_engine, edge_engine, stake_policy, risk_mgr)
    return engine


def make_market(
    implied_yes=0.50,
    ste=150.0,
    bid_yes=0.48, ask_yes=0.52,
    bid_no=0.48, ask_no=0.52,
) -> MarketSnapshot:
    now = time.time()
    return MarketSnapshot(
        condition_id="test_cid",
        token_id_yes="yes_token",
        token_id_no="no_token",
        best_bid_yes=bid_yes,
        best_ask_yes=ask_yes,
        best_bid_no=bid_no,
        best_ask_no=ask_no,
        last_trade_price_yes=implied_yes,
        window_end_ts=now + ste,
        fetched_at=now,
    )


def make_price(btc_mid=50000.0, vol=0.002) -> PriceSnapshot:
    return PriceSnapshot(btc_mid=btc_mid, timestamp=time.time(), realized_vol_60s=vol)


def make_bankroll(balance=30.0) -> BankrollState:
    return BankrollState(bankroll=balance, peak_bankroll=balance, total_pnl=0.0, drawdown=0.0)


def make_risk_state() -> RiskState:
    return RiskState(max_notional_per_trade=5.0)


class TestRegimeClassification:
    def test_quiet(self):
        cfg = make_settings()
        r = _classify_regime(0.0001, 0.5, cfg.extreme_upper, cfg.extreme_lower, cfg.sigma_floor, cfg.sigma_floor)
        assert r == "QUIET"

    def test_extreme_upper(self):
        cfg = make_settings()
        r = _classify_regime(0.01, 0.90, cfg.extreme_upper, cfg.extreme_lower, 0.003, cfg.sigma_floor)
        assert r == "EXTREME_ZONE"

    def test_extreme_lower(self):
        cfg = make_settings()
        r = _classify_regime(-0.01, 0.10, cfg.extreme_upper, cfg.extreme_lower, 0.003, cfg.sigma_floor)
        assert r == "EXTREME_ZONE"

    def test_trending(self):
        cfg = make_settings()
        r = _classify_regime(0.005, 0.6, cfg.extreme_upper, cfg.extreme_lower, cfg.sigma_floor * 4, cfg.sigma_floor)
        assert r == "TRENDING"


class TestPatternClassification:
    def test_noise_default(self):
        cfg = make_settings()
        p = _classify_pattern(0.0001, cfg.sigma_floor, cfg.sigma_floor)
        assert p == "NOISE"

    def test_sustained_move(self):
        cfg = make_settings()
        p = _classify_pattern(0.005, cfg.sigma_floor * 3, cfg.sigma_floor)
        assert p == "SUSTAINED_MOVE"


class TestTakerOutsideEntryWindow:
    def test_too_early_no_trade(self):
        cfg = make_settings()
        engine = make_components(cfg)
        # ste = 290 → elapsed = 10s < entry_start_sec=30
        market = make_market(ste=290.0)
        price = make_price()
        decision = engine.evaluate_taker(
            price_snap=price,
            market_snap=market,
            window_open=50000.0,
            bankroll_state=make_bankroll(),
            risk_state=make_risk_state(),
            binance_age_ms=100.0,
            polymarket_age_ms=1000.0,
        )
        assert decision.action == "NO_TRADE"
        assert "entry_window" in decision.reason

    def test_too_late_no_trade(self):
        cfg = make_settings()
        engine = make_components(cfg)
        # ste = 50s → elapsed = 250s > entry_end_sec=240
        market = make_market(ste=50.0)
        price = make_price()
        decision = engine.evaluate_taker(
            price_snap=price,
            market_snap=market,
            window_open=50000.0,
            bankroll_state=make_bankroll(),
            risk_state=make_risk_state(),
            binance_age_ms=100.0,
            polymarket_age_ms=1000.0,
        )
        assert decision.action == "NO_TRADE"
        assert "entry_window" in decision.reason


class TestTakerImpliedProbZone:
    def test_extreme_prob_no_trade(self):
        cfg = make_settings()
        engine = make_components(cfg)
        # implied_yes = 0.95 > taker_max_prob = 0.90
        market = make_market(implied_yes=0.95, ask_yes=0.95, bid_yes=0.94, ste=150.0)
        price = make_price()
        decision = engine.evaluate_taker(
            price_snap=price,
            market_snap=market,
            window_open=50000.0,
            bankroll_state=make_bankroll(),
            risk_state=make_risk_state(),
            binance_age_ms=100.0,
            polymarket_age_ms=1000.0,
        )
        assert decision.action == "NO_TRADE"
        assert "implied_prob" in decision.reason


class TestTakerRiskReject:
    def test_stale_binance_no_trade(self):
        cfg = make_settings()
        engine = make_components(cfg)
        market = make_market(ste=150.0)
        price = make_price()
        decision = engine.evaluate_taker(
            price_snap=price,
            market_snap=market,
            window_open=50000.0,
            bankroll_state=make_bankroll(),
            risk_state=make_risk_state(),
            binance_age_ms=10000.0,  # stale: > 3000ms
            polymarket_age_ms=1000.0,
        )
        assert decision.action == "NO_TRADE"
        assert "stale" in decision.reason.lower() or "risk_reject" in decision.reason

    def test_cooldown_no_trade(self):
        cfg = make_settings()
        engine = make_components(cfg)
        market = make_market(ste=150.0)
        price = make_price()
        risk_state = make_risk_state()
        risk_state.cooldown_windows_remaining = 1  # in cooldown

        decision = engine.evaluate_taker(
            price_snap=price,
            market_snap=market,
            window_open=50000.0,
            bankroll_state=make_bankroll(),
            risk_state=risk_state,
            binance_age_ms=100.0,
            polymarket_age_ms=1000.0,
        )
        assert decision.action == "NO_TRADE"
        assert "cooldown" in decision.reason


class TestTakerNoEdge:
    def test_no_edge_no_trade(self):
        """When implied price leaves no after-fee edge, expect NO_TRADE."""
        cfg = make_settings()
        engine = make_components(cfg)
        # fair_yes ≈ 0.5 (btc flat), ask_yes=0.55 and ask_no=0.50
        # YES: 0.5 - 0.55*1.02 = -0.061  → no edge
        # NO:  0.5 - 0.50*1.02 = -0.010  → no edge (both sides fee-heavy)
        market = make_market(bid_yes=0.54, ask_yes=0.55, bid_no=0.49, ask_no=0.50, ste=150.0)
        price = make_price(btc_mid=50000.0)  # flat → fair_yes ≈ 0.5

        decision = engine.evaluate_taker(
            price_snap=price,
            market_snap=market,
            window_open=50000.0,
            bankroll_state=make_bankroll(),
            risk_state=make_risk_state(),
            binance_age_ms=100.0,
            polymarket_age_ms=1000.0,
        )
        assert decision.action == "NO_TRADE"


class TestShadowOutsideWindow:
    def test_shadow_too_early(self):
        cfg = make_settings()
        engine = make_components(cfg)
        # ste = 295 → elapsed = 5s < shadow_start_sec=10
        market = make_market(ste=295.0)
        price = make_price()

        decision = engine.evaluate_shadow(
            price_snap=price,
            market_snap=market,
            window_open=50000.0,
            bankroll_state=make_bankroll(),
            risk_state=make_risk_state(),
            binance_age_ms=100.0,
            polymarket_age_ms=1000.0,
        )
        assert decision.action == "NO_QUOTE"
        assert "outside_shadow_window" in decision.reason


class TestSignalDecisionFields:
    def test_decision_has_required_fields(self):
        cfg = make_settings()
        engine = make_components(cfg)
        market = make_market(ste=150.0)
        price = make_price()

        decision = engine.evaluate_taker(
            price_snap=price,
            market_snap=market,
            window_open=50000.0,
            bankroll_state=make_bankroll(),
            risk_state=make_risk_state(),
            binance_age_ms=100.0,
            polymarket_age_ms=1000.0,
        )
        assert hasattr(decision, "ts")
        assert hasattr(decision, "window_ts")
        assert hasattr(decision, "lane")
        assert hasattr(decision, "action")
        assert hasattr(decision, "reason")
        assert decision.lane == "selective_taker"
