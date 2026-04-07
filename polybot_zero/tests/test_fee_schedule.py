"""
Tests for metadata.fee_schedule — fee curve computation correctness.

New fee formula (Polymarket Crypto category, exponent=1):
  fee = stake × feeRate × p × (1 − p)
where feeRate = feeRateBps / 10000, p = entry_price

These tests require no network, no external dependencies.
"""

import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from metadata.fee_schedule import FeeSchedule, FeeProvenance


class TestFeeScheduleKnownRate:
    """fee_rate_bps=200 (2%), entry_price=0.50"""

    def setup_method(self):
        self.fee = FeeSchedule.from_bps(fee_rate_bps=200, token_id="0xABC")

    def test_is_known(self):
        assert self.fee.is_known() is True

    def test_provenance_confirmed(self):
        assert self.fee.provenance == FeeProvenance.CONFIRMED

    def test_fee_rate_decimal(self):
        assert self.fee.fee_rate == pytest.approx(0.02)

    def test_fee_usdc_at_mid(self):
        # fee = 5.0 * 0.02 * 0.5 * 0.5 = 0.025
        result = self.fee.fee_usdc(stake_usdc=5.0, entry_price=0.5)
        assert result == pytest.approx(0.025, rel=1e-6)

    def test_fee_usdc_at_60pct(self):
        # fee = 5.0 * 0.02 * 0.6 * 0.4 = 0.024
        result = self.fee.fee_usdc(stake_usdc=5.0, entry_price=0.6)
        assert result == pytest.approx(0.024, rel=1e-6)

    def test_total_cost_usdc(self):
        # stake=5.0 + fee=0.025 = 5.025 at p=0.5
        result = self.fee.total_cost_usdc(stake_usdc=5.0, entry_price=0.5)
        assert result == pytest.approx(5.025, rel=1e-6)

    def test_quantity_from_stake(self):
        # quantity = stake / entry_price = 5.0 / 0.50 = 10.0
        result = self.fee.quantity_from_stake(entry_price=0.50, stake_usdc=5.0)
        assert result == pytest.approx(10.0, rel=1e-6)

    def test_net_pnl_correct(self):
        # stake=5.0, p=0.50, correct=True
        # quantity = 5.0 / 0.5 = 10.0
        # payout = 10.0
        # fee = 5.0 * 0.02 * 0.5 * 0.5 = 0.025
        # net = 10.0 - 5.0 - 0.025 = 4.975
        result = self.fee.net_pnl(stake_usdc=5.0, entry_price=0.5, correct=True)
        assert result == pytest.approx(4.975, rel=1e-6)

    def test_net_pnl_wrong(self):
        # stake=5.0, p=0.50, correct=False
        # fee = 0.025
        # net = -5.0 - 0.025 = -5.025
        result = self.fee.net_pnl(stake_usdc=5.0, entry_price=0.5, correct=False)
        assert result == pytest.approx(-5.025, rel=1e-6)


class TestFeeScheduleUnknownRate:
    def setup_method(self):
        self.fee = FeeSchedule.from_bps(fee_rate_bps=None, token_id="0xNOFEE")

    def test_is_not_known(self):
        assert self.fee.is_known() is False

    def test_provenance_unresolved(self):
        assert self.fee.provenance == FeeProvenance.UNRESOLVED

    def test_fee_usdc_returns_none(self):
        assert self.fee.fee_usdc(stake_usdc=5.0, entry_price=0.5) is None

    def test_total_cost_returns_none(self):
        assert self.fee.total_cost_usdc(stake_usdc=5.0, entry_price=0.5) is None

    def test_quantity_returns_none(self):
        assert self.fee.quantity_from_stake(0.50, 5.0) is None

    def test_net_pnl_returns_none(self):
        assert self.fee.net_pnl(5.0, 0.5, correct=True) is None
        assert self.fee.net_pnl(5.0, 0.5, correct=False) is None


class TestFeeScheduleZeroRate:
    def setup_method(self):
        self.fee = FeeSchedule.from_bps(fee_rate_bps=0, token_id="0xZERO")

    def test_is_known(self):
        # fee_rate_bps=0 is accepted (provenance=ZERO)
        assert self.fee.is_known() is True

    def test_provenance_zero(self):
        assert self.fee.provenance == FeeProvenance.ZERO

    def test_zero_fee(self):
        assert self.fee.fee_usdc(stake_usdc=5.0, entry_price=0.5) == pytest.approx(0.0)

    def test_cost_equals_stake_at_zero_fee(self):
        # total_cost = stake + 0 = stake
        assert self.fee.total_cost_usdc(stake_usdc=5.0, entry_price=0.5) == pytest.approx(5.0)


class TestFeeScheduleEdgeCases:
    def test_entry_price_zero_quantity_returns_none(self):
        fee = FeeSchedule.from_bps(200)
        result = fee.quantity_from_stake(entry_price=0.0, stake_usdc=5.0)
        assert result is None

    def test_negative_entry_price_quantity_returns_none(self):
        fee = FeeSchedule.from_bps(200)
        result = fee.quantity_from_stake(entry_price=-0.5, stake_usdc=5.0)
        assert result is None

    def test_fee_curve_symmetric_around_50pct(self):
        # fee at p=0.3 should equal fee at p=0.7 (symmetry of p*(1-p))
        fee = FeeSchedule.from_bps(200)
        f_30 = fee.fee_usdc(10.0, 0.30)
        f_70 = fee.fee_usdc(10.0, 0.70)
        assert f_30 == pytest.approx(f_70, rel=1e-6)

    def test_fee_maximised_at_50pct(self):
        # p*(1-p) is maximised at p=0.5
        fee = FeeSchedule.from_bps(200)
        f_50 = fee.fee_usdc(10.0, 0.5)
        f_40 = fee.fee_usdc(10.0, 0.4)
        f_60 = fee.fee_usdc(10.0, 0.6)
        assert f_50 > f_40
        assert f_50 > f_60

    def test_describe_includes_bps(self):
        fee = FeeSchedule.from_bps(150, token_id="tok123")
        desc = fee.describe()
        assert "150" in desc
        assert "tok123" in desc
