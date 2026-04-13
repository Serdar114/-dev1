"""
tests/test_no_trade_rules.py — Tests for no-trade condition evaluation.

Each condition is tested independently.
Tests verify that the condition is labelled correctly in the output list.
Tests verify that a clean state produces an empty no-trade list.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time
import pytest

from state import (
    SystemState, ChainlinkFeed, BinanceFeed,
    OrderbookSide, WindowState, MarketRecord, MarketMetadata,
)
from signals.no_trade_rules import evaluate


# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    return {
        "chainlink": {"max_age_seconds": 120},
        "binance": {"max_age_seconds": 30},
        "measurement": {
            "basis_warn_pct": 0.5,
            "pair_sum_min": 0.98,
            "pair_sum_max": 1.06,
            "max_spread": 0.05,
            "min_secs_to_expiry": 60,
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_state(window_secs_remaining=120) -> SystemState:
    """Build a SystemState that passes all no-trade rules."""
    state = SystemState()
    now = time.time()

    # Chainlink: fresh
    state.chainlink.price = 65000.0
    state.chainlink.oracle_updated_at = now - 5
    state.chainlink.fetched_at = now

    # Binance: fresh (mid close to chainlink)
    state.binance.bid = 65001.0
    state.binance.ask = 65003.0
    state.binance.updated_at = now

    # Market
    state.market = MarketRecord(
        slug="btc-up-down-5m-1700000000",
        condition_id="0xabc",
        up_token_id="up_id",
        down_token_id="dn_id",
        window_start=1700000000,
        window_end=1700000300,
    )

    # Window (enough time left): end is relative to NOW so secs_to_expiry is reliable
    ws = int(now // 300) * 300
    state.window = WindowState(start=ws, end=int(now) + window_secs_remaining)

    # Metadata: complete, canonical fee
    state.metadata = MarketMetadata(
        condition_id="0xabc",
        tick_size=0.001,
        tick_size_provenance="canonical",
        min_order_size=5.0,
        min_order_size_provenance="canonical",
        taker_fee_rate=0.02,
        fee_provenance="canonical_market_object",
    )

    # Orderbook: both sides with tight spread, good pair sum
    up = OrderbookSide(outcome="Up", token_id="up_id")
    up.snapshot_received = True
    up.updated_at = now
    up.apply_snapshot(
        bids=[{"price": "0.49", "size": "200"}],
        asks=[{"price": "0.50", "size": "300"}],
    )
    state.up_book = up

    dn = OrderbookSide(outcome="Down", token_id="dn_id")
    dn.snapshot_received = True
    dn.updated_at = now
    dn.apply_snapshot(
        bids=[{"price": "0.49", "size": "200"}],
        asks=[{"price": "0.50", "size": "300"}],
    )
    state.down_book = dn

    return state


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_clean_state_no_reasons(config):
    state = _clean_state()
    reasons = evaluate(state, config)
    assert reasons == [], f"Expected no reasons, got: {reasons}"


# ---------------------------------------------------------------------------
# Chainlink conditions
# ---------------------------------------------------------------------------

def test_chainlink_missing(config):
    state = _clean_state()
    state.chainlink.price = None
    state.chainlink.oracle_updated_at = None
    reasons = evaluate(state, config)
    assert any("chainlink_missing" in r for r in reasons)


def test_chainlink_stale(config):
    state = _clean_state()
    state.chainlink.oracle_updated_at = time.time() - 200  # 200s > 120s max
    reasons = evaluate(state, config)
    assert any("chainlink_stale" in r for r in reasons)


def test_chainlink_fresh_passes(config):
    state = _clean_state()
    state.chainlink.oracle_updated_at = time.time() - 10  # 10s < 120s
    reasons = evaluate(state, config)
    assert not any("chainlink" in r for r in reasons)


# ---------------------------------------------------------------------------
# Metadata conditions
# ---------------------------------------------------------------------------

def test_metadata_missing(config):
    state = _clean_state()
    state.metadata = None
    reasons = evaluate(state, config)
    assert any("metadata_missing" in r for r in reasons)


def test_tick_size_missing(config):
    state = _clean_state()
    state.metadata.tick_size = None
    reasons = evaluate(state, config)
    assert any("tick_size_missing" in r for r in reasons)


def test_min_order_size_missing(config):
    state = _clean_state()
    state.metadata.min_order_size = None
    reasons = evaluate(state, config)
    assert any("min_order_size_missing" in r for r in reasons)


def test_fee_rate_missing(config):
    state = _clean_state()
    state.metadata.taker_fee_rate = None
    state.metadata.fee_provenance = "missing"
    reasons = evaluate(state, config)
    assert any("fee_rate_missing" in r for r in reasons)


# ---------------------------------------------------------------------------
# Orderbook conditions
# ---------------------------------------------------------------------------

def test_up_book_no_snapshot(config):
    state = _clean_state()
    state.up_book.snapshot_received = False
    state.up_book.asks = []
    reasons = evaluate(state, config)
    assert any("up_book" in r for r in reasons)


def test_down_book_empty_asks(config):
    state = _clean_state()
    state.down_book.asks = []
    reasons = evaluate(state, config)
    assert any("down_book" in r for r in reasons)


# ---------------------------------------------------------------------------
# Basis condition
# ---------------------------------------------------------------------------

def test_basis_unstable_flagged(config):
    state = _clean_state()
    # Binance 1% above Chainlink => basis = 1% > 0.5% threshold
    state.binance.bid = 65650.0
    state.binance.ask = 65650.0
    reasons = evaluate(state, config)
    assert any("basis_unstable" in r for r in reasons)


def test_basis_within_threshold_passes(config):
    state = _clean_state()
    # Basis 0.1% — within 0.5% threshold
    state.binance.bid = 65065.0
    state.binance.ask = 65065.0
    reasons = evaluate(state, config)
    assert not any("basis" in r for r in reasons)


# ---------------------------------------------------------------------------
# Pair sum condition
# ---------------------------------------------------------------------------

def test_pair_sum_too_high(config):
    state = _clean_state()
    # Force asks to sum > 1.06
    state.up_book.asks = [(0.60, 100)]
    state.down_book.asks = [(0.50, 100)]
    reasons = evaluate(state, config)
    assert any("pair_sum_bad" in r for r in reasons)


def test_pair_sum_too_low(config):
    state = _clean_state()
    state.up_book.asks = [(0.40, 100)]
    state.down_book.asks = [(0.40, 100)]
    reasons = evaluate(state, config)
    assert any("pair_sum_bad" in r for r in reasons)


# ---------------------------------------------------------------------------
# Window timing
# ---------------------------------------------------------------------------

def test_window_expiring_soon(config):
    state = _clean_state(window_secs_remaining=30)  # 30s < 60s min
    reasons = evaluate(state, config)
    assert any("window_expiring_soon" in r for r in reasons)


def test_window_time_ok(config):
    state = _clean_state(window_secs_remaining=120)  # 120s >= 60s min
    reasons = evaluate(state, config)
    assert not any("window_expiring" in r for r in reasons)
