"""
Tests for metadata.fee_schedule — fee computation correctness.
These tests require no network, no external dependencies.
"""

import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from metadata.fee_schedule import FeeSchedule


class TestFeeScheduleKnownRate:
    def setup_method(self):
        self.fee = FeeSchedule(
            fee_rate=0.02,
            fee_source="api:clob/markets/0xABC",
            fees_enabled=True,
        )

    def test_is_known(self):
        assert self.fee.is_known() is True

    def test_effective_fee_usdc(self):
        # 2% of 5.0 USDC stake = 0.10
        result = self.fee.effective_fee_usdc(5.0)
        assert result == pytest.approx(0.10, rel=1e-6)

    def test_effective_cost_usdc(self):
        # entry_price=0.60, quantity=1.0
        # token_cost = 0.60 * 1.0 = 0.60
        # effective_cost = 0.60 * (1 + 0.02) = 0.612
        result = self.fee.effective_cost_usdc(entry_price=0.60, quantity=1.0)
        assert result == pytest.approx(0.612, rel=1e-6)

    def test_quantity_from_stake(self):
        # stake=5.0, entry_price=0.50, fee_rate=0.02
        # quantity = 5.0 / (0.50 * 1.02) = 5.0 / 0.51 = 9.803921...
        result = self.fee.quantity_from_stake(entry_price=0.50, stake_usdc=5.0)
        assert result == pytest.approx(5.0 / 0.51, rel=1e-6)

    def test_payout_if_correct(self):
        # payout = quantity * 1.0
        result = self.fee.payout_if_correct(quantity=9.80)
        assert result == pytest.approx(9.80, rel=1e-6)

    def test_net_pnl_correct(self):
        # stake=5.0, quantity=9.80, correct=True
        # fee = 0.02 * 5.0 = 0.10
        # payout = 9.80
        # net = 9.80 - 5.0 - 0.10 = 4.70
        result = self.fee.net_pnl(stake_usdc=5.0, quantity=9.80, correct=True)
        assert result == pytest.approx(4.70, rel=1e-6)

    def test_net_pnl_wrong(self):
        # stake=5.0, quantity=9.80, correct=False
        # fee = 0.02 * 5.0 = 0.10
        # net = -5.0 - 0.10 = -5.10
        result = self.fee.net_pnl(stake_usdc=5.0, quantity=9.80, correct=False)
        assert result == pytest.approx(-5.10, rel=1e-6)


class TestFeeScheduleUnknownRate:
    def setup_method(self):
        self.fee = FeeSchedule(fee_rate=None, fee_source=None, fees_enabled=None)

    def test_is_not_known(self):
        assert self.fee.is_known() is False

    def test_effective_fee_returns_none(self):
        assert self.fee.effective_fee_usdc(5.0) is None

    def test_effective_cost_returns_none(self):
        assert self.fee.effective_cost_usdc(0.50, 10.0) is None

    def test_quantity_returns_none(self):
        assert self.fee.quantity_from_stake(0.50, 5.0) is None

    def test_net_pnl_returns_none(self):
        assert self.fee.net_pnl(5.0, 9.80, correct=True) is None
        assert self.fee.net_pnl(5.0, 9.80, correct=False) is None


class TestFeeScheduleZeroRate:
    def setup_method(self):
        self.fee = FeeSchedule(fee_rate=0.0, fee_source="api:clob/markets/0xDEF", fees_enabled=False)

    def test_is_known(self):
        # fee_rate=0.0 is known (fee_source is not None)
        assert self.fee.is_known() is True

    def test_zero_fee(self):
        assert self.fee.effective_fee_usdc(5.0) == pytest.approx(0.0)

    def test_cost_equals_stake_at_zero_fee(self):
        # effective_cost = token_cost * (1 + 0) = token_cost
        assert self.fee.effective_cost_usdc(0.50, 10.0) == pytest.approx(5.0)


class TestFeeScheduleEdgeCases:
    def test_entry_price_zero_returns_none(self):
        fee = FeeSchedule(0.02, "api:x", True)
        result = fee.quantity_from_stake(entry_price=0.0, stake_usdc=5.0)
        assert result is None

    def test_negative_entry_price_returns_none(self):
        fee = FeeSchedule(0.02, "api:x", True)
        result = fee.quantity_from_stake(entry_price=-0.5, stake_usdc=5.0)
        assert result is None

    def test_describe_includes_fee_rate(self):
        fee = FeeSchedule(0.02, "api:test", True)
        desc = fee.describe()
        assert "0.02" in desc
        assert "api:test" in desc
