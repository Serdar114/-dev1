"""
tests/test_feature_builder.py — Tests for feature extraction.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time
import pytest

from state import SystemState, ChainlinkFeed, BinanceFeed, OrderbookSide, WindowState
from signals.feature_builder import build


def _make_state(
    cl_price=65000.0,
    cl_age=5.0,
    bn_bid=65010.0,
    bn_ask=65012.0,
    up_asks=None,
    up_bids=None,
    dn_asks=None,
    dn_bids=None,
    window_start=1700000000,
):
    state = SystemState()
    now = time.time()

    state.chainlink.price = cl_price
    state.chainlink.oracle_updated_at = now - cl_age if cl_age is not None else None
    state.chainlink.fetched_at = now

    state.binance.bid = bn_bid
    state.binance.ask = bn_ask
    state.binance.updated_at = now

    state.window = WindowState(start=window_start, end=window_start + 300)

    up = OrderbookSide(outcome="Up", token_id="up_tok")
    up.snapshot_received = True
    up.updated_at = now
    if up_asks is None:
        up_asks = [{"price": "0.51", "size": "300"}]
    if up_bids is None:
        up_bids = [{"price": "0.49", "size": "200"}]
    up.apply_snapshot(up_bids, up_asks)
    state.up_book = up

    dn = OrderbookSide(outcome="Down", token_id="dn_tok")
    dn.snapshot_received = True
    dn.updated_at = now
    if dn_asks is None:
        dn_asks = [{"price": "0.50", "size": "250"}]
    if dn_bids is None:
        dn_bids = [{"price": "0.48", "size": "150"}]
    dn.apply_snapshot(dn_bids, dn_asks)
    state.down_book = dn

    return state


def test_features_basic():
    state = _make_state()
    features = build(state)
    assert features.chainlink_price == 65000.0
    assert features.up_best_ask == pytest.approx(0.51)
    assert features.dn_best_ask == pytest.approx(0.50)
    assert features.pair_sum_ask == pytest.approx(1.01)


def test_basis_calculation():
    state = _make_state(cl_price=65000.0, bn_bid=65010.0, bn_ask=65010.0)
    features = build(state)
    # bn_mid = 65010, cl = 65000, basis = 10, pct = 10/65000 * 100 = 0.01538%
    assert features.basis_abs == pytest.approx(10.0)
    assert features.basis_pct is not None
    assert abs(features.basis_pct - 0.01538) < 0.001


def test_basis_none_when_chainlink_missing():
    state = _make_state(cl_price=None)
    features = build(state)
    assert features.basis_abs is None
    assert features.basis_pct is None


def test_features_missing_orderbook():
    state = _make_state(up_asks=[], up_bids=[], dn_asks=[], dn_bids=[])
    features = build(state)
    assert features.up_best_ask is None
    assert features.dn_best_ask is None
    assert features.pair_sum_ask is None


def test_spread_calculation():
    state = _make_state(
        up_asks=[{"price": "0.55", "size": "100"}],
        up_bids=[{"price": "0.45", "size": "100"}],
    )
    features = build(state)
    assert features.up_spread == pytest.approx(0.10, abs=0.001)


def test_window_id_in_features():
    state = _make_state(window_start=1700001200)
    features = build(state)
    assert features.window_id == 1700001200
