"""
tests/conftest.py — Shared fixtures for research bot tests.
"""
import pytest

# ---------------------------------------------------------------------------
# Minimal config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_config():
    """Minimal config dict matching production structure."""
    return {
        "phase": "0c",
        "sizing": {
            "min_shares": 5,
            "fixed_shares_v1": 5,
            "initial_bankroll": 30.0,
        },
        "fees": {
            "maker_fee_rate": 0.0,
            "taker_fee_C": 0.02,
            "assumed_slippage_bps": 0,
        },
        "signal": {
            "endcycle_entry_cutoff_seconds": 45,
            "feed_freshness_threshold_seconds": 8.0,
            "basis_mismatch_flag_threshold_bps": 30.0,
            "min_spread_quality_bps": 5.0,
            "extreme_zone_low": 0.10,
            "extreme_zone_high": 0.90,
            "momentum_persistence_candles": 2,
        },
        "quote_buckets": {
            "B1": [0.83, 0.86],
            "B2": [0.87, 0.90],
            "B3": [0.91, 0.92],
        },
        "daily_caps": {
            "max_candidates_per_day": 50,
            "max_entries_per_day": 5,
            "one_position_at_a_time": True,
        },
        "logging": {
            "level": "WARNING",
            "log_dir": "/tmp/bot_test_logs",
            "summary_interval_windows": 9999,
        },
    }


@pytest.fixture
def all_gates_open_feed_window():
    """
    A FeedWindow where all data is available and all gates should pass.
    Uses explicit current_yes_mid in probability space (0-1).
    """
    from sigeng.engine import FeedWindow
    return FeedWindow(
        window_open_ts=1_700_000_000,
        slug="btc-updown-5m-1700000000",
        open_fast_price=50_000.0,
        latest_fast_price=50_200.0,
        open_chainlink_price=50_005.0,
        latest_chainlink_price=50_100.0,
        current_yes_mid=0.87,       # YES probability: in B2 range, well within bounds
        yes_bid=0.86,
        yes_ask=0.88,
        fast_gap_seconds=2.0,
        chainlink_gap_seconds=3.0,
        fast_feed_stale=False,
        chainlink_feed_stale=False,
        seconds_to_window_close=120.0,
        candles_same_direction=3,
        yes_book_available=True,
        candles_available=True,
    )
