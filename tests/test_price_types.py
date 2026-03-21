"""
tests/test_price_types.py

Covers blocker #5 (round 3): explicit typed price-space classes with guards.

BTCSpotSnapshot — price_usd must be > 1.0
YesPriceSnapshot — probability must be in (0, 1) exclusive

Tests verify:
  - Valid values are accepted
  - Out-of-space values raise ValueError with a descriptive message
  - Frozen dataclasses cannot be mutated
  - The guard message helps developers identify the price-space mix-up
"""
import pytest
from feeds.price_types import BTCSpotSnapshot, YesPriceSnapshot


class TestBTCSpotSnapshot:
    def test_valid_btc_price_accepted(self):
        snap = BTCSpotSnapshot(price_usd=94_000.0, timestamp=1700000000.0, feed_name="fast")
        assert snap.price_usd == 94_000.0

    def test_low_btc_price_accepted(self):
        """Even $2 BTC/USD is > 1.0 — should be allowed."""
        snap = BTCSpotSnapshot(price_usd=2.0, timestamp=1700000000.0, feed_name="chainlink")
        assert snap.price_usd == 2.0

    def test_yes_probability_rejected(self):
        """A YES probability (0.87) passed as BTC/USD must raise ValueError."""
        with pytest.raises(ValueError, match="BTC/USD"):
            BTCSpotSnapshot(price_usd=0.87, timestamp=1700000000.0, feed_name="fast")

    def test_zero_price_rejected(self):
        with pytest.raises(ValueError):
            BTCSpotSnapshot(price_usd=0.0, timestamp=1700000000.0, feed_name="fast")

    def test_exactly_one_rejected(self):
        with pytest.raises(ValueError):
            BTCSpotSnapshot(price_usd=1.0, timestamp=1700000000.0, feed_name="fast")

    def test_negative_price_rejected(self):
        with pytest.raises(ValueError):
            BTCSpotSnapshot(price_usd=-94000.0, timestamp=1700000000.0, feed_name="fast")

    def test_is_frozen(self):
        snap = BTCSpotSnapshot(price_usd=94_000.0, timestamp=1700000000.0, feed_name="fast")
        with pytest.raises((AttributeError, TypeError)):
            snap.price_usd = 95_000.0  # type: ignore

    def test_is_stale_defaults_false(self):
        snap = BTCSpotSnapshot(price_usd=50_000.0, timestamp=1700000000.0, feed_name="fast")
        assert snap.is_stale is False

    def test_gap_seconds_defaults_none(self):
        snap = BTCSpotSnapshot(price_usd=50_000.0, timestamp=1700000000.0, feed_name="fast")
        assert snap.gap_seconds is None

    def test_feed_name_stored(self):
        snap = BTCSpotSnapshot(price_usd=50_000.0, timestamp=1700000000.0, feed_name="chainlink")
        assert snap.feed_name == "chainlink"

    def test_error_message_mentions_yes_probability(self):
        """Error message should help the developer understand the price-space mix-up."""
        with pytest.raises(ValueError) as exc_info:
            BTCSpotSnapshot(price_usd=0.5, timestamp=1700000000.0, feed_name="fast")
        assert "YES probability" in str(exc_info.value) or "BTC/USD" in str(exc_info.value)


class TestYesPriceSnapshot:
    def test_valid_yes_probability_accepted(self):
        snap = YesPriceSnapshot(probability=0.87, timestamp=1700000000.0, source="clob_midpoint")
        assert snap.probability == 0.87

    def test_low_probability_accepted(self):
        snap = YesPriceSnapshot(probability=0.11, timestamp=1700000000.0, source="clob_midpoint")
        assert snap.probability == 0.11

    def test_btc_price_rejected(self):
        """A BTC/USD price (94000.0) passed as YES probability must raise ValueError."""
        with pytest.raises(ValueError, match="YES probability"):
            YesPriceSnapshot(probability=94_000.0, timestamp=1700000000.0, source="clob_midpoint")

    def test_probability_zero_rejected(self):
        """0.0 is not in (0, 1) exclusive — must reject."""
        with pytest.raises(ValueError):
            YesPriceSnapshot(probability=0.0, timestamp=1700000000.0, source="clob_midpoint")

    def test_probability_one_rejected(self):
        """1.0 is not in (0, 1) exclusive — must reject."""
        with pytest.raises(ValueError):
            YesPriceSnapshot(probability=1.0, timestamp=1700000000.0, source="clob_midpoint")

    def test_probability_above_one_rejected(self):
        with pytest.raises(ValueError):
            YesPriceSnapshot(probability=1.5, timestamp=1700000000.0, source="clob_midpoint")

    def test_negative_probability_rejected(self):
        with pytest.raises(ValueError):
            YesPriceSnapshot(probability=-0.5, timestamp=1700000000.0, source="clob_midpoint")

    def test_is_provisional_defaults_true(self):
        snap = YesPriceSnapshot(probability=0.87, timestamp=1700000000.0, source="clob_midpoint")
        assert snap.is_provisional is True

    def test_token_id_defaults_none(self):
        snap = YesPriceSnapshot(probability=0.87, timestamp=1700000000.0, source="clob_midpoint")
        assert snap.token_id is None

    def test_is_frozen(self):
        snap = YesPriceSnapshot(probability=0.87, timestamp=1700000000.0, source="clob_midpoint")
        with pytest.raises((AttributeError, TypeError)):
            snap.probability = 0.90  # type: ignore

    def test_token_id_stored(self):
        snap = YesPriceSnapshot(
            probability=0.87, timestamp=1700000000.0,
            source="clob_midpoint", token_id="0xabc123"
        )
        assert snap.token_id == "0xabc123"

    def test_error_message_mentions_btc_usd(self):
        """Error message should identify the price-space confusion."""
        with pytest.raises(ValueError) as exc_info:
            YesPriceSnapshot(probability=50_000.0, timestamp=1700000000.0, source="x")
        msg = str(exc_info.value)
        assert "BTC/USD" in msg or "(0, 1)" in msg
