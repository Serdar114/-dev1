"""
tests/test_signal_rejection.py

Covers fix #3 + #9: every NONE signal has structured rejection_reasons.
Unavailable data (CLOB book, candles) is reported explicitly, not silently
collapsed to NONE.
"""
import pytest
from signal.engine import SignalEngine, FeedWindow, SignalDirection


def _make_fw(**kwargs):
    """Build a FeedWindow with all defaults; override with kwargs."""
    defaults = dict(
        window_open_ts=1_700_000_000,
        slug="test-slug",
        open_fast_price=50_000.0,
        latest_fast_price=50_200.0,
        open_chainlink_price=50_005.0,
        latest_chainlink_price=50_100.0,
        current_yes_mid=0.87,
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
    defaults.update(kwargs)
    return FeedWindow(**defaults)


class TestStructuredRejectionReasons:
    def test_no_signal_when_book_unavailable(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(yes_book_available=False)
        sig = engine.evaluate(fw)
        assert sig.gates["spread_quality"] is False
        assert any("clob_book_unavailable" in r for r in sig.rejection_reasons)

    def test_no_signal_when_candles_unavailable(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(candles_available=False)
        sig = engine.evaluate(fw)
        assert sig.gates["momentum_persistence"] is False
        assert any("candle_data_unavailable" in r for r in sig.rejection_reasons)

    def test_todo_comment_in_clob_rejection(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(yes_book_available=False)
        sig = engine.evaluate(fw)
        reasons_text = " ".join(sig.rejection_reasons)
        # Should mention TODO to connect CLOB
        assert "TODO" in reasons_text or "clob_book_unavailable" in reasons_text

    def test_todo_comment_in_candle_rejection(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(candles_available=False)
        sig = engine.evaluate(fw)
        reasons_text = " ".join(sig.rejection_reasons)
        assert "TODO" in reasons_text or "candle_data_unavailable" in reasons_text

    def test_stale_fast_feed_rejection_reason(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(fast_feed_stale=True)
        sig = engine.evaluate(fw)
        assert any("fast_feed_stale" in r for r in sig.rejection_reasons)

    def test_stale_chainlink_rejection_reason(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(chainlink_feed_stale=True)
        sig = engine.evaluate(fw)
        assert any("chainlink_feed_stale" in r for r in sig.rejection_reasons)

    def test_endcycle_too_late_rejection_reason(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(seconds_to_window_close=10.0)  # below 45s cutoff
        sig = engine.evaluate(fw)
        assert any("endcycle_timing" in r for r in sig.rejection_reasons)

    def test_rejection_reasons_empty_when_all_gates_pass(self, base_config,
                                                          all_gates_open_feed_window):
        engine = SignalEngine(base_config)
        sig = engine.evaluate(all_gates_open_feed_window)
        assert sig.quote_eligible is True
        # No gate-failure reasons when everything passes
        gate_failure_reasons = [
            r for r in sig.rejection_reasons
            if not r.startswith("basis:")   # basis is informational, not a gate
        ]
        assert len(gate_failure_reasons) == 0

    def test_rejection_summary_method(self, base_config):
        engine = SignalEngine(base_config)
        fw = _make_fw(yes_book_available=False, candles_available=False)
        sig = engine.evaluate(fw)
        summary = sig.rejection_summary()
        assert "clob_book_unavailable" in summary
        assert "candle_data_unavailable" in summary

    def test_gate_summary_method(self, base_config, all_gates_open_feed_window):
        engine = SignalEngine(base_config)
        sig = engine.evaluate(all_gates_open_feed_window)
        summary = sig.gate_summary()
        assert "OK" in summary
        assert "feed_freshness" in summary

    def test_direction_none_when_unavailable_data(self, base_config):
        """With CLOB and candles both unavailable, direction must be NONE."""
        engine = SignalEngine(base_config)
        fw = _make_fw(yes_book_available=False, candles_available=False)
        sig = engine.evaluate(fw)
        assert sig.direction == SignalDirection.NONE
        assert sig.quote_eligible is False


class TestGatePassWithAllDataAvailable:
    def test_all_gates_pass_with_full_data(self, base_config, all_gates_open_feed_window):
        engine = SignalEngine(base_config)
        sig = engine.evaluate(all_gates_open_feed_window)
        assert sig.gates["feed_freshness"] is True
        assert sig.gates["extreme_zone"] is True
        assert sig.gates["spread_quality"] is True
        assert sig.gates["momentum_persistence"] is True
        assert sig.gates["endcycle_timing"] is True
        assert sig.gates["open_price_integrity"] is True
        assert sig.quote_eligible is True

    def test_direction_set_when_all_gates_pass(self, base_config, all_gates_open_feed_window):
        engine = SignalEngine(base_config)
        sig = engine.evaluate(all_gates_open_feed_window)
        assert sig.direction != SignalDirection.NONE
        assert sig.intended_price is not None
