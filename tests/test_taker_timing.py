"""
tests/test_taker_timing.py

Covers fix #6: taker lane must use decision-time price, record decision_ts,
and document execution_source. Assumed slippage is logged (not applied).
"""
import time
import pytest
from execution.taker_lane import (
    TakerLane,
    SRC_FAST_FEED_AT_SIGNAL,
    SRC_CLOB_ASK_AT_SIGNAL,
    SRC_UNAVAILABLE,
)


@pytest.fixture
def taker(base_config):
    return TakerLane(base_config)


class TestTakerDecisionTiming:
    def test_decision_ts_is_recorded(self, taker):
        ts = time.time()
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.87, bankroll=30.0,
            decision_ts=ts,
        )
        assert result.decision_ts == pytest.approx(ts)

    def test_decision_ts_auto_set_if_not_provided(self, taker):
        before = time.time()
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.87, bankroll=30.0,
        )
        after = time.time()
        assert result.decision_ts is not None
        assert before <= result.decision_ts <= after

    def test_execution_source_recorded(self, taker):
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.87, bankroll=30.0,
            execution_source=SRC_FAST_FEED_AT_SIGNAL,
        )
        assert result.execution_source == SRC_FAST_FEED_AT_SIGNAL

    def test_clob_source_recorded(self, taker):
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.87, bankroll=30.0,
            execution_source=SRC_CLOB_ASK_AT_SIGNAL,
        )
        assert result.execution_source == SRC_CLOB_ASK_AT_SIGNAL

    def test_unavailable_source_when_no_signal(self, taker):
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="NONE",
            decision_price=None, bankroll=30.0,
        )
        assert result.execution_source == SRC_UNAVAILABLE
        assert result.filled is False

    def test_assumed_slippage_is_logged(self, taker):
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.87, bankroll=30.0,
        )
        # v1: slippage field exists and is 0 bps (not applied to P&L)
        assert result.assumed_slippage_bps == pytest.approx(0.0)

    def test_assumed_slippage_not_applied_to_pnl(self, taker):
        """Slippage is documented but must NOT distort net_pnl in v1."""
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.87, bankroll=30.0,
        )
        result = taker.settle(result, "YES")
        expected_gross = (1.0 - 0.87) * 5
        from execution.fees import compute_taker_fee
        fee = compute_taker_fee(0.87, 5, 0.02).total_fee
        expected_net = expected_gross - fee
        assert result.net_pnl == pytest.approx(expected_net)

    def test_bankroll_fraction_at_30_not_1000(self, taker):
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=0.87, bankroll=30.0,
        )
        expected = (0.87 * 5) / 30.0
        assert result.bankroll_fraction == pytest.approx(expected)
        assert result.bankroll_fraction != pytest.approx(0.87 * 5 / 1000.0)

    def test_window_log_has_decision_ts_and_source_fields(self):
        from logger.summary import WindowLog
        wl = WindowLog(window_open_ts=1, slug="s", phase="0c")
        assert hasattr(wl, "taker_decision_ts")
        assert hasattr(wl, "taker_execution_source")
        assert hasattr(wl, "taker_assumed_slippage_bps")


class TestTakerFeeOnDecisionPrice:
    def test_fee_computed_from_decision_price(self, taker):
        p = 0.87
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=p, bankroll=30.0,
        )
        from execution.fees import compute_taker_fee
        expected_fee = compute_taker_fee(p, 5, 0.02).total_fee
        assert result.total_fee == pytest.approx(expected_fee)

    def test_break_even_wr_accounts_for_fee(self, taker):
        p = 0.87
        result = taker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            decision_price=p, bankroll=30.0,
        )
        from execution.fees import compute_taker_fee, break_even_win_rate
        fee_per_share = compute_taker_fee(p, 5, 0.02).fee_per_share
        expected_be = break_even_win_rate(p, fee_per_share)
        assert result.break_even_wr_estimate == pytest.approx(expected_be)
