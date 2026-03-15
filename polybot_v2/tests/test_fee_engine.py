"""Tests for fee_engine.py"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fee_engine import FeeEngine, TakerFeeEstimate, MakerFeeEstimate


class TestFeeEngineInit:
    def test_valid_init(self):
        fe = FeeEngine(taker_fee_rate=0.02, maker_rebate_rate=0.0)
        assert fe.taker_fee_rate == 0.02
        assert fe.maker_rebate_rate == 0.0

    def test_invalid_taker_rate_negative(self):
        with pytest.raises(ValueError):
            FeeEngine(taker_fee_rate=-0.01, maker_rebate_rate=0.0)

    def test_invalid_taker_rate_over_one(self):
        with pytest.raises(ValueError):
            FeeEngine(taker_fee_rate=1.0, maker_rebate_rate=0.0)

    def test_invalid_maker_rebate_negative(self):
        with pytest.raises(ValueError):
            FeeEngine(taker_fee_rate=0.02, maker_rebate_rate=-0.01)


class TestTakerEstimate:
    def setup_method(self):
        self.fe = FeeEngine(taker_fee_rate=0.02, maker_rebate_rate=0.0)

    def test_basic_math(self):
        est = self.fe.taker_estimate(0.50)
        assert est.entry_price == 0.50
        assert est.fee_per_share == pytest.approx(0.01, rel=1e-9)
        assert est.effective_cost == pytest.approx(0.51, rel=1e-9)
        assert est.breakeven_price == pytest.approx(0.51, rel=1e-9)

    def test_zero_fee(self):
        fe = FeeEngine(taker_fee_rate=0.0, maker_rebate_rate=0.0)
        est = fe.taker_estimate(0.60)
        assert est.fee_per_share == 0.0
        assert est.effective_cost == 0.60

    def test_small_price(self):
        est = self.fe.taker_estimate(0.10)
        assert est.fee_per_share == pytest.approx(0.002, rel=1e-9)
        assert est.effective_cost == pytest.approx(0.102, rel=1e-9)

    def test_near_one_price(self):
        est = self.fe.taker_estimate(0.95)
        assert est.fee_per_share == pytest.approx(0.019, rel=1e-9)
        assert est.effective_cost == pytest.approx(0.969, rel=1e-9)


class TestTakerAfterFeePnl:
    def setup_method(self):
        self.fe = FeeEngine(taker_fee_rate=0.02, maker_rebate_rate=0.0)

    def test_win(self):
        # Buy YES at 0.50, win (outcome=1.0)
        pnl = self.fe.taker_after_fee_pnl_per_share(0.50, 1.0)
        # pnl = 1.0 - 0.50 - 0.01 = 0.49
        assert pnl == pytest.approx(0.49, rel=1e-9)

    def test_loss(self):
        # Buy YES at 0.50, lose (outcome=0.0)
        pnl = self.fe.taker_after_fee_pnl_per_share(0.50, 0.0)
        # pnl = 0.0 - 0.50 - 0.01 = -0.51
        assert pnl == pytest.approx(-0.51, rel=1e-9)

    def test_breakeven_impossible(self):
        # At entry_price=0.50 fee=0.02: breakeven impossible without fee
        # We need price 0.51 to break even (not achievable at resolution 0/1)
        pass


class TestTakerEdge:
    def setup_method(self):
        self.fe = FeeEngine(taker_fee_rate=0.02, maker_rebate_rate=0.0)

    def test_positive_edge_yes(self):
        # fair_yes=0.70, ask_yes=0.55 → raw_edge=0.15, fee=0.011 → after_fee=0.139
        edge = self.fe.taker_edge_yes(fair_yes_prob=0.70, ask_yes=0.55)
        expected = 0.70 - (0.55 + 0.55 * 0.02)
        assert edge == pytest.approx(expected, rel=1e-9)

    def test_negative_edge_yes(self):
        # fair_yes=0.50, ask_yes=0.55 → below fair
        edge = self.fe.taker_edge_yes(fair_yes_prob=0.50, ask_yes=0.55)
        assert edge < 0

    def test_positive_edge_no(self):
        edge = self.fe.taker_edge_no(fair_no_prob=0.70, ask_no=0.55)
        expected = 0.70 - (0.55 + 0.55 * 0.02)
        assert edge == pytest.approx(expected, rel=1e-9)


class TestMakerEstimate:
    def setup_method(self):
        self.fe = FeeEngine(taker_fee_rate=0.02, maker_rebate_rate=0.0)

    def test_no_rebate_phase1(self):
        est = self.fe.maker_estimate(0.48)
        assert est.rebate_per_share == 0.0
        assert est.effective_proceeds == pytest.approx(0.48, rel=1e-9)

    def test_with_rebate(self):
        fe = FeeEngine(taker_fee_rate=0.02, maker_rebate_rate=0.005)
        est = fe.maker_estimate(0.48)
        assert est.rebate_per_share == pytest.approx(0.0024, rel=1e-9)
        assert est.effective_proceeds == pytest.approx(0.4824, rel=1e-9)
