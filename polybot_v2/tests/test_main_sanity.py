"""
Tests for main.py: build_market_snapshot slug propagation,
_sanity_check_market degenerate-book / mid-consistency behaviour,
and _log_signal null-safety.
"""
import sys, time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from main import build_market_snapshot, PolybotV2
from market_discovery import ActiveMarket
from models import MarketSnapshot
from settings import Settings

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def make_active_market(slug="btc-updown-5m-1773645300") -> ActiveMarket:
    return ActiveMarket(
        condition_id="cid-test",
        token_id_yes="yes-token",
        token_id_no="no-token",
        window_end_ts=time.time() + 150.0,
        slug=slug,
    )


def make_mock_client(bid_yes=0.48, ask_yes=0.52, bid_no=0.48, ask_no=0.52):
    """PolymarketClient mock that returns a minimal valid book."""
    client = MagicMock()
    client.get_order_book.return_value = {"bids": [], "asks": []}
    client.parse_best_bid_ask.side_effect = lambda book: (
        (bid_yes, ask_yes) if client.parse_best_bid_ask.call_count % 2 == 1
        else (bid_no, ask_no)
    )
    # Alternate: first call → YES, second call → NO
    call_count = [0]
    def _parse(book):
        call_count[0] += 1
        if call_count[0] % 2 == 1:
            return (bid_yes, ask_yes)
        return (bid_no, ask_no)
    client.parse_best_bid_ask.side_effect = _parse
    client.get_last_trade_price.return_value = None
    return client


def make_bot():
    """Minimal PolybotV2 instance with UI disabled for unit testing."""
    cfg = Settings(CONFIG_PATH)
    # Override UI so we don't need a terminal
    cfg._raw.setdefault("ui", {})["enabled"] = False
    bot = PolybotV2.__new__(PolybotV2)
    # Manually seed only the attributes used by _sanity_check_market
    bot._cfg = cfg
    bot._last_sanity_details = {}
    return bot


def make_market(bid_yes=0.48, ask_yes=0.52, bid_no=0.48, ask_no=0.52, ste=150.0):
    return MarketSnapshot(
        condition_id="c", token_id_yes="y", token_id_no="n",
        best_bid_yes=bid_yes, best_ask_yes=ask_yes,
        best_bid_no=bid_no, best_ask_no=ask_no,
        last_trade_price_yes=None,
        window_end_ts=time.time() + ste,
    )


# ──────────────────────────────────────────────────────────────────────────────
# A) Slug propagation
# ──────────────────────────────────────────────────────────────────────────────

class TestSlugPropagation:
    def test_slug_copied_from_active_market(self):
        """build_market_snapshot must copy ActiveMarket.slug into MarketSnapshot.slug."""
        slug = "btc-updown-5m-1773645300"
        market_info = make_active_market(slug=slug)
        client = make_mock_client()
        snap = build_market_snapshot(market_info, client)
        assert snap is not None
        assert snap.slug == slug, f"Expected slug={slug!r}, got {snap.slug!r}"

    def test_slug_empty_string_preserved(self):
        """If ActiveMarket.slug is empty, MarketSnapshot.slug is also empty (not crashed)."""
        market_info = make_active_market(slug="")
        client = make_mock_client()
        snap = build_market_snapshot(market_info, client)
        assert snap is not None
        assert snap.slug == ""

    def test_slug_not_lost_when_book_fails(self):
        """build_market_snapshot returns None when order book fetch fails — no slug issue."""
        market_info = make_active_market(slug="btc-updown-5m-1773645300")
        client = MagicMock()
        client.get_order_book.return_value = None  # fetch fails
        snap = build_market_snapshot(market_info, client)
        assert snap is None  # early return — not a slug bug


# ──────────────────────────────────────────────────────────────────────────────
# B) Degenerate book detection
# ──────────────────────────────────────────────────────────────────────────────

class TestDegenerateBookReject:
    def test_yes_bid_zero_rejected(self):
        """YES bid=0 → degenerate_book_reject (not silently passed with fallback mid=0.5)."""
        bot = make_bot()
        market = make_market(bid_yes=0.0, ask_yes=0.01)
        result = bot._sanity_check_market(market)
        assert result is not None
        assert "degenerate_book_reject" in result
        assert "yes_bid" in result

    def test_yes_ask_zero_rejected(self):
        """YES ask=0 → degenerate_book_reject."""
        bot = make_bot()
        market = make_market(bid_yes=0.0, ask_yes=0.0)
        result = bot._sanity_check_market(market)
        assert result is not None
        assert "degenerate_book_reject" in result

    def test_yes_bid_ge_ask_rejected(self):
        """YES bid >= ask (crossed book) → degenerate_book_reject."""
        bot = make_bot()
        market = make_market(bid_yes=0.55, ask_yes=0.50)  # crossed
        result = bot._sanity_check_market(market)
        assert result is not None
        assert "degenerate_book_reject" in result

    def test_no_bid_zero_rejected(self):
        """NO bid=0 with valid YES → degenerate_book_reject on NO side."""
        bot = make_bot()
        market = make_market(bid_yes=0.48, ask_yes=0.52, bid_no=0.0, ask_no=0.52)
        result = bot._sanity_check_market(market)
        assert result is not None
        assert "degenerate_book_reject" in result
        assert "no_bid" in result

    def test_valid_book_not_rejected(self):
        """Normal tight book passes degenerate check."""
        bot = make_bot()
        market = make_market(bid_yes=0.48, ask_yes=0.52, bid_no=0.48, ask_no=0.52)
        result = bot._sanity_check_market(market)
        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# C) Mid consistency: yes_mid must equal (bid+ask)/2, never silent 0.5 fallback
# ──────────────────────────────────────────────────────────────────────────────

class TestSanityMidConsistency:
    def test_yes_mid_derived_from_raw_quotes(self):
        """yes_mid in _last_sanity_details == (bid_yes + ask_yes) / 2 exactly."""
        bot = make_bot()
        market = make_market(bid_yes=0.46, ask_yes=0.54)
        bot._sanity_check_market(market)
        sd = bot._last_sanity_details
        assert sd["yes_mid"] == pytest.approx((0.46 + 0.54) / 2.0)

    def test_no_mid_derived_from_raw_quotes(self):
        """no_mid in _last_sanity_details == (bid_no + ask_no) / 2 exactly."""
        bot = make_bot()
        market = make_market(bid_no=0.44, ask_no=0.56)
        bot._sanity_check_market(market)
        sd = bot._last_sanity_details
        assert sd["no_mid"] == pytest.approx((0.44 + 0.56) / 2.0)

    def test_degenerate_book_logs_none_mid_not_fallback(self):
        """Degenerate book must NOT log yes_mid=0.5 (the silent fallback).
        It must log yes_mid=None so no row contains contradictory fields."""
        bot = make_bot()
        market = make_market(bid_yes=0.0, ask_yes=0.01)
        bot._sanity_check_market(market)
        sd = bot._last_sanity_details
        # Raw quotes logged as-is
        assert sd["yes_bid"] == 0.0
        assert sd["yes_ask"] == 0.01
        # Mid must NOT be the 0.5 fallback — must be None
        assert sd["yes_mid"] is None, f"Expected None, got {sd['yes_mid']}"
        assert sd["no_mid"] is None

    def test_spread_computed_from_raw_not_fallback(self):
        """spread_yes must equal ask_yes - bid_yes directly."""
        bot = make_bot()
        market = make_market(bid_yes=0.47, ask_yes=0.53)
        bot._sanity_check_market(market)
        sd = bot._last_sanity_details
        assert sd["spread_yes"] == pytest.approx(0.53 - 0.47)

    def test_complement_skew_consistent_with_mid(self):
        """complement_skew == |yes_mid + no_mid - 1.0| using raw-derived mids."""
        bot = make_bot()
        # Slightly skewed: YES mid=0.51, NO mid=0.52 → sum=1.03, skew=0.03
        market = make_market(bid_yes=0.50, ask_yes=0.52, bid_no=0.51, ask_no=0.53)
        bot._sanity_check_market(market)
        sd = bot._last_sanity_details
        expected_skew = abs((0.50 + 0.52) / 2 + (0.51 + 0.53) / 2 - 1.0)
        assert sd["complement_skew"] == pytest.approx(expected_skew, abs=1e-6)


# ──────────────────────────────────────────────────────────────────────────────
# D) _log_signal null-safety: degenerate book must not crash round()
# ──────────────────────────────────────────────────────────────────────────────

class TestLogSignalNullSafe:
    """
    After a degenerate_book_reject, _last_sanity_details contains
    spread_yes=None, spread_no=None, complement_skew=None, yes_mid=None,
    no_mid=None.  The previous code did round(sd.get("spread_yes", 0.0), 5)
    which returned None (key exists) and then round(None, 5) threw TypeError.
    """

    def _make_logged_signals(self, bot, decision):
        """Run _log_signal and return the captured JSONL record (no real file I/O)."""
        captured = {}

        class _CapturingLogger:
            def log(self, channel, record):
                captured.update(record)

        bot._structured = _CapturingLogger()
        bot._current_market = None
        bot._log_signal(decision)
        return captured

    def _make_decision_for_bot(self):
        """Minimal SignalDecision with degenerate-book context."""
        from models import SignalDecision
        return SignalDecision(
            ts=time.time(), window_ts=time.time() + 150,
            lane="selective_taker", action="NO_TRADE",
            chosen_side=None, reason="degenerate_book_reject(yes_bid=0.0000 yes_ask=0.0100)",
            seconds_to_expiry=150.0,
            elapsed_from_window_start=30.0,
            btc_mid=84000.0, window_open=84000.0,
            delta_pct=0.0, delta_raw_fraction=0.0, delta_pct_display=0.0,
            realized_vol_60s=0.001,
            implied_yes_prob=0.5,
            fair_computed_fresh=False, context_from_cache=False,
        )

    def test_degenerate_book_no_crash_on_log_signal(self):
        """_log_signal must not raise TypeError when spread/mid/skew are None in sd."""
        bot = make_bot()
        # Seed _last_sanity_details exactly as degenerate path produces it
        bot._last_sanity_details = {
            "yes_bid": 0.0, "yes_ask": 0.01,
            "no_bid": 0.48, "no_ask": 0.52,
            "yes_mid": None, "no_mid": None,
            "midpoint_sum": None, "spread_yes": None, "spread_no": None,
            "complement_skew": None,
            "spread_threshold": 0.05, "skew_threshold": 0.05,
            "reject": "degenerate_book_reject(yes_bid=0.0000 yes_ask=0.0100)",
        }
        decision = self._make_decision_for_bot()
        # Must not raise — this was the crash site
        record = self._make_logged_signals(bot, decision)
        assert record["spread_yes"] is None
        assert record["spread_no"] is None
        assert record["complement_skew"] is None
        assert record["yes_mid"] is None
        assert record["no_mid"] is None
        assert record["sanity_status"] == "reject"

    def test_valid_book_spread_rounded(self):
        """After a normal sanity pass, spread fields are numeric and rounded."""
        bot = make_bot()
        market = make_market(bid_yes=0.4712345, ask_yes=0.5287655)
        bot._sanity_check_market(market)
        decision = self._make_decision_for_bot()
        decision.implied_yes_prob = market.implied_yes_prob
        record = self._make_logged_signals(bot, decision)
        assert record["spread_yes"] is not None
        assert isinstance(record["spread_yes"], float)
        # 5 decimal places
        assert record["spread_yes"] == round(0.5287655 - 0.4712345, 5)

    def test_yes_mid_rounded_on_valid_book(self):
        """yes_mid is rounded to 4dp on a valid book."""
        bot = make_bot()
        market = make_market(bid_yes=0.4812345, ask_yes=0.5187655)
        bot._sanity_check_market(market)
        decision = self._make_decision_for_bot()
        record = self._make_logged_signals(bot, decision)
        expected = round((0.4812345 + 0.5187655) / 2.0, 4)
        assert record["yes_mid"] == expected
