"""Tests for discovery.py token/outcome extraction correctness."""
import pytest
from weatherbot.discovery import (
    RawMarket,
    normalize_jsonish_list,
    normalize_text_items,
    _extract_token_ids,
    _extract_outcomes,
    _is_weather_market,
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


# ── Endpoint order for fetch_event_by_slug ───────────────────────────────────

def test_fetch_event_by_slug_tries_slug_endpoint_first(monkeypatch):
    """fetch_event_by_slug must try GET /events/slug/{slug} before any other endpoint."""
    called_urls: list[str] = []

    def mock_get(url, params, **kwargs):
        called_urls.append(url)
        return None  # all fail — we just want to observe call order

    monkeypatch.setattr("weatherbot.discovery._get", mock_get)
    from weatherbot.discovery import fetch_event_by_slug
    fetch_event_by_slug("highest-temperature-in-seoul-on-april-27-2026")

    assert len(called_urls) >= 1, "Expected at least one HTTP call"
    assert called_urls[0].endswith("/events/slug/highest-temperature-in-seoul-on-april-27-2026"), (
        f"First call must be /events/slug/{{slug}}, got: {called_urls[0]}"
    )


def test_fetch_event_by_slug_falls_back_to_events_query(monkeypatch):
    """If /events/slug/ returns None, try GET /events?slug=."""
    called_urls: list[str] = []

    def mock_get(url, params, **kwargs):
        called_urls.append(url)
        return None

    monkeypatch.setattr("weatherbot.discovery._get", mock_get)
    from weatherbot.discovery import fetch_event_by_slug
    fetch_event_by_slug("some-slug")

    urls_str = " ".join(called_urls)
    assert "/events/slug/" in urls_str, "Expected /events/slug/ call"
    assert any("/events" in u and "slug" not in u.split("?")[0].split("/")[-1]
               for u in called_urls), "Expected fallback /events call"


# ── Broad discovery weather filter ───────────────────────────────────────────

def test_temperature_slug_passes_weather_filter():
    """Temperature event slugs must pass is_weather_candidate."""
    assert is_weather_candidate(
        "Will the highest temperature in Tokyo on July 4 be above 30°C?",
        slug="highest-temperature-in-tokyo-july-4-2026",
    ) is True


def test_crypto_event_fails_weather_filter():
    """Bitcoin/crypto slugs must not pass weather filter."""
    assert is_weather_candidate(
        "Will Bitcoin reach $100,000 by end of 2026?",
        slug="bitcoin-100k-2026",
    ) is False


def test_election_event_fails_weather_filter():
    """Election event slugs must not pass weather filter."""
    assert is_weather_candidate(
        "Will the Democratic candidate win the 2026 midterms?",
        slug="democrats-midterms-2026",
    ) is False


# ── WeatherEventSummary and _make_event_summary ───────────────────────────────

def test_make_event_summary_fields():
    """_make_event_summary returns correct fields from a fake event+markets pair."""
    from weatherbot.discovery import WeatherEventSummary, _make_event_summary, _parse_raw_market

    fake_event = {
        "id": "evt_seoul_test",
        "slug": "highest-temperature-in-seoul-on-april-27-2026",
        "title": "Highest Temperature in Seoul on April 27, 2026",
        "endDate": "2026-04-28T00:00:00Z",
        "markets": [],
    }
    fake_market_dict = {
        "id": "mkt_test_001",
        "question": "Will the highest temperature in Seoul on April 27 be 18°C?",
        "active": True,
        "clobTokenIds": ["111111111111111", "222222222222222"],
        "endDate": "2026-04-28T00:00:00Z",
    }
    market = _parse_raw_market(fake_market_dict)
    assert market is not None

    summary = _make_event_summary(fake_event, [market])
    assert isinstance(summary, WeatherEventSummary)
    assert summary.event_id == "evt_seoul_test"
    assert summary.slug == "highest-temperature-in-seoul-on-april-27-2026"
    assert summary.n_markets == 1
    assert summary.close_time == "2026-04-28T00:00:00Z"


# ── Model sanity (_compute_model_sanity in runner) ────────────────────────────

def test_model_sanity_outside_range():
    """All members above bucket_high → model_distribution_outside_bucket_range=True."""
    from weatherbot.runner import _compute_model_sanity
    sanity = _compute_model_sanity(
        daily_extreme_values=[30.0, 31.0, 32.0],
        bucket_low=14.0, bucket_high=14.0,
        open_ended_low=False, open_ended_high=False,
    )
    assert sanity["model_distribution_outside_bucket_range"] is True
    assert sanity["members_inside_bucket"] == 0


def test_model_sanity_inside_range():
    """One member exactly in exact bucket → outside_range=False."""
    from weatherbot.runner import _compute_model_sanity
    sanity = _compute_model_sanity(
        daily_extreme_values=[13.0, 14.0, 15.0],
        bucket_low=14.0, bucket_high=14.0,
        open_ended_low=False, open_ended_high=False,
    )
    assert sanity["model_distribution_outside_bucket_range"] is False
    assert sanity["members_inside_bucket"] == 1


def test_model_sanity_open_ended_high():
    """Open-ended high: all members >= bucket_low count as inside."""
    from weatherbot.runner import _compute_model_sanity
    sanity = _compute_model_sanity(
        daily_extreme_values=[22.0, 23.0, 25.0],
        bucket_low=23.0, bucket_high=None,
        open_ended_low=False, open_ended_high=True,
    )
    assert sanity["members_inside_bucket"] == 2
    assert sanity["model_distribution_outside_bucket_range"] is False


def test_model_sanity_empty_members():
    """Empty members → all None stats, outside_range=True."""
    from weatherbot.runner import _compute_model_sanity
    sanity = _compute_model_sanity([], None, None, False, False)
    assert sanity["model_distribution_outside_bucket_range"] is True
    assert sanity["member_min"] is None
    assert sanity["members_inside_bucket"] == 0


def test_model_sanity_open_ended_low():
    """Open-ended low: all members <= bucket_high count as inside."""
    from weatherbot.runner import _compute_model_sanity
    sanity = _compute_model_sanity(
        daily_extreme_values=[10.0, 11.0, 13.0, 14.0],
        bucket_low=None, bucket_high=13.0,
        open_ended_low=True, open_ended_high=False,
    )
    assert sanity["members_inside_bucket"] == 3
    assert sanity["model_distribution_outside_bucket_range"] is False


def test_model_sanity_percentile_stats():
    """Distribution stats are computed correctly for a known list."""
    from weatherbot.runner import _compute_model_sanity
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    sanity = _compute_model_sanity(
        daily_extreme_values=vals,
        bucket_low=25.0, bucket_high=35.0,
        open_ended_low=False, open_ended_high=False,
    )
    assert sanity["member_min"] == 10.0
    assert sanity["member_max"] == 50.0
    assert sanity["members_inside_bucket"] == 1  # only 30.0
    assert sanity["model_distribution_outside_bucket_range"] is False


# ── normalize_text_items ──────────────────────────────────────────────────────

def test_normalize_text_items_none():
    assert normalize_text_items(None) == []


def test_normalize_text_items_string():
    assert normalize_text_items("Weather") == ["weather"]


def test_normalize_text_items_empty_string():
    assert normalize_text_items("") == []


def test_normalize_text_items_list_of_strings():
    assert normalize_text_items(["Weather", "Temperature"]) == ["weather", "temperature"]


def test_normalize_text_items_list_of_dicts():
    """Dicts with label/name/slug keys are extracted."""
    result = normalize_text_items([{"label": "Weather", "slug": "weather"}])
    assert "weather" in result


def test_normalize_text_items_dict_name():
    result = normalize_text_items({"name": "Weather"})
    assert result == ["weather"]


def test_normalize_text_items_mixed_list():
    """Mixed list with strings, dicts, and bad types must not crash."""
    result = normalize_text_items(["Weather", {"slug": "temperature"}, 123, None])
    assert "weather" in result
    assert "temperature" in result


def test_normalize_text_items_bad_types_no_crash():
    """Numbers, None, dicts with no useful keys — must not crash."""
    result = normalize_text_items([123, None, {"x": "bad"}, True])
    assert isinstance(result, list)


def test_normalize_text_items_dict_no_useful_keys():
    """Dict with only unknown keys returns empty."""
    result = normalize_text_items({"foo": "bar", "baz": 42})
    assert result == []


# ── _is_weather_market with dict tags ────────────────────────────────────────

def _weather_event(extra: dict) -> dict:
    base = {
        "slug": "some-event",
        "title": "Some Event",
        "question": "",
        "markets": [],
    }
    base.update(extra)
    return base


def test_is_weather_market_tags_dict_label_weather():
    """tags=[{"label":"Weather","slug":"weather"}] must classify as weather."""
    m = _weather_event({"tags": [{"label": "Weather", "slug": "weather"}]})
    assert _is_weather_market(m) is True


def test_is_weather_market_tags_dict_name_weather():
    """tags=[{"name":"Temperature"}] must classify as weather via WEATHER_TAGS."""
    m = _weather_event({"tags": [{"name": "Temperature"}]})
    assert _is_weather_market(m) is True


def test_is_weather_market_tags_dict_crypto_no_title_match():
    """tags=[{"name":"Crypto"}] with no weather in title/slug must return False."""
    m = _weather_event({"tags": [{"name": "Crypto"}]})
    assert _is_weather_market(m) is False


def test_is_weather_market_category_dict():
    """category={"name":"Weather"} must classify as weather."""
    m = _weather_event({"category": {"name": "Weather"}})
    assert _is_weather_market(m) is True


def test_is_weather_market_mixed_tags_string_and_dict():
    """Mixed tags ["Sports", {"slug":"temperature"}] triggers weather via dict slug."""
    m = _weather_event({"tags": ["Sports", {"slug": "temperature"}]})
    assert _is_weather_market(m) is True


def test_is_weather_market_bad_tags_no_crash():
    """tags=[123, None, {"x":"bad"}] must not raise — returns False (no match)."""
    m = _weather_event({"tags": [123, None, {"x": "bad"}]})
    result = _is_weather_market(m)
    assert isinstance(result, bool)


def test_is_weather_market_temperature_in_slug():
    """Slug containing 'highest-temperature-in-' must always be weather regardless of tags."""
    m = _weather_event({
        "slug": "highest-temperature-in-tokyo-april-2026",
        "tags": [{"name": "Crypto"}],
    })
    assert _is_weather_market(m) is True


def test_parse_raw_market_dict_tags_normalised():
    """_parse_raw_market with dict tags must produce normalised string list, not crash."""
    m = {
        "id": "mkt_tag_test",
        "question": "Will the daily high in London be above 20°C?",
        "active": True,
        "tags": [{"label": "Weather", "slug": "weather"}, {"name": "Temperature"}],
        "clobTokenIds": ["111111111111111", "222222222222222"],
    }
    raw = _parse_raw_market(m)
    assert raw is not None
    assert isinstance(raw.tags, list)
    assert all(isinstance(t, str) for t in raw.tags)
    assert "weather" in raw.tags or "temperature" in raw.tags
