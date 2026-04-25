"""Tests for discovery.py token/outcome extraction correctness."""
import pytest
from weatherbot.discovery import (
    RawMarket,
    normalize_jsonish_list,
    _extract_token_ids,
    _extract_outcomes,
    _parse_raw_market,
    is_weather_candidate,
)
from weatherbot.runner import _is_valid_token_id


# ── normalize_jsonish_list ─────────────────────────────────────────────────────

def test_normalize_none_returns_empty():
    assert normalize_jsonish_list(None) == []


def test_normalize_already_list():
    assert normalize_jsonish_list(["111", "222"]) == ["111", "222"]


def test_normalize_json_string_list():
    """JSON-encoded string must be parsed, not iterated character-by-character."""
    result = normalize_jsonish_list('["111", "222"]')
    assert result == ["111", "222"], f"got {result!r}"


def test_normalize_json_string_no_bracket_character():
    """'[' must never appear as an element of the result."""
    result = normalize_jsonish_list('["111", "222"]')
    assert "[" not in result


def test_normalize_empty_string_returns_empty():
    assert normalize_jsonish_list("") == []


def test_normalize_plain_string_returns_empty():
    """A plain non-JSON string (no leading '[') should yield []."""
    assert normalize_jsonish_list("sometoken") == []


def test_normalize_single_element_json():
    assert normalize_jsonish_list('["abc"]') == ["abc"]


# ── _extract_token_ids ────────────────────────────────────────────────────────

def test_extract_token_ids_from_json_string():
    """clobTokenIds as JSON string must yield proper token list, not ['[', ...]."""
    market = {"clobTokenIds": '["111111111111", "222222222222"]'}
    result = _extract_token_ids(market)
    assert result == ["111111111111", "222222222222"], f"got {result!r}"
    assert "[" not in result


def test_extract_token_ids_from_list():
    market = {"clobTokenIds": ["111111111111", "222222222222"]}
    result = _extract_token_ids(market)
    assert result == ["111111111111", "222222222222"]


def test_extract_token_ids_from_tokens_objects():
    """Gamma tokens[] array-of-objects format."""
    market = {
        "tokens": [
            {"token_id": "111111111111", "outcome": "Yes"},
            {"token_id": "222222222222", "outcome": "No"},
        ]
    }
    result = _extract_token_ids(market)
    assert result == ["111111111111", "222222222222"]


def test_extract_token_ids_fallback_outcome_prices():
    """outcomePrices dict keys used as last resort."""
    market = {"outcomePrices": {"333333333333": "0.6", "444444444444": "0.4"}}
    result = _extract_token_ids(market)
    assert "333333333333" in result
    assert "444444444444" in result


def test_extract_token_ids_empty_market():
    assert _extract_token_ids({}) == []


# ── _extract_outcomes ─────────────────────────────────────────────────────────

def test_extract_outcomes_json_string():
    """outcomes as JSON string must be parsed."""
    market = {"outcomes": '["25°C", "26°C"]'}
    result = _extract_outcomes(market)
    assert result == ["25°C", "26°C"]


def test_extract_outcomes_list():
    market = {"outcomes": ["Yes", "No"]}
    assert _extract_outcomes(market) == ["Yes", "No"]


# ── token-outcome alignment ───────────────────────────────────────────────────

def _make_market_dict(**kwargs) -> dict:
    base = {
        "id": "mkt1",
        "question": "Will the daily high in Chicago be above 70°F on June 1, 2026?",
        "active": True,
    }
    base.update(kwargs)
    return base


def test_token_mapping_ok_when_lengths_match():
    m = _make_market_dict(
        clobTokenIds=["111111111111", "222222222222"],
        outcomes=["Yes", "No"],
    )
    raw = _parse_raw_market(m)
    assert raw is not None
    assert raw.token_mapping_failed is False


def test_token_mapping_failed_on_length_mismatch():
    """Two token IDs but three outcomes → token_mapping_failed=True."""
    m = _make_market_dict(
        clobTokenIds=["111111111111", "222222222222"],
        outcomes=["<65°F", "65-70°F", ">70°F"],
    )
    raw = _parse_raw_market(m)
    assert raw is not None
    assert raw.token_mapping_failed is True


def test_token_mapping_not_failed_when_tokens_empty():
    """Empty token list + outcomes → not a mismatch (simply no book data)."""
    m = _make_market_dict(clobTokenIds=[], outcomes=["Yes", "No"])
    raw = _parse_raw_market(m)
    assert raw is not None
    assert raw.token_mapping_failed is False


def test_token_ids_never_contain_bracket_from_json_string():
    m = _make_market_dict(
        clobTokenIds='["999999999999", "888888888888"]',
        outcomes=["Yes", "No"],
    )
    raw = _parse_raw_market(m)
    assert raw is not None
    assert "[" not in raw.token_ids
    assert raw.token_ids == ["999999999999", "888888888888"]


# ── _is_valid_token_id (runner guard) ─────────────────────────────────────────

def test_invalid_token_id_bracket():
    assert _is_valid_token_id("[") is False


def test_invalid_token_id_bracket_url_encoded():
    assert _is_valid_token_id("%5B") is False  # URL-encoded "[" is not valid either


def test_invalid_token_id_too_short():
    assert _is_valid_token_id("abc") is False


def test_invalid_token_id_yes_no():
    assert _is_valid_token_id("YES") is False
    assert _is_valid_token_id("NO") is False


def test_invalid_token_id_none_empty():
    assert _is_valid_token_id(None) is False
    assert _is_valid_token_id("") is False


def test_valid_token_id_large_decimal():
    """A realistic Polymarket asset ID (large decimal string) must be accepted."""
    tid = "52114319501245915516055106046884209969926127482827954674443846427813813222426"
    assert _is_valid_token_id(tid) is True


# ── is_weather_candidate ──────────────────────────────────────────────────────

def test_seoul_slug_is_weather():
    assert is_weather_candidate(
        "Will the highest temperature in Seoul on April 27 be above 20°C?",
        slug="highest-temperature-in-seoul-on-april-27-2026",
    ) is True


def test_seoul_slug_only_triggers_weather():
    """Slug alone (even with empty question) must trigger weather candidate."""
    assert is_weather_candidate("", slug="highest-temperature-in-seoul-on-april-27-2026") is True


def test_non_weather_microstrategy():
    assert is_weather_candidate(
        "Will MicroStrategy buy more Bitcoin this week?",
        slug="microstrategy-bitcoin-purchase",
    ) is False


def test_non_weather_ukraine():
    assert is_weather_candidate(
        "Will Ukraine sign a ceasefire agreement in 2025?",
        slug="ukraine-ceasefire-2025",
    ) is False


def test_non_weather_gta():
    assert is_weather_candidate(
        "Will GTA 6 be released before December 2025?",
        slug="gta-6-release-2025",
    ) is False


def test_non_weather_taylor_swift():
    assert is_weather_candidate(
        "Will Taylor Swift release a new album in 2025?",
        slug="taylor-swift-album-2025",
    ) is False


def test_precipitation_question_is_weather():
    assert is_weather_candidate(
        "Will there be more than 10mm of precipitation in Tokyo on April 27?",
    ) is True


def test_wet_word_not_weather():
    """'wet' alone must not classify a market as weather."""
    assert is_weather_candidate(
        "Will the streets be wet after the flood in New Orleans?",
        slug="new-orleans-flood-damage",
    ) is False


def test_flood_word_not_weather():
    """'flood' alone must not classify a market as weather."""
    assert is_weather_candidate(
        "Will the 2025 Mississippi River flood exceed 1993 levels?",
        slug="mississippi-river-flood-1993",
    ) is False
