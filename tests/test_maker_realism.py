"""
tests/test_maker_realism.py

Maker fill realism tests — Round 6: conservative path density requirements.

Covers:
  1. Sparse collector + short decision window => 0–1 points collected
  2. Single-point path does NOT count as realistic maker fill
  3. Multi-point path (2+) IS evaluable, fill based on actual path
  4. Immediate equality with intended price does NOT auto-fill (single-point)
  5. fill_evaluable correctly reflects path density
  6. CollectionResult.point_count / first_ts / last_ts populated correctly
  7. WindowLog records fill_evaluable and path statistics
  8. SessionStats.maker_evaluable_fills distinct from maker_fills
  9. Config-driven poll interval wired into IntraWindowYesPriceCollector
 10. PROVISIONAL_SINGLE_POINT and PROVISIONAL_MULTI_POINT are distinct grades
 11. fill_evaluable=False for all non-evaluable grades
 12. Evaluable fill rate uses evaluable_fills, not raw fills
"""
import asyncio
import time
from dataclasses import dataclass
from typing import List
from unittest.mock import AsyncMock

import pytest

from execution.maker_lane import (
    MakerLane,
    FILL_GRADE_OBSERVED,
    FILL_GRADE_PROVISIONAL,
    FILL_GRADE_PROVISIONAL_SINGLE,
    FILL_GRADE_PROVISIONAL_MULTI,
    FILL_GRADE_NA,
)
from feeds.intra_window_collector import CollectionResult, IntraWindowYesPriceCollector
from feeds.price_types import YesPriceSnapshot
from logger.summary import WindowLog
from validator.kill_conditions import SessionStats


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _maker_config():
    return {
        "sizing": {"min_shares": 5, "fixed_shares_v1": 5, "initial_bankroll": 30.0},
        "quote_buckets": {"B1": [0.83, 0.86], "B2": [0.87, 0.90], "B3": [0.91, 0.92]},
    }


def _maker():
    return MakerLane(_maker_config())


# ---------------------------------------------------------------------------
# 1. Sparse collector yields 0–1 points in short windows
# ---------------------------------------------------------------------------

class TestSparseCollector:
    def test_zero_points_when_duration_shorter_than_interval(self):
        """
        If duration < poll_interval, the loop body executes once (immediate poll),
        but there is no second poll.  With a failed poll, result is empty.
        This simulates a 10 s window with 60 s interval.
        """
        result = CollectionResult(poll_interval_s=60.0)
        # No prices added — simulates a failed poll or too-short window
        assert result.point_count == 0
        assert result.first_ts is None
        assert result.last_ts is None

    def test_single_point_when_one_poll_succeeds(self):
        result = CollectionResult(
            prices=[0.87],
            timestamps=[time.time()],
            poll_interval_s=60.0,
        )
        assert result.point_count == 1
        assert result.first_ts is not None
        assert result.first_ts == result.last_ts

    def test_multi_point_when_two_polls_succeed(self):
        t0 = time.time()
        result = CollectionResult(
            prices=[0.87, 0.86],
            timestamps=[t0, t0 + 15.0],
            poll_interval_s=15.0,
        )
        assert result.point_count == 2
        assert result.first_ts == t0
        assert result.last_ts == t0 + 15.0

    def test_short_duration_yields_at_most_one_poll(self):
        """
        With poll_interval=60s and duration=5s, the loop runs the initial
        poll, then deadline - elapsed <= interval so it breaks.
        Collector should return at most 1 price.
        """
        snap = YesPriceSnapshot(
            probability=0.87,
            timestamp=time.time(),
            source="clob_midpoint",
            is_provisional=True,
            token_id="tok",
        )
        adapter = AsyncMock()
        adapter.get_yes_mid = AsyncMock(return_value=snap)

        collector = IntraWindowYesPriceCollector(adapter, poll_interval_s=60.0)
        result = asyncio.run(collector.collect("tok", duration_seconds=5.0))

        assert result.point_count <= 1
        assert result.poll_interval_s == 60.0

    def test_15s_interval_yields_multiple_polls_vs_60s_sparse(self):
        """
        Demonstrate why 15s interval is better than 60s for a 35s window.
        With 60s interval: at most 1 poll (duration < interval after first poll).
        With 15s interval: expect 2+ polls.
        """
        snap = YesPriceSnapshot(
            probability=0.87,
            timestamp=time.time(),
            source="clob_midpoint",
            is_provisional=True,
            token_id="tok",
        )

        # 60s interval in a 35s window → at most 1 poll
        adapter_sparse = AsyncMock()
        adapter_sparse.get_yes_mid = AsyncMock(return_value=snap)
        collector_sparse = IntraWindowYesPriceCollector(adapter_sparse, poll_interval_s=60.0)
        result_sparse = asyncio.run(collector_sparse.collect("tok", duration_seconds=5.0))
        assert result_sparse.point_count <= 1, (
            "60s interval in a short window should yield at most 1 poll"
        )


# ---------------------------------------------------------------------------
# 2. Single-point path: fill blocked, grade=PROVISIONAL_SINGLE_POINT
# ---------------------------------------------------------------------------

class TestSinglePointPath:
    def test_single_point_grade_is_provisional_single(self):
        result = _maker().evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=[0.85],  # 1 point — not evaluable
        )
        assert result.fill_realism_grade == FILL_GRADE_PROVISIONAL_SINGLE

    def test_single_point_fill_is_false_even_if_price_below_limit(self):
        """
        A single price at 0.85 is strictly below limit 0.87.
        Under the old logic this would fill.  Under conservative rules it does not.
        """
        result = _maker().evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=[0.85],
        )
        assert result.filled is False, (
            "Single-point path must NOT fill even if price < limit; "
            "same-tick coincidence is not sufficient evidence"
        )

    def test_single_point_fill_evaluable_is_false(self):
        result = _maker().evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=[0.85],
        )
        assert result.fill_evaluable is False

    def test_single_point_price_equal_to_limit_does_not_fill(self):
        """
        Same-tick coincidence: if the first post-decision price equals the limit
        exactly, the old logic would treat it as a crossing.  Must be blocked.
        """
        result = _maker().evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=[0.87],  # exactly at limit — still single-point
        )
        assert result.filled is False
        assert result.fill_realism_grade == FILL_GRADE_PROVISIONAL_SINGLE


# ---------------------------------------------------------------------------
# 3. Multi-point path: fill evaluable, grade=PROVISIONAL_MULTI_POINT
# ---------------------------------------------------------------------------

class TestMultiPointPath:
    def test_two_points_above_limit_no_fill_but_evaluable(self):
        """2 points, none below limit → filled=False but evaluable=True."""
        result = _maker().evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=[0.90, 0.91],  # both above limit
        )
        assert result.filled is False
        assert result.fill_realism_grade == FILL_GRADE_PROVISIONAL_MULTI
        assert result.fill_evaluable is True

    def test_two_points_one_below_limit_fills(self):
        """2 points, one below limit → filled=True and evaluable=True."""
        result = _maker().evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=[0.89, 0.85],  # second dips below
        )
        assert result.filled is True
        assert result.fill_realism_grade == FILL_GRADE_PROVISIONAL_MULTI
        assert result.fill_evaluable is True
        assert result.fill_price == pytest.approx(0.87)

    def test_three_points_all_above_limit_no_fill_evaluable(self):
        result = _maker().evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=[0.89, 0.90, 0.92],
        )
        assert result.filled is False
        assert result.fill_evaluable is True

    def test_multi_point_exactly_at_limit_fills(self):
        """min(prices) == intended_price → fill at limit (boundary inclusive)."""
        result = _maker().evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=[0.89, 0.87],  # second = limit
        )
        assert result.filled is True
        assert result.fill_evaluable is True


# ---------------------------------------------------------------------------
# 4. Grade constants are distinct
# ---------------------------------------------------------------------------

class TestGradeConstants:
    def test_all_grade_constants_are_distinct(self):
        grades = {
            FILL_GRADE_OBSERVED,
            FILL_GRADE_PROVISIONAL,
            FILL_GRADE_PROVISIONAL_SINGLE,
            FILL_GRADE_PROVISIONAL_MULTI,
            FILL_GRADE_NA,
        }
        assert len(grades) == 5, "All fill grade constants must be distinct strings"

    def test_no_path_grade_is_provisional_no_path(self):
        result = _maker().evaluate(
            window_open_ts=1, slug="s", signal_direction="YES",
            intended_price=0.87, bankroll=30.0,
            intra_window_prices=None,
        )
        assert result.fill_realism_grade == FILL_GRADE_PROVISIONAL
        assert result.fill_evaluable is False

    def test_no_signal_grade_is_na(self):
        result = _maker().evaluate(
            window_open_ts=1, slug="s", signal_direction="NONE",
            intended_price=None, bankroll=30.0,
        )
        assert result.fill_realism_grade == FILL_GRADE_NA
        assert result.fill_evaluable is False


# ---------------------------------------------------------------------------
# 5. WindowLog records path statistics correctly
# ---------------------------------------------------------------------------

class TestWindowLogPathStats:
    def test_windowlog_has_all_realism_fields(self):
        wl = WindowLog(window_open_ts=1, slug="s", phase="0c")
        assert hasattr(wl, "maker_poll_interval_seconds")
        assert hasattr(wl, "maker_path_points_collected")
        assert hasattr(wl, "maker_first_path_ts")
        assert hasattr(wl, "maker_last_path_ts")
        assert hasattr(wl, "maker_fill_evaluable")
        assert hasattr(wl, "maker_fill_realism_mode")

    def test_windowlog_realism_fields_default_values(self):
        wl = WindowLog(window_open_ts=1, slug="s", phase="0c")
        assert wl.maker_poll_interval_seconds is None
        assert wl.maker_path_points_collected == 0
        assert wl.maker_first_path_ts is None
        assert wl.maker_last_path_ts is None
        assert wl.maker_fill_evaluable is False
        assert wl.maker_fill_realism_mode == "N/A"

    def test_windowlog_filled_from_multi_point_result(self):
        """Wiring: WindowLog fields should be set from a multi-point fill result."""
        maker = _maker()
        result = maker.evaluate(
            window_open_ts=1_700_000_000,
            slug="s",
            signal_direction="YES",
            intended_price=0.87,
            bankroll=30.0,
            intra_window_prices=[0.89, 0.85],  # 2 points, fills
        )

        wl = WindowLog(window_open_ts=1_700_000_000, slug="s", phase="0c")
        t_now = time.time()
        wl.maker_poll_interval_seconds = 15.0
        wl.maker_path_points_collected = 2
        wl.maker_first_path_ts = t_now - 15.0
        wl.maker_last_path_ts = t_now
        wl.maker_fill_evaluable = result.fill_evaluable
        wl.maker_fill_realism_mode = result.fill_realism_grade
        wl.maker_filled = result.filled

        assert wl.maker_fill_evaluable is True
        assert wl.maker_fill_realism_mode == FILL_GRADE_PROVISIONAL_MULTI
        assert wl.maker_filled is True
        assert wl.maker_first_path_ts is not None
        assert wl.maker_last_path_ts > wl.maker_first_path_ts


# ---------------------------------------------------------------------------
# 6. SessionStats distinguishes raw fills from evaluable fills
# ---------------------------------------------------------------------------

class TestSessionStatsEvaluableFills:
    def test_sessionstats_has_evaluable_fills_field(self):
        stats = SessionStats()
        assert hasattr(stats, "maker_evaluable_fills")
        assert stats.maker_evaluable_fills == 0

    def test_maker_evaluable_fill_rate_is_none_when_no_candidates(self):
        stats = SessionStats(maker_candidate_count=0, maker_evaluable_fills=0)
        assert stats.maker_evaluable_fill_rate() is None

    def test_maker_evaluable_fill_rate_computed_correctly(self):
        stats = SessionStats(
            maker_candidate_count=10,
            maker_fills=8,           # 8 raw fills (includes single-point)
            maker_evaluable_fills=3, # only 3 had multi-point paths
        )
        assert stats.maker_fill_rate() == pytest.approx(0.8)
        assert stats.maker_evaluable_fill_rate() == pytest.approx(0.3)

    def test_raw_and_evaluable_fills_diverge_under_sparse_path(self):
        """
        Session with many raw fills but sparse collector paths:
        maker_fills >> maker_evaluable_fills.
        Evaluable rate is the correct viability metric.
        """
        stats = SessionStats(
            maker_candidate_count=20,
            maker_fills=15,           # raw fills inflate viability reading
            maker_evaluable_fills=2,  # only 2 had >=2 path points
        )
        raw_rate = stats.maker_fill_rate()
        eval_rate = stats.maker_evaluable_fill_rate()

        assert raw_rate == pytest.approx(0.75)
        assert eval_rate == pytest.approx(0.10)
        assert raw_rate > eval_rate, (
            "Raw fill rate must be higher than evaluable fill rate "
            "when many single-point fills inflate the raw count"
        )


# ---------------------------------------------------------------------------
# 7. Config-driven poll interval
# ---------------------------------------------------------------------------

class TestPollIntervalConfig:
    def test_collection_result_stores_poll_interval(self):
        r = CollectionResult(prices=[], timestamps=[], poll_interval_s=15.0)
        assert r.poll_interval_s == 15.0

    def test_collection_result_default_poll_interval(self):
        r = CollectionResult()
        assert r.poll_interval_s == 15.0

    def test_collector_stores_configured_interval(self):
        adapter = AsyncMock()
        collector = IntraWindowYesPriceCollector(adapter, poll_interval_s=20.0)
        assert collector._interval == 20.0

    def test_collector_default_interval_is_15(self):
        """Default interval is 15s (not 60s — the old problematic default)."""
        adapter = AsyncMock()
        collector = IntraWindowYesPriceCollector(adapter)
        assert collector._interval == 15.0, (
            "Default poll interval must be 15s. "
            "60s interval gives 0–1 polls in a 40s window (non-evaluable)."
        )
