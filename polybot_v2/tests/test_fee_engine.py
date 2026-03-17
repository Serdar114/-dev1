"""Tests for fee_engine.py — dynamic crypto fee curve."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fee_engine import FeeEngine, TakerFeeEstimate, MakerFeeEstimate


# Reference: fee_rate_base=0.25, fee_exponent=2
# fee_per_share = p * 0.25 * (p * (1-p))^2

def _expected_fee(p: float, rate: float = 0.25, exp: int = 2) -> float:
    return p * rate * (p * (1.0 - p)) ** exp


class TestFeeEngineInit:
    def test_valid_init(self):
        fe = FeeEngine(fee_rate_base=0.25, fee_exponent=2, maker_rebate_share=0.20)
        assert fe.fee_rate_base == 0.25
        assert fe.fee_exponent == 2
        assert fe.maker_rebate_share == 0.20

    def test_defaults_ok(self):
        fe = FeeEngine(fee_rate_base=0.25, fee_exponent=2)
        assert fe.maker_rebate_share == 0.0

    def test_invalid_fee_rate_negative(self):
        with pytest.raises(ValueError):
            FeeEngine(fee_rate_base=-0.01, fee_exponent=2)

    def test_invalid_exponent_negative(self):
        with pytest.raises(ValueError):
            FeeEngine(fee_rate_base=0.25, fee_exponent=-1)

    def test_invalid_rebate_negative(self):
        with pytest.raises(ValueError):
            FeeEngine(fee_rate_base=0.25, fee_exponent=2, maker_rebate_share=-0.01)


class TestTakerEstimateFormula:
    """Verify the canonical fee examples from spec."""

    def setup_method(self):
        self.fe = FeeEngine(fee_rate_base=0.25, fee_exponent=2)

    def test_p050(self):
        # p=0.50 -> fee = 0.50 * 0.25 * (0.50*0.50)^2 = 0.50 * 0.25 * 0.0625 = 0.0078125
        est = self.fe.taker_estimate(0.50)
        assert est.fee_per_share == pytest.approx(0.0078125, rel=1e-9)
        assert est.effective_cost == pytest.approx(0.5078125, rel=1e-9)

    def test_p090(self):
        # p=0.90 -> fee = 0.90 * 0.25 * (0.90*0.10)^2 = 0.90 * 0.25 * 0.0081 = 0.0018225
        est = self.fe.taker_estimate(0.90)
        assert est.fee_per_share == pytest.approx(0.0018225, rel=1e-9)

    def test_p010(self):
        # p=0.10 -> fee = 0.10 * 0.25 * (0.10*0.90)^2 = 0.10 * 0.25 * 0.0081 = 0.0002025
        est = self.fe.taker_estimate(0.10)
        assert est.fee_per_share == pytest.approx(0.0002025, rel=1e-9)

    def test_effective_cost_equals_price_plus_fee(self):
        for p in (0.10, 0.30, 0.50, 0.70, 0.90):
            est = self.fe.taker_estimate(p)
            assert est.effective_cost == pytest.approx(p + est.fee_per_share, rel=1e-12)

    def test_breakeven_equals_effective_cost(self):
        est = self.fe.taker_estimate(0.50)
        assert est.breakeven_price == est.effective_cost

    def test_effective_rate_logged(self):
        est = self.fe.taker_estimate(0.50)
        # effective_rate = fee_per_share / entry_price
        assert est.effective_rate == pytest.approx(est.fee_per_share / 0.50, rel=1e-12)

    def test_zero_fee_rate(self):
        fe = FeeEngine(fee_rate_base=0.0, fee_exponent=2)
        est = fe.taker_estimate(0.60)
        assert est.fee_per_share == 0.0
        assert est.effective_cost == pytest.approx(0.60, rel=1e-12)

    def test_fee_smaller_near_extremes(self):
        # Near extremes p*(1-p) is small, so fee should be smaller than at p=0.5
        fe = self.fe
        fee_mid = fe.taker_estimate(0.50).fee_per_share
        fee_extreme = fe.taker_estimate(0.95).fee_per_share
        assert fee_extreme < fee_mid

    def test_fee_formula_general(self):
        """Verify against direct formula for various prices."""
        fe = self.fe
        for p in (0.15, 0.25, 0.40, 0.60, 0.75, 0.85):
            est = fe.taker_estimate(p)
            expected = _expected_fee(p)
            assert est.fee_per_share == pytest.approx(expected, rel=1e-10)


class TestTakerAfterFeePnl:
    def setup_method(self):
        self.fe = FeeEngine(fee_rate_base=0.25, fee_exponent=2)

    def test_win_at_p050(self):
        # outcome=1.0 (win): pnl = 1.0 - 0.50 - fee(0.50)
        fee = _expected_fee(0.50)
        pnl = self.fe.taker_after_fee_pnl_per_share(0.50, 1.0)
        assert pnl == pytest.approx(1.0 - 0.50 - fee, rel=1e-10)
        assert pnl > 0  # winning trade should be positive

    def test_loss_at_p050(self):
        # outcome=0.0 (loss): pnl = 0.0 - 0.50 - fee(0.50)
        fee = _expected_fee(0.50)
        pnl = self.fe.taker_after_fee_pnl_per_share(0.50, 0.0)
        assert pnl == pytest.approx(-0.50 - fee, rel=1e-10)
        assert pnl < 0

    def test_win_at_p090(self):
        fee = _expected_fee(0.90)
        pnl = self.fe.taker_after_fee_pnl_per_share(0.90, 1.0)
        assert pnl == pytest.approx(1.0 - 0.90 - fee, rel=1e-10)
        assert pnl > 0

    def test_loss_at_p090(self):
        fee = _expected_fee(0.90)
        pnl = self.fe.taker_after_fee_pnl_per_share(0.90, 0.0)
        assert pnl == pytest.approx(-0.90 - fee, rel=1e-10)


class TestTakerEdge:
    def setup_method(self):
        self.fe = FeeEngine(fee_rate_base=0.25, fee_exponent=2)

    def test_positive_edge_yes(self):
        # fair_yes=0.70, ask_yes=0.55
        edge = self.fe.taker_edge_yes(fair_yes_prob=0.70, ask_yes=0.55)
        fee = _expected_fee(0.55)
        expected = 0.70 - (0.55 + fee)
        assert edge == pytest.approx(expected, rel=1e-10)
        assert edge > 0

    def test_negative_edge_yes(self):
        # fair_yes=0.50, ask_yes=0.55 → fee makes it worse
        edge = self.fe.taker_edge_yes(fair_yes_prob=0.50, ask_yes=0.55)
        assert edge < 0

    def test_positive_edge_no(self):
        edge = self.fe.taker_edge_no(fair_no_prob=0.70, ask_no=0.55)
        fee = _expected_fee(0.55)
        expected = 0.70 - (0.55 + fee)
        assert edge == pytest.approx(expected, rel=1e-10)

    def test_edge_accounts_for_fee_correctly(self):
        # Verify edge = fair_prob - effective_cost exactly
        p = 0.48
        fair = 0.60
        edge = self.fe.taker_edge_yes(fair_yes_prob=fair, ask_yes=p)
        est = self.fe.taker_estimate(p)
        assert edge == pytest.approx(fair - est.effective_cost, rel=1e-12)


class TestMakerEstimate:
    def test_phase1_no_live_rebate(self):
        fe = FeeEngine(fee_rate_base=0.25, fee_exponent=2, maker_rebate_share=0.20)
        est = fe.maker_estimate(0.48)
        # Phase 1: rebate_per_share = 0 (informational only)
        assert est.rebate_per_share == 0.0
        assert est.effective_proceeds == pytest.approx(0.48, rel=1e-9)
        # But maker_rebate_share is stored for reference
        assert est.maker_rebate_share == pytest.approx(0.20, rel=1e-9)

    def test_maker_estimate_stores_quote_price(self):
        fe = FeeEngine(fee_rate_base=0.25, fee_exponent=2)
        est = fe.maker_estimate(0.45)
        assert est.quote_price == pytest.approx(0.45, rel=1e-9)


class TestFeeLogging:
    """Verify fields needed for logging are present."""

    def test_fee_per_share_present(self):
        fe = FeeEngine(fee_rate_base=0.25, fee_exponent=2)
        est = fe.taker_estimate(0.50)
        assert hasattr(est, "fee_per_share")
        assert est.fee_per_share > 0

    def test_effective_rate_present(self):
        fe = FeeEngine(fee_rate_base=0.25, fee_exponent=2)
        est = fe.taker_estimate(0.50)
        assert hasattr(est, "effective_rate")
        # effective_rate = fee_per_share / entry_price
        assert est.effective_rate == pytest.approx(est.fee_per_share / 0.50, rel=1e-12)
