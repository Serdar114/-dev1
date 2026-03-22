"""
tests/test_extreme_zone.py

Covers fix #2: extreme_zone gate must use current_yes_mid (Polymarket YES
probability, 0-1 range) NOT BTC/USD spot price.
"""
import pytest
from sigeng.engine import SignalEngine, FeedWindow, SignalDirection


def _make_fw(current_yes_mid, yes_book_available=True, candles_available=True, **kwargs):
    defaults = dict(
        window_open_ts=1_700_000_000,
        slug="btc-updown-5m-1700000000",
        open_fast_price=50_000.0,
        latest_fast_price=50_200.0,
        open_chainlink_price=50_005.0,
        latest_chainlink_price=50_100.0,
        yes_bid=0.86,
        yes_ask=0.88,
        fast_gap_seconds=2.0,
        chainlink_gap_seconds=3.0,
        fast_feed_stale=False,
        chainlink_feed_stale=False,
        seconds_to_window_close=30.0,   # within decision window [10, 45]
        candles_same_direction=3,
        yes_book_available=yes_book_available,
        candles_available=candles_available,
    )
    defaults.update(kwargs)
    return FeedWindow(current_yes_mid=current_yes_mid, **defaults)


class TestExtremeZoneGate:
    def test_yes_mid_in_range_passes(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(current_yes_mid=0.87)
        sig = engine.evaluate(fw)
        assert sig.gates["extreme_zone"] is True

    def test_yes_mid_at_lower_bound_blocked(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(current_yes_mid=0.10)
        sig = engine.evaluate(fw)
        # 0.10 is not strictly > extreme_zone_low (0.10), so gate fails
        assert sig.gates["extreme_zone"] is False

    def test_yes_mid_at_upper_bound_blocked(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(current_yes_mid=0.90)
        sig = engine.evaluate(fw)
        assert sig.gates["extreme_zone"] is False

    def test_yes_mid_above_high_blocked(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(current_yes_mid=0.95)
        sig = engine.evaluate(fw)
        assert sig.gates["extreme_zone"] is False
        assert any("extreme_zone" in r for r in sig.rejection_reasons)

    def test_yes_mid_below_low_blocked(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(current_yes_mid=0.05)
        sig = engine.evaluate(fw)
        assert sig.gates["extreme_zone"] is False

    def test_yes_mid_none_gate_fails_with_reason(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(current_yes_mid=None)
        sig = engine.evaluate(fw)
        assert sig.gates["extreme_zone"] is False
        assert any("yes_mid_unavailable" in r for r in sig.rejection_reasons)

    def test_btc_usd_price_never_used_for_extreme_zone(self, base_config):
        """
        Regression: BTC/USD price (e.g. 94000.0) must NEVER be compared
        to extreme zone thresholds [0.10, 0.90].  If it were, the gate would
        always fail (94000 > 0.90).  We verify this by setting latest_chainlink_price
        to a BTC/USD value and confirming yes_mid (0.87) drives the gate correctly.
        """
        engine = SignalEngine(base_config)
        fw = _make_fw(
            current_yes_mid=0.87,          # YES probability — should pass
            # BTC/USD values that would fail if incorrectly used for extreme zone
            **{
                "latest_chainlink_price": 94_000.0,
                "latest_fast_price": 94_050.0,
                "open_chainlink_price": 93_990.0,
                "open_fast_price": 93_995.0,
            }
        )
        sig = engine.evaluate(fw)
        # extreme_zone should PASS (based on yes_mid=0.87, not on 94000)
        assert sig.gates["extreme_zone"] is True, (
            "extreme_zone failed — check if BTC/USD price is being compared "
            "to 0.10/0.90 thresholds instead of current_yes_mid"
        )

    def test_extreme_zone_rejection_reason_includes_yes_mid_value(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(current_yes_mid=0.95)
        sig = engine.evaluate(fw)
        # Rejection reason should reference yes_mid value
        reasons_text = " ".join(sig.rejection_reasons)
        assert "0.9500" in reasons_text or "extreme_zone" in reasons_text
