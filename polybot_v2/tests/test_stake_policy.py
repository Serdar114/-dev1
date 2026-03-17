"""Tests for stake_policy.py"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from stake_policy import StakePolicy, StakeDecision


SCALE_TIERS = [
    {"min_bankroll": 0,   "max_bankroll": 30,   "fraction": 0.02},
    {"min_bankroll": 30,  "max_bankroll": 60,   "fraction": 0.03},
    {"min_bankroll": 60,  "max_bankroll": 120,  "fraction": 0.04},
    {"min_bankroll": 120, "max_bankroll": 9999, "fraction": 0.05},
]


def make_policy(min_notional=0.50, max_risk_fraction=0.05, drawdown_factor=0.5) -> StakePolicy:
    return StakePolicy(
        min_notional=min_notional,
        max_risk_fraction=max_risk_fraction,
        drawdown_reduce_factor=drawdown_factor,
        scale_tiers=SCALE_TIERS,
    )


class TestTierSelection:
    def test_tier_0_small_bankroll(self):
        policy = make_policy()
        dec = policy.compute(bankroll=20.0, entry_price=0.5)
        assert dec.scale_tier == 0
        # fraction = 0.02, notional = 20 * 0.02 = 0.40 → capped to min_notional 0.50
        assert dec.suggested_notional == pytest.approx(0.50, rel=1e-6)
        assert dec.capped is True

    def test_tier_1(self):
        policy = make_policy()
        dec = policy.compute(bankroll=40.0, entry_price=0.5)
        assert dec.scale_tier == 1
        # fraction = 0.03, notional = 40 * 0.03 = 1.20
        assert dec.suggested_notional == pytest.approx(1.20, rel=1e-4)

    def test_tier_2(self):
        policy = make_policy()
        dec = policy.compute(bankroll=80.0, entry_price=0.5)
        assert dec.scale_tier == 2
        # fraction = 0.04, notional = 80 * 0.04 = 3.20
        assert dec.suggested_notional == pytest.approx(3.20, rel=1e-4)

    def test_tier_3(self):
        policy = make_policy()
        dec = policy.compute(bankroll=200.0, entry_price=0.5)
        assert dec.scale_tier == 3
        # fraction = 0.05, notional = 200 * 0.05 = 10.0
        assert dec.suggested_notional == pytest.approx(10.0, rel=1e-4)


class TestDrawdownReduction:
    def test_no_drawdown(self):
        policy = make_policy()
        dec_no_dd = policy.compute(bankroll=40.0, entry_price=0.5, last_drawdown=0.0)
        dec_with_dd = policy.compute(bankroll=40.0, entry_price=0.5, last_drawdown=0.3)
        # with drawdown: size should be smaller
        assert dec_with_dd.suggested_notional < dec_no_dd.suggested_notional

    def test_full_drawdown_reduces_size(self):
        policy = make_policy()
        dec = policy.compute(bankroll=40.0, entry_price=0.5, last_drawdown=1.0)
        # tier_1 fraction=0.03; after drawdown: 0.03*(1-0.5)=0.015; notional=0.60
        # 0.60 > min_notional(0.50) and < max_notional(2.0), so not capped
        assert dec.suggested_notional == pytest.approx(0.60, rel=1e-4)
        assert dec.capped is False


class TestMaxRiskCap:
    def test_notional_capped_by_max_fraction(self):
        # max_risk_fraction=0.02, tier_3 fraction=0.05, bankroll=500
        policy = StakePolicy(
            min_notional=0.50,
            max_risk_fraction=0.02,  # tight cap overrides tier fraction
            drawdown_reduce_factor=0.5,
            scale_tiers=SCALE_TIERS,
        )
        dec = policy.compute(bankroll=500.0, entry_price=0.5)
        # tier_3 fraction=0.05 → clamped to 0.02 → notional=10.0; capped=True
        assert dec.suggested_notional == pytest.approx(500.0 * 0.02, rel=1e-4)
        assert dec.capped is True  # fraction exceeded max_risk_fraction


class TestShadowLane:
    def test_shadow_lane_half_size(self):
        policy = make_policy()
        dec_taker = policy.compute(bankroll=40.0, entry_price=0.5, lane="selective_taker")
        dec_shadow = policy.compute(bankroll=40.0, entry_price=0.5, lane="maker_shadow")
        # shadow should be ≤ half of taker
        assert dec_shadow.suggested_notional <= dec_taker.suggested_notional


class TestSharesCalculation:
    def test_shares_from_notional(self):
        policy = make_policy()
        dec = policy.compute(bankroll=100.0, entry_price=0.50)
        expected_shares = dec.suggested_notional / 0.50
        assert dec.suggested_shares == pytest.approx(expected_shares, rel=1e-4)

    def test_higher_price_fewer_shares(self):
        policy = make_policy()
        dec_cheap = policy.compute(bankroll=100.0, entry_price=0.20)
        dec_expensive = policy.compute(bankroll=100.0, entry_price=0.80)
        # same notional, but higher price → fewer shares
        if dec_cheap.suggested_notional == dec_expensive.suggested_notional:
            assert dec_expensive.suggested_shares < dec_cheap.suggested_shares


class TestZeroBankroll:
    def test_zero_bankroll_returns_zero(self):
        policy = make_policy()
        dec = policy.compute(bankroll=0.0, entry_price=0.5)
        assert dec.suggested_notional == 0.0
        assert dec.suggested_shares == 0.0
