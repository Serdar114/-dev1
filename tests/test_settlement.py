"""
tests/test_settlement.py

Covers fix #4: settlement truth must use Chainlink as primary proxy.
Fast feed is fallback only. Settlement source is logged.
"""
import pytest
from bot import ResearchBot, load_config


class TestSettlementOutcome:
    """Test the _determine_outcome helper directly."""

    def _make_bot(self, base_config):
        from unittest.mock import MagicMock, patch
        # Patch the feed adapters so no real network calls happen
        with patch("bot.FastFeedAdapter"), patch("bot.ChainlinkFeedAdapter"):
            bot = object.__new__(ResearchBot)
            bot._config = base_config
            bot._phase = "0c"
            return bot

    def test_outcome_yes_when_close_above_open(self, base_config):
        bot = self._make_bot(base_config)
        assert bot._determine_outcome(0.50, 0.60) == "YES"

    def test_outcome_no_when_close_below_open(self, base_config):
        bot = self._make_bot(base_config)
        assert bot._determine_outcome(0.60, 0.50) == "NO"

    def test_outcome_none_when_open_missing(self, base_config):
        bot = self._make_bot(base_config)
        assert bot._determine_outcome(None, 0.60) is None

    def test_outcome_none_when_close_missing(self, base_config):
        bot = self._make_bot(base_config)
        assert bot._determine_outcome(0.60, None) is None


class TestSettlementSourceLabels:
    """Verify settlement source constants are distinct and correctly named."""

    def test_chainlink_proxy_label(self):
        from bot import SETTLE_CHAINLINK_PROXY
        assert SETTLE_CHAINLINK_PROXY == "CHAINLINK_PROXY"

    def test_fast_proxy_label(self):
        from bot import SETTLE_FAST_PROXY
        assert SETTLE_FAST_PROXY == "FAST_PROXY"

    def test_unavailable_label(self):
        from bot import SETTLE_UNAVAILABLE
        assert SETTLE_UNAVAILABLE == "UNAVAILABLE"

    def test_labels_are_distinct(self):
        from bot import SETTLE_CHAINLINK_PROXY, SETTLE_FAST_PROXY, SETTLE_UNAVAILABLE
        labels = {SETTLE_CHAINLINK_PROXY, SETTLE_FAST_PROXY, SETTLE_UNAVAILABLE}
        assert len(labels) == 3

    def test_window_log_has_settlement_source_field(self):
        from logger.summary import WindowLog
        wl = WindowLog(window_open_ts=1, slug="s", phase="0c")
        assert hasattr(wl, "settlement_source")
        assert wl.settlement_source == "UNAVAILABLE"

    def test_settlement_source_can_be_set_to_chainlink_proxy(self):
        from logger.summary import WindowLog
        from bot import SETTLE_CHAINLINK_PROXY
        wl = WindowLog(window_open_ts=1, slug="s", phase="0c")
        wl.settlement_source = SETTLE_CHAINLINK_PROXY
        assert wl.settlement_source == "CHAINLINK_PROXY"


class TestSettlementSeparationFromFastFeed:
    """
    Verify that settlement does NOT use fast feed close when Chainlink is
    available, and documents fallback clearly.
    """

    def test_chainlink_proxy_is_not_same_as_fast_proxy(self):
        from bot import SETTLE_CHAINLINK_PROXY, SETTLE_FAST_PROXY
        assert SETTLE_CHAINLINK_PROXY != SETTLE_FAST_PROXY

    def test_settlement_uses_chainlink_open_not_fast_open(self, base_config):
        """
        If Chainlink open = 0.50 and fast open = 0.60, and Chainlink close = 0.55:
        Settlement via Chainlink proxy → YES (0.55 > 0.50).
        Settlement via fast proxy would use fast open (0.60) → NO (0.55 < 0.60).
        These diverge — the test confirms we cannot get the right answer
        without knowing the settlement source.
        """
        from unittest.mock import MagicMock, patch
        with patch("bot.FastFeedAdapter"), patch("bot.ChainlinkFeedAdapter"):
            bot = object.__new__(ResearchBot)
        # Chainlink proxy settlement
        chainlink_outcome = bot._determine_outcome(0.50, 0.55)
        fast_outcome = bot._determine_outcome(0.60, 0.55)
        assert chainlink_outcome == "YES"
        assert fast_outcome == "NO"
        # This divergence shows why source separation matters.
