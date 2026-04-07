"""
Tests for signals.feature_builder — feature assembly correctness.
All offline, no network.
"""

import pytest
import time
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.feature_builder import FeatureBuilder
from loggingx.schemas import (
    ChainlinkPrice, BinancePrice, OrderBookSnapshot, PriceLevel,
    MarketMetadata, FreshnessState,
)


def _fresh_chainlink(price: float) -> ChainlinkPrice:
    now = time.time()
    return ChainlinkPrice(price_usd=price, round_id=1, updated_at=now-5, fetched_at=now, freshness=FreshnessState.FRESH)


def _fresh_binance(bid: float, ask: float) -> BinancePrice:
    return BinancePrice(bid=bid, ask=ask, fetched_at=time.time()-1, freshness=FreshnessState.FRESH)


def _book(token_id: str, bid: float, ask: float) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        bids=[PriceLevel(price=bid, size=100.0)],
        asks=[PriceLevel(price=ask, size=100.0)],
        updated_at=time.time(),
    )


def _meta(fee_rate: float = 0.02) -> MarketMetadata:
    return MarketMetadata(
        condition_id="test_cid",
        fees_enabled=True,
        fee_rate=fee_rate,
        fee_source="api:test",
        tick_size=0.01,
        min_order_size=1.0,
        active=True,
        closed=False,
    )


def _base_fv(builder: FeatureBuilder, **overrides):
    now = time.time()
    defaults = dict(
        condition_id="test_cid",
        up_token_id="up_tok",
        down_token_id="dn_tok",
        window_start_ts=now - 150,
        window_end_ts=now + 150,
        chainlink=_fresh_chainlink(50000.0),
        chainlink_open=49900.0,
        binance=_fresh_binance(50010.0, 50020.0),
        up_book=_book("up_tok", 0.48, 0.52),
        down_book=_book("dn_tok", 0.46, 0.50),
        metadata=_meta(),
    )
    defaults.update(overrides)
    return builder.build(**defaults)


class TestFeatureBuilderFull:
    def setup_method(self):
        self.builder = FeatureBuilder()

    def test_is_tradeable_when_all_present(self):
        fv = _base_fv(self.builder)
        assert fv.is_tradeable is True

    def test_chainlink_now_set(self):
        fv = _base_fv(self.builder)
        assert fv.chainlink_now == pytest.approx(50000.0)

    def test_chainlink_open_set(self):
        fv = _base_fv(self.builder)
        assert fv.chainlink_open == pytest.approx(49900.0)

    def test_chainlink_delta_bps(self):
        # (50000 - 49900) / 49900 * 10000 = 20.04...
        fv = _base_fv(self.builder)
        expected = (50000.0 - 49900.0) / 49900.0 * 10000.0
        assert fv.chainlink_delta_bps == pytest.approx(expected, rel=1e-4)

    def test_pair_sum(self):
        fv = _base_fv(self.builder)
        # up_best_ask=0.52, down_best_ask=0.50
        assert fv.pair_sum_best_ask == pytest.approx(1.02, rel=1e-6)

    def test_fee_fields_populated(self):
        fv = _base_fv(self.builder)
        assert fv.fee_rate == pytest.approx(0.02)
        assert fv.fee_source == "api:test"

    def test_secs_to_expiry_positive(self):
        fv = _base_fv(self.builder)
        assert fv.secs_to_expiry > 0

    def test_binance_fields_populated(self):
        fv = _base_fv(self.builder)
        assert fv.binance_bid == pytest.approx(50010.0)
        assert fv.binance_ask == pytest.approx(50020.0)

    def test_basis_bps_computed(self):
        fv = _base_fv(self.builder)
        # chainlink=50000, binance_mid=(50010+50020)/2=50015
        # basis = (50015 - 50000) / 50000 * 10000 = 3 bps
        assert fv.basis_bps == pytest.approx(3.0, rel=0.01)


class TestFeatureBuilderMissingInputs:
    def setup_method(self):
        self.builder = FeatureBuilder()

    def test_not_tradeable_when_chainlink_missing(self):
        fv = _base_fv(self.builder, chainlink=None)
        assert fv.is_tradeable is False
        assert fv.chainlink_now is None
        assert fv.chainlink_freshness == FreshnessState.MISSING

    def test_not_tradeable_when_chainlink_open_missing(self):
        fv = _base_fv(self.builder, chainlink_open=None)
        assert fv.is_tradeable is False

    def test_not_tradeable_when_up_book_missing(self):
        fv = _base_fv(self.builder, up_book=None)
        assert fv.is_tradeable is False
        assert fv.up_best_ask is None

    def test_not_tradeable_when_metadata_missing(self):
        fv = _base_fv(self.builder, metadata=None)
        assert fv.is_tradeable is False
        assert fv.fee_rate is None

    def test_not_tradeable_when_fee_rate_missing(self):
        meta_no_fee = MarketMetadata(
            condition_id="test_cid",
            fee_rate=None,
            fee_source=None,
            fees_enabled=None,
            tick_size=0.01,
            min_order_size=1.0,
        )
        fv = _base_fv(self.builder, metadata=meta_no_fee)
        assert fv.is_tradeable is False

    def test_delta_bps_none_when_chainlink_open_none(self):
        fv = _base_fv(self.builder, chainlink_open=None)
        assert fv.chainlink_delta_bps is None

    def test_pair_sum_none_when_down_book_missing(self):
        fv = _base_fv(self.builder, down_book=None)
        assert fv.pair_sum_best_ask is None

    def test_binance_freshness_missing_when_none(self):
        fv = _base_fv(self.builder, binance=None)
        assert fv.binance_bid is None
        assert fv.binance_freshness == FreshnessState.MISSING

    def test_not_tradeable_when_expired(self):
        now = time.time()
        fv = _base_fv(
            self.builder,
            window_start_ts=now - 310,
            window_end_ts=now - 10,  # expired 10s ago
        )
        assert fv.is_tradeable is False


class TestFeatureBuilderStaleChainlink:
    def setup_method(self):
        self.builder = FeatureBuilder()

    def test_not_tradeable_when_stale(self):
        stale = ChainlinkPrice(
            price_usd=50000.0,
            round_id=1,
            updated_at=time.time() - 100,
            fetched_at=time.time(),
            freshness=FreshnessState.STALE,
        )
        fv = _base_fv(self.builder, chainlink=stale)
        assert fv.is_tradeable is False
        assert fv.chainlink_freshness == FreshnessState.STALE
