"""Tests for parser.py"""
import pytest
from weatherbot.parser import (
    MARKET_TYPE_DAILY_HIGH,
    MARKET_TYPE_DAILY_LOW,
    MARKET_TYPE_PRECIPITATION,
    MARKET_TYPE_UNKNOWN,
    UNIT_C,
    UNIT_F,
    parse_market,
)


def test_daily_high_basic_range():
    q = "Will the daily high temperature in Chicago on April 25, 2025 be between 60°F and 69°F?"
    r = parse_market("m1", q)
    assert r.market_type == MARKET_TYPE_DAILY_HIGH
    assert r.unit == UNIT_F
    assert r.bucket_low == 60.0
    assert r.bucket_high == 69.0
    assert not r.open_ended_low
    assert not r.open_ended_high
    assert r.parse_failed is False
    assert r.city == "Chicago"


def test_daily_high_open_ended_above():
    q = "Will the daily high temperature in Miami reach at least 90°F on July 4, 2025?"
    r = parse_market("m2", q)
    assert r.market_type == MARKET_TYPE_DAILY_HIGH
    assert r.unit == UNIT_F
    assert r.bucket_low == 90.0
    assert r.open_ended_high is True
    assert r.open_ended_low is False
    assert r.parse_failed is False


def test_daily_low_open_ended_below():
    q = "Will the daily low temperature in New York City be below 32°F on January 15, 2025?"
    r = parse_market("m3", q)
    assert r.market_type == MARKET_TYPE_DAILY_LOW
    assert r.unit == UNIT_F
    assert r.bucket_high == 32.0
    assert r.open_ended_low is True
    assert r.parse_failed is False


def test_celsius_market():
    q = "Will the daily high temperature in Tokyo be between 25°C and 29°C on August 1, 2025?"
    r = parse_market("m4", q)
    assert r.unit == UNIT_C
    assert r.bucket_low == 25.0
    assert r.bucket_high == 29.0
    assert r.parse_failed is False


def test_precipitation_detection():
    q = "Will rainfall in New York exceed 1 inch on April 10, 2025?"
    r = parse_market("m5", q)
    assert r.market_type == MARKET_TYPE_PRECIPITATION
    assert r.is_precipitation is True


def test_precipitation_explicit():
    q = "Will there be more than 2mm of precipitation in London on March 5, 2025?"
    r = parse_market("m6", q)
    assert r.is_precipitation is True


def test_paris_manipulation_flag():
    q = "Will the daily high temperature in Paris be between 20°C and 24°C on June 15, 2025?"
    r = parse_market("m7", q)
    assert r.manipulation_flag is True


def test_cdg_manipulation_flag():
    q = "Will the high temperature at CDG airport be above 30°C on July 10, 2025?"
    r = parse_market("m8", q)
    assert r.manipulation_flag is True


def test_wunderground_risk_keyword():
    q = "Will the daily high temperature in Dallas be above 100°F? Source: Weather Underground station."
    r = parse_market("m9", q)
    assert "wunderground" in [k.lower() for k in r.risk_keywords_found] or \
           "weather underground" in [k.lower() for k in r.risk_keywords_found]


def test_noaa_risk_keyword():
    q = "Will the high in Chicago be between 70°F and 79°F? Resolved using NOAA data."
    r = parse_market("m10", q)
    assert "noaa" in [k.lower() for k in r.risk_keywords_found]


def test_parse_failed_no_bucket():
    q = "Will the weather be nice tomorrow?"
    r = parse_market("m11", q)
    assert r.parse_failed is True


def test_or_higher_pattern():
    q = "Will the daily high in Atlanta be 95°F or higher on August 20, 2025?"
    r = parse_market("m12", q)
    assert r.bucket_low == 95.0
    assert r.open_ended_high is True
    assert r.parse_failed is False


def test_or_lower_pattern():
    q = "Will the daily low in Minneapolis be 0°F or lower on December 15, 2025?"
    r = parse_market("m13", q)
    assert r.bucket_high == 0.0
    assert r.open_ended_low is True
    assert r.parse_failed is False


def test_date_extraction():
    q = "Will the daily high in Houston be between 85°F and 94°F on 2025-06-15?"
    r = parse_market("m14", q)
    assert r.date_str is not None
    assert "2025" in r.date_str


def test_le_bourget_detection():
    q = "Will the temperature at Le Bourget be above 28°C on July 20?"
    r = parse_market("m15", q)
    assert r.manipulation_flag is True


def test_market_type_unknown_non_temperature():
    q = "Will it be windy in Chicago on March 10, 2025?"
    r = parse_market("m16", q)
    assert r.market_type == MARKET_TYPE_UNKNOWN
    assert r.parse_failed is True


def test_outcomes_fallback():
    q = "Will the temperature in Chicago be between 70 and 79 degrees Fahrenheit on April 1?"
    outcomes = ["70°F - 79°F", "Below 70°F", "80°F or above"]
    r = parse_market("m17", q, outcomes)
    assert r.parse_failed is False
    assert r.unit == UNIT_F


def test_bucket_negative_temp():
    q = "Will the daily low in Chicago be between -5°F and 4°F on January 10, 2025?"
    r = parse_market("m18", q)
    assert r.bucket_low == -5.0
    assert r.bucket_high == 4.0
    assert r.parse_failed is False


def test_empty_question():
    r = parse_market("m19", "")
    assert r.parse_failed is True


def test_parse_never_crashes():
    """Parser must not crash on any input."""
    weird_inputs = [
        "None",
        "🌡️ temperature!!! ???",
        "Will the 999999°F happen?",
        "A" * 1000,
        "between and between",
    ]
    for i, q in enumerate(weird_inputs):
        r = parse_market(f"weird_{i}", q)
        assert r is not None  # never crashes


# ── ICAO extraction ───────────────────────────────────────────────────────────

def test_icao_not_extracted_from_question_word():
    """'WILL' from 'Will the highest temperature...' must not be returned as ICAO."""
    q = "Will the highest temperature in Hong Kong on April 26 be 29°C?"
    r = parse_market("icao1", q)
    assert r.station_code_from_text is None, (
        f"Expected None but got {r.station_code_from_text!r}"
    )


def test_icao_extracted_from_metar_station_context():
    """VHHH accepted when it appears near 'METAR station' in resolution text."""
    r = parse_market(
        "icao2",
        "Will the daily high temperature in Hong Kong be above 30°C on April 26?",
        resolution_text="Weather reported by METAR station VHHH",
    )
    assert r.station_code_from_text == "VHHH"


def test_icao_extracted_from_airport_station_context():
    """KLGA accepted when it appears near 'Airport station' in rules text."""
    r = parse_market(
        "icao3",
        "Will the daily high in New York City be above 70°F on June 1, 2026?",
        rules="Resolves using LaGuardia Airport station KLGA data.",
    )
    assert r.station_code_from_text == "KLGA"


def test_icao_denylist_words_rejected():
    """Common English words that match ICAO prefix must be rejected."""
    for word in ("WILL", "HIGH", "TEMP", "DATE", "CITY", "THIS", "THAT", "OVER", "LESS", "MORE"):
        q = f"Will the temperature be {word} degrees Fahrenheit on July 4, 2026?"
        r = parse_market(f"deny_{word}", q)
        assert r.station_code_from_text != word, (
            f"Denylist word {word!r} was incorrectly returned as ICAO"
        )
