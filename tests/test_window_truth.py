"""
tests/test_window_truth.py — Tests for window clock and resolution logic.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time
import pytest
from unittest.mock import MagicMock

from truth.window_clock import (
    current_window_start,
    current_window_end,
    window_bounds,
    secs_to_expiry,
    prev_window_start,
    is_same_window,
    WINDOW_SECONDS,
)
from truth.resolution_truth import resolve_window, Resolution


# ---------------------------------------------------------------------------
# Window clock
# ---------------------------------------------------------------------------

def test_current_window_start_is_multiple_of_300():
    ts = current_window_start()
    assert ts % 300 == 0


def test_current_window_end_is_start_plus_300():
    start = current_window_start()
    end = current_window_end()
    assert end == start + 300


def test_window_bounds_consistent():
    s, e = window_bounds()
    assert e - s == 300
    assert s % 300 == 0


def test_secs_to_expiry_is_positive_and_lte_300():
    s = secs_to_expiry()
    assert 0 <= s <= 300


def test_prev_window_start_is_300_before_current():
    cur = current_window_start()
    prev = prev_window_start()
    assert cur - prev == 300


def test_is_same_window_true_for_same_window():
    ts = current_window_start()
    assert is_same_window(ts, ts + 10)
    assert is_same_window(ts, ts + 299)


def test_is_same_window_false_across_boundary():
    ts = current_window_start()
    assert not is_same_window(ts, ts + 300)
    assert not is_same_window(ts, ts - 1)


def test_window_start_for_specific_time():
    # 16:37:23 UTC => 16:35:00 UTC
    import datetime
    dt = datetime.datetime(2024, 1, 15, 16, 37, 23)
    ts = int(dt.timestamp())  # ignores TZ but close enough for logic test
    ws = current_window_start(ts)
    assert ws % 300 == 0
    assert ws <= ts
    assert ts - ws < 300


# ---------------------------------------------------------------------------
# Resolution truth
# ---------------------------------------------------------------------------

def _make_chainlink_client(price, oracle_age_seconds):
    """Build a minimal mock chainlink client."""
    client = MagicMock()
    snap = MagicMock()
    snap.price = price
    snap.oracle_updated_at = time.time() - oracle_age_seconds if oracle_age_seconds is not None else None

    def age_seconds():
        if snap.oracle_updated_at is None:
            return None
        return time.time() - snap.oracle_updated_at

    snap.age_seconds = age_seconds
    snap.is_fresh = lambda max_age: age_seconds() is not None and age_seconds() <= max_age
    client.snapshot.return_value = snap
    return client


def test_resolution_up_when_price_rises():
    cl = _make_chainlink_client(price=65000.0, oracle_age_seconds=10)
    res = resolve_window(
        window_start=1700000000,
        price_at_start=64000.0,
        chainlink=cl,
        max_oracle_age=120,
    )
    assert res.status == "resolved_canonical"
    assert res.outcome == "Up"


def test_resolution_down_when_price_falls():
    cl = _make_chainlink_client(price=63000.0, oracle_age_seconds=10)
    res = resolve_window(
        window_start=1700000000,
        price_at_start=64000.0,
        chainlink=cl,
        max_oracle_age=120,
    )
    assert res.status == "resolved_canonical"
    assert res.outcome == "Down"


def test_resolution_stale_oracle_blocks():
    cl = _make_chainlink_client(price=65000.0, oracle_age_seconds=200)
    res = resolve_window(
        window_start=1700000000,
        price_at_start=64000.0,
        chainlink=cl,
        max_oracle_age=120,
    )
    assert res.status == "stale_oracle"
    assert res.outcome is None


def test_resolution_missing_oracle_blocks():
    cl = _make_chainlink_client(price=None, oracle_age_seconds=None)
    res = resolve_window(
        window_start=1700000000,
        price_at_start=64000.0,
        chainlink=cl,
        max_oracle_age=120,
    )
    assert res.status == "missing_oracle"
    assert res.outcome is None


def test_resolution_missing_start_price_blocks():
    cl = _make_chainlink_client(price=65000.0, oracle_age_seconds=10)
    res = resolve_window(
        window_start=1700000000,
        price_at_start=None,
        chainlink=cl,
        max_oracle_age=120,
    )
    assert res.status == "missing_start"
    assert res.outcome is None


def test_resolution_equal_price_undefined():
    cl = _make_chainlink_client(price=64000.0, oracle_age_seconds=10)
    res = resolve_window(
        window_start=1700000000,
        price_at_start=64000.0,
        chainlink=cl,
        max_oracle_age=120,
    )
    assert res.status == "equal_price"
    assert res.outcome is None
