"""
tests/test_provisional_mode.py

Covers blocker #6 (round 3): STRICT vs PROVISIONAL bot mode behaviour.

Key invariants:
  - PROVISIONAL mode soft-passes spread_quality and momentum_persistence when
    data is unavailable (candles_available=False, yes_book_available=False).
    These windows are quote_eligible=True if direction is computable.
  - STRICT mode hard-fails those gates — no soft-pass.
  - Hard gates (feed_freshness, endcycle_timing, extreme_zone,
    open_price_integrity) are NEVER soft-passed in either mode.
  - is_provisional flag is set on WindowSignal when any gate was soft-passed.
  - PROVISIONAL_SOFT_PASS appears in rejection_reasons for soft-passed gates.

Also covers blocker #3 (round 3): bot is not structurally no-trade in
PROVISIONAL mode — candidates are produced even without CLOB book / candles.
"""
import pytest
from sigeng.engine import SignalEngine, FeedWindow, SignalDirection


def _make_fw(**kwargs):
    """FeedWindow with all gates passable by default; override with kwargs.

    seconds_to_window_close defaults to 30.0 — within the decision window [10, 45].
    Tests of the endcycle gate explicitly override this.
    """
    defaults = dict(
        window_open_ts=1_700_000_000,
        slug="btc-updown-5m-1700000000",
        open_fast_price=50_000.0,
        latest_fast_price=50_200.0,
        open_chainlink_price=50_005.0,
        latest_chainlink_price=50_100.0,
        # decision_fast_price > open_chainlink_price → YES direction
        decision_fast_price=50_200.0,
        decision_chainlink_price=50_100.0,
        current_yes_mid=0.87,            # YES probability — in-zone
        yes_bid=None,                    # book unavailable by default
        yes_ask=None,
        fast_gap_seconds=2.0,
        chainlink_gap_seconds=3.0,
        fast_feed_stale=False,
        chainlink_feed_stale=False,
        seconds_to_window_close=30.0,    # within decision window [10, 45]
        candles_same_direction=0,        # no candles by default
        yes_book_available=False,        # no CLOB book by default
        candles_available=False,         # no candles by default
    )
    defaults.update(kwargs)
    return FeedWindow(**defaults)


def _provisional_config():
    return {
        "bot_mode": "PROVISIONAL",
        "signal": {
            "decision_window_start_seconds_to_close": 45,
            "decision_window_end_seconds_to_close": 10,
            "endcycle_entry_cutoff_seconds": 45,   # legacy alias
            "feed_freshness_threshold_seconds": 8.0,
            "basis_mismatch_flag_threshold_bps": 30.0,
            "min_spread_quality_bps": 5.0,
            "extreme_zone_low": 0.10,
            "extreme_zone_high": 0.90,
            "momentum_persistence_candles": 2,
        },
    }


def _strict_config():
    cfg = _provisional_config()
    cfg["bot_mode"] = "STRICT"
    return cfg


class TestProvisionalModeProducesCandidates:
    def test_provisional_with_no_clob_no_candles_is_eligible(self):
        """
        Blocker #3: in PROVISIONAL mode with yes_book_available=False and
        candles_available=False, the signal should be quote_eligible=True
        (both gates are soft-passed) when all hard gates pass and direction
        is computable.
        """
        engine = SignalEngine(_provisional_config())
        fw = _make_fw()   # no book, no candles — both soft gates fail by data
        sig = engine.evaluate(fw)
        assert sig.quote_eligible is True, (
            f"Expected quote_eligible=True in PROVISIONAL mode with missing data. "
            f"Gates: {sig.gates}. Rejections: {sig.rejection_reasons}"
        )

    def test_provisional_direction_is_yes_not_none(self):
        engine = SignalEngine(_provisional_config())
        fw = _make_fw()
        sig = engine.evaluate(fw)
        assert sig.direction == SignalDirection.YES

    def test_provisional_is_provisional_flag_set(self):
        engine = SignalEngine(_provisional_config())
        fw = _make_fw()
        sig = engine.evaluate(fw)
        assert sig.is_provisional is True

    def test_provisional_soft_pass_in_spread_quality_reason(self):
        engine = SignalEngine(_provisional_config())
        fw = _make_fw(yes_book_available=False)
        sig = engine.evaluate(fw)
        assert sig.gates["spread_quality"] is True  # soft-passed
        assert any("PROVISIONAL_SOFT_PASS" in r for r in sig.rejection_reasons), (
            "Expected PROVISIONAL_SOFT_PASS in rejection_reasons for spread_quality gate"
        )

    def test_provisional_soft_pass_in_momentum_reason(self):
        engine = SignalEngine(_provisional_config())
        fw = _make_fw(candles_available=False)
        sig = engine.evaluate(fw)
        assert sig.gates["momentum_persistence"] is True  # soft-passed
        assert any("PROVISIONAL_SOFT_PASS" in r for r in sig.rejection_reasons), (
            "Expected PROVISIONAL_SOFT_PASS in rejection_reasons for momentum_persistence gate"
        )


class TestStrictModeHardFailsOnMissingData:
    def test_strict_with_no_clob_is_not_eligible(self):
        engine = SignalEngine(_strict_config())
        fw = _make_fw(yes_book_available=False, candles_available=False)
        sig = engine.evaluate(fw)
        assert sig.quote_eligible is False

    def test_strict_spread_quality_hard_fails(self):
        engine = SignalEngine(_strict_config())
        fw = _make_fw(yes_book_available=False)
        sig = engine.evaluate(fw)
        assert sig.gates["spread_quality"] is False
        # Must use STRICT_FAIL label, not PROVISIONAL_SOFT_PASS
        assert any("STRICT_FAIL" in r for r in sig.rejection_reasons), (
            f"Expected STRICT_FAIL in rejection_reasons. Got: {sig.rejection_reasons}"
        )

    def test_strict_momentum_hard_fails(self):
        engine = SignalEngine(_strict_config())
        fw = _make_fw(candles_available=False)
        sig = engine.evaluate(fw)
        assert sig.gates["momentum_persistence"] is False
        assert any("STRICT_FAIL" in r for r in sig.rejection_reasons)

    def test_strict_is_provisional_false(self):
        engine = SignalEngine(_strict_config())
        fw = _make_fw(yes_book_available=True, candles_available=True,
                      yes_bid=0.86, yes_ask=0.88, candles_same_direction=3)
        sig = engine.evaluate(fw)
        assert sig.is_provisional is False


class TestHardGatesNeverSoftPassed:
    """Hard gates must fail in both STRICT and PROVISIONAL modes when data is bad."""

    def test_stale_feed_hard_fails_in_provisional(self):
        engine = SignalEngine(_provisional_config())
        fw = _make_fw(fast_feed_stale=True)
        sig = engine.evaluate(fw)
        assert sig.gates["feed_freshness"] is False
        assert sig.quote_eligible is False

    def test_endcycle_timing_hard_fails_when_too_late(self):
        """Evaluated below dw_end (5s < 10s) — too late to submit."""
        engine = SignalEngine(_provisional_config())
        fw = _make_fw(seconds_to_window_close=5.0)   # below dw_end=10
        sig = engine.evaluate(fw)
        assert sig.gates["endcycle_timing"] is False
        assert sig.quote_eligible is False
        assert any("too_late" in r for r in sig.rejection_reasons)

    def test_endcycle_timing_hard_fails_when_too_early(self):
        """Evaluated far above dw_start (290s > 45s) — window just opened, not endcycle."""
        engine = SignalEngine(_provisional_config())
        fw = _make_fw(seconds_to_window_close=290.0)  # above dw_start=45
        sig = engine.evaluate(fw)
        assert sig.gates["endcycle_timing"] is False
        assert sig.quote_eligible is False
        assert any("too_early" in r for r in sig.rejection_reasons)

    def test_extreme_zone_hard_fails_in_provisional(self):
        engine = SignalEngine(_provisional_config())
        fw = _make_fw(current_yes_mid=None)  # no YES mid
        sig = engine.evaluate(fw)
        assert sig.gates["extreme_zone"] is False
        assert sig.quote_eligible is False

    def test_open_price_integrity_hard_fails_in_provisional(self):
        engine = SignalEngine(_provisional_config())
        # Make open prices diverge by >> tolerance
        fw = _make_fw(
            open_fast_price=50_000.0,
            open_chainlink_price=60_000.0,  # 2000 bps divergence > 60 bps tolerance
        )
        sig = engine.evaluate(fw)
        assert sig.gates["open_price_integrity"] is False
        assert sig.quote_eligible is False


class TestDefaultModeIsStrict:
    def test_no_bot_mode_key_defaults_to_strict(self):
        """If bot_mode is missing from config, default must be STRICT."""
        config = {
            "signal": {
                "endcycle_entry_cutoff_seconds": 45,
                "feed_freshness_threshold_seconds": 8.0,
                "basis_mismatch_flag_threshold_bps": 30.0,
                "min_spread_quality_bps": 5.0,
                "extreme_zone_low": 0.10,
                "extreme_zone_high": 0.90,
                "momentum_persistence_candles": 2,
            }
            # no bot_mode key
        }
        engine = SignalEngine(config)
        fw = _make_fw(yes_book_available=False, candles_available=False)
        sig = engine.evaluate(fw)
        # In STRICT mode (default), missing data = hard fail
        assert sig.quote_eligible is False
