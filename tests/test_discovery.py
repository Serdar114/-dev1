"""
tests/test_discovery.py — Unit tests for market discovery.

Tests slug construction, clobTokenIds parsing, and market record building.
No live network calls. All Gamma API calls are mocked.
"""
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch

from discovery.market_discovery import (
    MarketDiscovery,
    _parse_token_ids,
    _parse_outcomes,
    _market_from_gamma,
    _extract_window_ts_from_slug,
)
from truth.window_clock import current_window_start, prev_window_start


# ---------------------------------------------------------------------------
# _parse_token_ids
# ---------------------------------------------------------------------------

def test_parse_token_ids_from_list():
    raw = ["abc123", "def456"]
    result = _parse_token_ids(raw)
    assert result == ["abc123", "def456"]


def test_parse_token_ids_from_json_string():
    raw = '["abc123", "def456"]'
    result = _parse_token_ids(raw)
    assert result == ["abc123", "def456"]


def test_parse_token_ids_rejects_raw_string_not_json():
    """A raw non-JSON string must raise, not silently produce garbage."""
    with pytest.raises((json.JSONDecodeError, ValueError)):
        _parse_token_ids("notjson")


def test_parse_token_ids_rejects_wrong_json_type():
    """A JSON string that decodes to non-list must raise."""
    with pytest.raises(ValueError):
        _parse_token_ids('{"key": "value"}')


def test_parse_token_ids_converts_ints_to_str():
    raw = [123, 456]
    result = _parse_token_ids(raw)
    assert result == ["123", "456"]


def test_parse_token_ids_rejects_unsupported_type():
    with pytest.raises(ValueError):
        _parse_token_ids(42)


# ---------------------------------------------------------------------------
# _extract_window_ts_from_slug
# ---------------------------------------------------------------------------

def test_extract_ts_from_standard_slug():
    slug = "btc-up-down-5m-1700000100"
    ts = _extract_window_ts_from_slug(slug)
    assert ts == 1700000100


def test_extract_ts_handles_missing_ts():
    """If no plausible Unix timestamp found, falls back to current window."""
    slug = "btc-no-ts-here"
    ts = _extract_window_ts_from_slug(slug)
    expected = current_window_start()
    # Should be within 300 seconds of current window
    assert abs(ts - expected) <= 300


# ---------------------------------------------------------------------------
# _market_from_gamma
# ---------------------------------------------------------------------------

def test_market_from_gamma_with_list_token_ids():
    record = {
        "id": "0xcondition1",
        "slug": "btc-up-down-5m-1700000100",
        "clobTokenIds": ["token_up", "token_down"],
        "outcomes": '["Up", "Down"]',
    }
    market = _market_from_gamma(record)
    assert market is not None
    assert market.up_token_id == "token_up"
    assert market.down_token_id == "token_down"
    assert market.condition_id == "0xcondition1"
    assert market.window_start == 1700000100


def test_market_from_gamma_with_json_string_token_ids():
    record = {
        "id": "0xcondition2",
        "slug": "btc-up-down-5m-1700000400",
        "clobTokenIds": '["token_a", "token_b"]',
        "outcomes": '["Up", "Down"]',
    }
    market = _market_from_gamma(record)
    assert market is not None
    assert market.up_token_id == "token_a"
    assert market.down_token_id == "token_b"


def test_market_from_gamma_returns_none_on_missing_token_ids():
    record = {
        "id": "0xcondition3",
        "slug": "btc-up-down-5m-1700000700",
        "outcomes": '["Up", "Down"]',
        # no clobTokenIds
    }
    market = _market_from_gamma(record)
    assert market is None


def test_market_from_gamma_returns_none_on_single_token_id():
    record = {
        "id": "0xcondition4",
        "slug": "btc-up-down-5m-1700001000",
        "clobTokenIds": ["only_one"],
        "outcomes": '["Up"]',
    }
    market = _market_from_gamma(record)
    assert market is None


# ---------------------------------------------------------------------------
# MarketDiscovery.discover (mocked)
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    return {
        "polymarket": {
            "gamma_base": "https://gamma-api.polymarket.com",
            "slug_prefixes": ["btc-up-down-5m", "btc-updown-5m"],
            "search_tag": "btc",
            "search_limit": 20,
        }
    }


def test_discover_finds_current_slug(config):
    disc = MarketDiscovery(config)
    fake_ts = current_window_start()
    fake_market = {
        "id": "0xabc",
        "slug": f"btc-up-down-5m-{fake_ts}",
        "clobTokenIds": ["up_id", "dn_id"],
        "outcomes": '["Up", "Down"]',
    }

    with patch.object(disc, "_query_by_slug", return_value=fake_market):
        result = disc.discover()

    assert result is not None
    assert result.up_token_id == "up_id"
    assert result.discovery_source == "gamma_slug_current"


def test_discover_falls_to_prev_window_slug(config):
    disc = MarketDiscovery(config)
    prev_ts = prev_window_start()
    fake_market = {
        "id": "0xdef",
        "slug": f"btc-up-down-5m-{prev_ts}",
        "clobTokenIds": ["up2", "dn2"],
        "outcomes": '["Up", "Down"]',
    }

    call_count = {"n": 0}

    def mock_query(slug):
        call_count["n"] += 1
        if str(prev_ts) in slug:
            return fake_market
        return None

    with patch.object(disc, "_query_by_slug", side_effect=mock_query):
        result = disc.discover()

    assert result is not None
    assert result.discovery_source == "gamma_slug_prev"


def test_discover_returns_none_when_nothing_found(config):
    disc = MarketDiscovery(config)
    with patch.object(disc, "_query_by_slug", return_value=None):
        with patch.object(disc, "_search_recent", return_value=None):
            result = disc.discover()
    assert result is None
