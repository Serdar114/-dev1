"""
Tests for main.py: build_market_snapshot slug propagation and
_sanity_check_market degenerate-book / mid-consistency behaviour.
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
