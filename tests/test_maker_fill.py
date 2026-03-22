"""
tests/test_maker_fill.py

Covers maker fill simulation realism grades:
- PROVISIONAL_NO_PATH when intra_window_prices=None → filled=False, evaluable=False
- PROVISIONAL_SINGLE_POINT when len(prices)==1 → filled=False, evaluable=False
- PROVISIONAL_MULTI_POINT when len(prices)>=2 → fill based on path, evaluable=True
- OBSERVED_PATH when fill_realism_source explicitly set (WebSocket future path)
- No degenerate same-tick / auto-fill behaviour
"""
import pytest
from execution.maker_lane import (
    MakerLane,
    FILL_GRADE_PROVISIONAL,
    FILL_GRADE_PROVISIONAL_SINGLE,
    FILL_GRADE_PROVISIONAL_MULTI,
    FILL_GRADE_OBSERVED,
    FILL_GRADE_NA,
)


@pytest.fixture
def maker(base_config):
    return MakerLane(base_config)


class TestMakerFillRealism:
    def test_no_path_gives_provisional_grade(self, maker):
        result = maker.evaluate(
            window_open_ts=1,
            slug="test-slug",
            signal_direction="YES",
            intended_price=0.87,
            bankroll=30.0,
            intra_window_prices=None,
        )
        assert result.fill_realism_grade == FILL_GRADE_PROVISIONAL

    def test_no_path_means_not_filled(self, maker):
        """Conservative: no price path → no fill, never optimistic."""
        result = maker.evaluate(
            window_open_ts=1,
            slug="test-slug",
            signal_direction="YES",
            intended_price=0.87,
            bankroll=30.0,
            intra_window_prices=None,
        )
        assert result.filled is False
        assert result.fill_price is None

    def test_multi_point_path_with_crossing_price_fills(self, maker):
        """2+ prices, path dips to/below limit → PROVISIONAL_MULTI_POINT, filled."""
        result = maker.evaluate(
            window_open_ts=1,
            slug="test-slug",
            signal_direction="YES",
            intended_price=0.87,
            bankroll=30.0,
            intra_window_prices=[0.89, 0.88, 0.86, 0.87, 0.90],  # dips to 0.86 < 0.87
        )
        assert result.fill_realism_grade == FILL_GRADE_PROVISIONAL_MULTI
        assert result.filled is True
        assert result.fill_price == pytest.approx(0.87)
        assert result.fill_evaluable is True

    def test_multi_point_path_without_crossing_no_fill(self, maker):
        """2+ prices, none dip to/below limit → PROVISIONAL_MULTI_POINT, not filled."""
        result = maker.evaluate(
            window_open_ts=1,
            slug="test-slug",
            signal_direction="YES",
            intended_price=0.87,
            bankroll=30.0,
            intra_window_prices=[0.89, 0.91, 0.92, 0.90],  # never dips to 0.87
        )
        assert result.fill_realism_grade == FILL_GRADE_PROVISIONAL_MULTI
        assert result.filled is False
        assert result.fill_evaluable is True

    def test_observed_path_requires_explicit_source(self, maker):
        """OBSERVED_PATH grade only when fill_realism_source explicitly set."""
        result = maker.evaluate(
            window_open_ts=1,
            slug="test-slug",
            signal_direction="YES",
            intended_price=0.87,
            bankroll=30.0,
            intra_window_prices=[0.89, 0.86],
            fill_realism_source=FILL_GRADE_OBSERVED,
        )
        assert result.fill_realism_grade == FILL_GRADE_OBSERVED
        assert result.fill_evaluable is True

    def test_open_price_equals_intended_does_not_auto_fill_without_path(self, maker):
        """
        Regression: no path → no fill, regardless of price coincidence.
        """
        result = maker.evaluate(
            window_open_ts=1,
            slug="test-slug",
            signal_direction="YES",
            intended_price=0.87,
            bankroll=30.0,
            intra_window_prices=None,   # explicitly no path
        )
        assert result.filled is False, (
            "Degenerate fill: no path should NOT auto-fill"
        )
        assert result.fill_realism_grade == FILL_GRADE_PROVISIONAL
        assert result.fill_evaluable is False

    def test_single_point_does_not_fill_even_if_below_limit(self, maker):
        """
        Single-point path: even if the price is below the limit, fill is blocked.
        Same-tick coincidence is not sufficient evidence.
        """
        result = maker.evaluate(
            window_open_ts=1,
            slug="test-slug",
            signal_direction="YES",
            intended_price=0.87,
            bankroll=30.0,
            intra_window_prices=[0.85],  # 1 point, below limit — but NOT evaluable
        )
        assert result.filled is False
        assert result.fill_realism_grade == FILL_GRADE_PROVISIONAL_SINGLE
        assert result.fill_evaluable is False

    def test_single_point_grade_distinct_from_no_path(self, maker):
        """PROVISIONAL_SINGLE_POINT and PROVISIONAL_NO_PATH are different grades."""
        no_path = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0, intra_window_prices=None,
        )
        one_point = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0, intra_window_prices=[0.85],
        )
        assert no_path.fill_realism_grade == FILL_GRADE_PROVISIONAL
        assert one_point.fill_realism_grade == FILL_GRADE_PROVISIONAL_SINGLE
        assert no_path.fill_realism_grade != one_point.fill_realism_grade

    def test_no_signal_grade_is_na(self, maker):
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="NONE",
            intended_price=None, bankroll=30.0,
        )
        assert result.fill_realism_grade == FILL_GRADE_NA

    def test_ineligible_bucket_grade_is_na(self, maker):
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.50,   # outside all buckets
            bankroll=30.0,
        )
        assert result.fill_realism_grade == FILL_GRADE_NA
        assert result.quote_bucket == "INELIGIBLE"

    def test_bankroll_fraction_uses_30_not_1000(self, maker):
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
        )
        expected = (0.87 * 5) / 30.0
        assert result.bankroll_fraction == pytest.approx(expected)
        # Not 4.35/1000.0 = 0.00435
        assert result.bankroll_fraction != pytest.approx(0.00435)


class TestMakerSettlement:
    def test_settle_win_updates_pnl(self, maker):
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=[0.86, 0.85],  # 2 points, fills → evaluable
        )
        result = maker.settle(result, "YES")
        assert result.outcome_correct is True
        assert result.gross_pnl == pytest.approx((1.0 - 0.87) * 5)
        assert result.net_pnl == result.gross_pnl   # maker fee = 0

    def test_settle_loss_updates_pnl(self, maker):
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=[0.86, 0.85],  # 2 points, fills → evaluable
        )
        result = maker.settle(result, "NO")
        assert result.outcome_correct is False
        assert result.gross_pnl == pytest.approx(-0.87 * 5)

    def test_settle_provisional_unfilled_has_no_pnl(self, maker):
        result = maker.evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=None,  # provisional, not filled
        )
        result = maker.settle(result, "YES")
        assert result.outcome_correct is None
        assert result.net_pnl is None
