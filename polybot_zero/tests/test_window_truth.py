"""
Tests for truth.window_clock and truth.resolution_truth.
All tests run offline — no network required.
"""

import pytest
import time
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from truth.window_clock import WindowClock
from truth.resolution_truth import ResolutionTruthTracker
from truth.freshness import check_freshness, is_fresh, age_secs
from loggingx.schemas import ChainlinkPrice, FreshnessState, ResolutionOutcome


# ─── Freshness tests ──────────────────────────────────────────

class TestFreshness:
    def test_fresh(self):
        ts = time.time() - 5  # 5 seconds ago
        result = check_freshness(ts, max_age_secs=30)
        assert result == FreshnessState.FRESH

    def test_stale(self):
        ts = time.time() - 60  # 60 seconds ago
        result = check_freshness(ts, max_age_secs=30)
        assert result == FreshnessState.STALE

    def test_missing(self):
        result = check_freshness(None, max_age_secs=30)
        assert result == FreshnessState.MISSING

    def test_is_fresh_true(self):
        ts = time.time() - 1
        assert is_fresh(ts, max_age_secs=10) is True

    def test_is_fresh_false_stale(self):
        ts = time.time() - 100
        assert is_fresh(ts, max_age_secs=10) is False

    def test_is_fresh_false_none(self):
        assert is_fresh(None, max_age_secs=10) is False

    def test_age_secs(self):
        ts = time.time() - 10
        age = age_secs(ts)
        assert 9.9 <= age <= 10.5

    def test_age_secs_none(self):
        assert age_secs(None) is None


# ─── WindowClock tests ────────────────────────────────────────

class TestWindowClock:
    def _make_clock(self, offset_start: float = -10, duration: float = 300):
        """
        offset_start: seconds relative to now (negative = started in past)
        """
        now = time.time()
        start = now + offset_start
        end   = start + duration
        return WindowClock("test_cid", start, end)

    def test_is_live(self):
        clock = self._make_clock(offset_start=-10)
        assert clock.is_live() is True

    def test_is_before_open(self):
        clock = self._make_clock(offset_start=100)  # starts in future
        assert clock.is_before_open() is True
        assert clock.is_live() is False

    def test_is_after_close(self):
        clock = self._make_clock(offset_start=-400)  # started 400s ago, 300s duration
        assert clock.is_after_close() is True
        assert clock.is_live() is False

    def test_secs_to_expiry_positive(self):
        clock = self._make_clock(offset_start=-10)  # 10s in, 290s left
        ste = clock.secs_to_expiry()
        assert 285 <= ste <= 295

    def test_secs_to_expiry_negative_after_close(self):
        clock = self._make_clock(offset_start=-400)
        assert clock.secs_to_expiry() < 0

    def test_should_fire_open_once(self):
        clock = self._make_clock(offset_start=-5)  # already past open
        assert clock.should_fire_open() is True
        assert clock.should_fire_open() is False  # should NOT fire again

    def test_should_fire_close_once(self):
        # Create a clock that is within close tolerance
        now = time.time()
        clock = WindowClock("cid", now - 280, now + 20, close_capture_tolerance_secs=30)
        # 20s to close = within 30s tolerance
        assert clock.should_fire_close() is True
        assert clock.should_fire_close() is False  # should NOT fire again

    def test_five_minute_window(self):
        clock = self._make_clock(offset_start=-10, duration=300)
        assert clock.is_five_minute_window() is True

    def test_not_five_minute_window(self):
        clock = self._make_clock(offset_start=-10, duration=600)  # 10 minutes
        assert clock.is_five_minute_window() is False

    def test_invalid_window_raises(self):
        now = time.time()
        with pytest.raises(ValueError):
            WindowClock("cid", now + 100, now)  # end before start


# ─── ResolutionTruthTracker tests ─────────────────────────────

def _make_fresh_chainlink(price: float) -> ChainlinkPrice:
    now = time.time()
    return ChainlinkPrice(
        price_usd=price,
        round_id=100,
        updated_at=now - 5,   # 5s ago = fresh within 45s threshold
        fetched_at=now,
        freshness=FreshnessState.FRESH,
    )


def _make_stale_chainlink(price: float) -> ChainlinkPrice:
    now = time.time()
    return ChainlinkPrice(
        price_usd=price,
        round_id=100,
        updated_at=now - 60,   # 60s ago = stale within 45s threshold
        fetched_at=now,
        freshness=FreshnessState.STALE,
    )


class TestResolutionTruthTracker:
    def _make_tracker(self):
        now = time.time()
        return ResolutionTruthTracker(
            condition_id="test_cid",
            window_start_ts=now - 300,
            window_end_ts=now,
        )

    def test_up_outcome(self):
        tracker = self._make_tracker()
        tracker.capture_open(_make_fresh_chainlink(50000.0))
        tracker.capture_close(_make_fresh_chainlink(50100.0))  # higher = UP
        assert tracker.truth.outcome == ResolutionOutcome.UP
        assert tracker.is_resolved() is True

    def test_down_outcome(self):
        tracker = self._make_tracker()
        tracker.capture_open(_make_fresh_chainlink(50000.0))
        tracker.capture_close(_make_fresh_chainlink(49900.0))  # lower = DOWN
        assert tracker.truth.outcome == ResolutionOutcome.DOWN

    def test_tie_goes_up(self):
        tracker = self._make_tracker()
        tracker.capture_open(_make_fresh_chainlink(50000.0))
        tracker.capture_close(_make_fresh_chainlink(50000.0))  # equal = UP (>= rule)
        assert tracker.truth.outcome == ResolutionOutcome.UP

    def test_stale_open_gives_unresolved(self):
        tracker = self._make_tracker()
        tracker.capture_open(_make_stale_chainlink(50000.0))   # stale!
        tracker.capture_close(_make_fresh_chainlink(50100.0))
        assert tracker.truth.outcome == ResolutionOutcome.UNRESOLVED
        assert tracker.truth.chainlink_open_ok is False

    def test_stale_close_gives_unresolved(self):
        tracker = self._make_tracker()
        tracker.capture_open(_make_fresh_chainlink(50000.0))
        tracker.capture_close(_make_stale_chainlink(50100.0))  # stale!
        assert tracker.truth.outcome == ResolutionOutcome.UNRESOLVED
        assert tracker.truth.chainlink_close_ok is False

    def test_missing_open_gives_unresolved(self):
        tracker = self._make_tracker()
        tracker.capture_open(None)
        tracker.capture_close(_make_fresh_chainlink(50100.0))
        assert tracker.truth.outcome == ResolutionOutcome.UNRESOLVED
        assert tracker.truth.chainlink_open_ok is False

    def test_missing_close_gives_unresolved(self):
        tracker = self._make_tracker()
        tracker.capture_open(_make_fresh_chainlink(50000.0))
        tracker.capture_close(None)
        assert tracker.truth.outcome == ResolutionOutcome.UNRESOLVED
        assert tracker.truth.chainlink_close_ok is False

    def test_open_price_recorded(self):
        tracker = self._make_tracker()
        tracker.capture_open(_make_fresh_chainlink(50000.0))
        assert tracker.truth.chainlink_open == pytest.approx(50000.0)

    def test_close_price_recorded(self):
        tracker = self._make_tracker()
        tracker.capture_open(_make_fresh_chainlink(50000.0))
        tracker.capture_close(_make_fresh_chainlink(50100.0))
        assert tracker.truth.chainlink_close == pytest.approx(50100.0)
