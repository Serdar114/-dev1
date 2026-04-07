"""
Tests for signals.no_trade_rules — every rule fires correctly.
All offline, no network.
"""

import pytest
import time
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.no_trade_rules import (
    rule_chainlink_not_missing,
    rule_chainlink_not_stale,
    rule_window_open_captured,
    rule_window_not_expired,
    rule_metadata_complete,
    rule_fee_provenance_known,
    rule_book_present,
    rule_spread_acceptable,
    rule_pair_sum_valid,
    evaluate_all,
    TRADE_OK,
)
from loggingx.schemas import FeatureVector, FreshnessState, NoTradeReasonCode


def _base_fv(**overrides) -> FeatureVector:
    """Build a fully valid FeatureVector. Override fields to test failures."""
    now = time.time()
    defaults = dict(
        condition_id="cid_test",
        up_token_id="up_tok",
        down_token_id="dn_tok",
        window_start_ts=now - 150,
        window_end_ts=now + 150,
        built_at=now,
        secs_to_expiry=150.0,
        chainlink_open=49000.0,
        chainlink_now=49100.0,
        chainlink_delta_bps=20.4,
        chainlink_freshness=FreshnessState.FRESH,
        binance_bid=49090.0,
        binance_ask=49110.0,
        binance_delta_bps=18.0,
        binance_freshness=FreshnessState.FRESH,
        basis_bps=2.0,
        up_best_bid=0.48,
        up_best_ask=0.52,
        up_spread=0.04,
        down_best_bid=0.46,
        down_best_ask=0.50,
        down_spread=0.04,
        pair_sum_best_ask=1.02,
        fees_enabled=True,
        fee_rate=0.02,
        fee_source="api:test",
        effective_fee=0.01,
        tick_size=0.01,
        min_order_size=1.0,
        is_tradeable=True,
    )
    defaults.update(overrides)
    return FeatureVector(**defaults)


# ─── Individual rule tests ────────────────────────────────────

class TestRuleChainlinkNotMissing:
    def test_passes_when_present(self):
        fv = _base_fv()
        ok, reason, _ = rule_chainlink_not_missing(fv)
        assert ok is True

    def test_fails_when_none(self):
        fv = _base_fv(chainlink_now=None)
        ok, reason, details = rule_chainlink_not_missing(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.CHAINLINK_MISSING
        assert details["_canonical"] is True


class TestRuleChainlinkNotStale:
    def test_passes_when_fresh(self):
        fv = _base_fv(chainlink_freshness=FreshnessState.FRESH)
        ok, _, _ = rule_chainlink_not_stale(fv)
        assert ok is True

    def test_fails_when_stale(self):
        fv = _base_fv(chainlink_freshness=FreshnessState.STALE)
        ok, reason, details = rule_chainlink_not_stale(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.CHAINLINK_STALE
        assert details["_canonical"] is True

    def test_fails_when_missing(self):
        fv = _base_fv(chainlink_freshness=FreshnessState.MISSING, chainlink_now=None)
        ok, reason, _ = rule_chainlink_not_stale(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.CHAINLINK_STALE


class TestRuleWindowOpenCaptured:
    def test_passes_when_open_present(self):
        fv = _base_fv(chainlink_open=49000.0)
        ok, _, _ = rule_window_open_captured(fv)
        assert ok is True

    def test_fails_when_open_none(self):
        fv = _base_fv(chainlink_open=None)
        ok, reason, details = rule_window_open_captured(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.WINDOW_OPEN_NOT_CAPTURED
        assert details["_canonical"] is True


class TestRuleWindowNotExpired:
    def test_passes_when_enough_time(self):
        fv = _base_fv(secs_to_expiry=100.0)
        ok, _, _ = rule_window_not_expired(fv, min_secs=30.0)
        assert ok is True

    def test_fails_when_too_close(self):
        fv = _base_fv(secs_to_expiry=10.0)
        ok, reason, details = rule_window_not_expired(fv, min_secs=30.0)
        assert ok is False
        assert reason == NoTradeReasonCode.TOO_CLOSE_TO_EXPIRY
        assert details["secs_to_expiry"] == pytest.approx(10.0)

    def test_fails_when_secs_none(self):
        fv = _base_fv(secs_to_expiry=None)
        ok, reason, _ = rule_window_not_expired(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.MARKET_NOT_LIVE


class TestRuleMetadataComplete:
    def test_passes_when_complete(self):
        fv = _base_fv()
        ok, _, _ = rule_metadata_complete(fv)
        assert ok is True

    def test_fails_when_tick_size_missing(self):
        fv = _base_fv(tick_size=None)
        ok, reason, details = rule_metadata_complete(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.METADATA_INCOMPLETE
        assert "tick_size" in details["missing_fields"]

    def test_fails_when_min_order_missing(self):
        fv = _base_fv(min_order_size=None)
        ok, reason, details = rule_metadata_complete(fv)
        assert ok is False
        assert "min_order_size" in details["missing_fields"]


class TestRuleFeeProvenance:
    def test_passes_when_fee_known(self):
        fv = _base_fv(fee_rate=0.02, fee_source="api:x")
        ok, _, _ = rule_fee_provenance_known(fv)
        assert ok is True

    def test_fails_when_fee_rate_none(self):
        fv = _base_fv(fee_rate=None, fee_source=None)
        ok, reason, _ = rule_fee_provenance_known(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.FEE_PROVENANCE_UNCLEAR

    def test_fails_when_fee_source_none(self):
        fv = _base_fv(fee_rate=0.02, fee_source=None)
        ok, reason, _ = rule_fee_provenance_known(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.FEE_PROVENANCE_UNCLEAR


class TestRuleBookPresent:
    def test_passes_when_both_present(self):
        fv = _base_fv()
        ok, _, _ = rule_book_present(fv)
        assert ok is True

    def test_fails_when_up_ask_missing(self):
        fv = _base_fv(up_best_ask=None)
        ok, reason, details = rule_book_present(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.BOOK_INSUFFICIENT
        assert details["token"] == "up"

    def test_fails_when_down_ask_missing(self):
        fv = _base_fv(down_best_ask=None)
        ok, reason, details = rule_book_present(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.BOOK_INSUFFICIENT
        assert details["token"] == "down"


class TestRuleSpreadAcceptable:
    def test_passes_when_spread_ok(self):
        fv = _base_fv(up_spread=0.04, down_spread=0.04)
        ok, _, _ = rule_spread_acceptable(fv, max_spread=0.10)
        assert ok is True

    def test_fails_when_up_spread_too_wide(self):
        fv = _base_fv(up_spread=0.20)
        ok, reason, details = rule_spread_acceptable(fv, max_spread=0.10)
        assert ok is False
        assert reason == NoTradeReasonCode.SPREAD_TOO_WIDE
        assert details["token"] == "up"


class TestRulePairSum:
    def test_passes_at_fair_value(self):
        fv = _base_fv(pair_sum_best_ask=1.02)
        ok, _, _ = rule_pair_sum_valid(fv, pair_sum_min=0.90, pair_sum_max=1.20)
        assert ok is True

    def test_fails_when_too_low(self):
        fv = _base_fv(pair_sum_best_ask=0.80)
        ok, reason, details = rule_pair_sum_valid(fv, pair_sum_min=0.90, pair_sum_max=1.20)
        assert ok is False
        assert reason == NoTradeReasonCode.PAIR_SUM_SUSPICIOUS

    def test_fails_when_too_high(self):
        fv = _base_fv(pair_sum_best_ask=1.30)
        ok, reason, _ = rule_pair_sum_valid(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.PAIR_SUM_SUSPICIOUS

    def test_fails_when_none(self):
        fv = _base_fv(pair_sum_best_ask=None)
        ok, reason, _ = rule_pair_sum_valid(fv)
        assert ok is False


# ─── evaluate_all integration tests ──────────────────────────

class TestEvaluateAll:
    def test_trade_ok_when_all_pass(self):
        fv = _base_fv()
        ok, reason, details = evaluate_all(fv)
        assert ok is True
        assert reason is None
        assert details is None

    def test_stops_at_first_canonical_failure(self):
        # Chainlink missing should stop early, before book check
        fv = _base_fv(chainlink_now=None, up_best_ask=None)
        ok, reason, _ = evaluate_all(fv)
        assert ok is False
        # Should be chainlink issue, not book issue
        assert reason == NoTradeReasonCode.CHAINLINK_MISSING

    def test_chainlink_stale_blocks_even_with_good_book(self):
        fv = _base_fv(chainlink_freshness=FreshnessState.STALE)
        ok, reason, _ = evaluate_all(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.CHAINLINK_STALE

    def test_all_canonical_pass_book_blocks(self):
        fv = _base_fv(up_best_ask=None)
        ok, reason, _ = evaluate_all(fv)
        assert ok is False
        assert reason == NoTradeReasonCode.BOOK_INSUFFICIENT

    def test_canonical_flag_in_details(self):
        fv = _base_fv(chainlink_now=None)
        ok, reason, details = evaluate_all(fv)
        assert details["_canonical"] is True
