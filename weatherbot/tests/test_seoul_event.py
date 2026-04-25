"""
Tests for Seoul event — 11-bucket parsing correctness (Patch 5).

Known-positive fixture: https://polymarket.com/event/highest-temperature-in-seoul-on-april-27-2026
11 binary markets: 13°C or below, 14–22°C (exact), 23°C or higher
Rules: Incheon Intl Airport Station RKSI, Wunderground, Celsius, April 27, 2026
"""
import pytest
from datetime import date

from weatherbot.parser import (
    MARKET_TYPE_DAILY_HIGH,
    UNIT_C,
    parse_market,
)
from weatherbot.discovery import RawMarket, _parse_raw_market

_CLOSE = "2026-04-28T00:00:00Z"
_RULES = (
    "Resolved using highest temperature recorded at Incheon Intl Airport Station RKSI. "
    "Source: Wunderground. Temperature in degrees Celsius."
)


def _q(bucket_text: str) -> str:
    return f"Will the highest temperature in Seoul on April 27 be {bucket_text}?"


# ── Exact single-degree buckets ───────────────────────────────────────────────

@pytest.mark.parametrize("temp_c", [14, 15, 16, 17, 18, 19, 20, 21, 22])
def test_exact_bucket_parses(temp_c):
    r = parse_market("s_exact", _q(f"{temp_c}°C"), rules=_RULES, close_time=_CLOSE)
    assert r.parse_failed is False, f"temp={temp_c}: {r.parse_failure_reason}"
    assert r.bucket_low == float(temp_c), f"bucket_low={r.bucket_low}"
    assert r.bucket_high == float(temp_c), f"bucket_high={r.bucket_high}"
    assert r.bucket_label == str(float(temp_c))


@pytest.mark.parametrize("temp_c", [14, 18, 22])
def test_exact_bucket_unit_c(temp_c):
    r = parse_market("s_unit", _q(f"{temp_c}°C"), rules=_RULES, close_time=_CLOSE)
    assert r.unit == UNIT_C, f"temp={temp_c}: unit={r.unit}"


@pytest.mark.parametrize("temp_c", [14, 18, 22])
def test_exact_bucket_market_type(temp_c):
    r = parse_market("s_type", _q(f"{temp_c}°C"), rules=_RULES, close_time=_CLOSE)
    assert r.market_type == MARKET_TYPE_DAILY_HIGH


@pytest.mark.parametrize("temp_c", [14, 18, 22])
def test_exact_bucket_target_date(temp_c):
    r = parse_market("s_date", _q(f"{temp_c}°C"), rules=_RULES, close_time=_CLOSE)
    assert r.parsed_target_date == date(2026, 4, 27), repr(r.parsed_target_date)
    assert r.forecast_blocked_reason is None


# ── Open-ended boundary buckets ───────────────────────────────────────────────

def test_open_ended_low_13_or_below():
    r = parse_market("s_low", _q("13°C or below"), rules=_RULES, close_time=_CLOSE)
    assert r.parse_failed is False
    assert r.unit == UNIT_C
    assert r.bucket_high == 13.0
    assert r.open_ended_low is True
    assert r.open_ended_high is False
    assert "<=" in (r.bucket_label or "")


def test_open_ended_high_23_or_higher():
    r = parse_market("s_high", _q("23°C or higher"), rules=_RULES, close_time=_CLOSE)
    assert r.parse_failed is False
    assert r.unit == UNIT_C
    assert r.bucket_low == 23.0
    assert r.open_ended_high is True
    assert r.open_ended_low is False
    assert ">=" in (r.bucket_label or "")


# ── Station code and source ───────────────────────────────────────────────────

def test_station_code_rksi():
    r = parse_market("s_icao", _q("18°C"), rules=_RULES, close_time=_CLOSE)
    assert r.station_code_from_text == "RKSI"


def test_source_type_wunderground():
    r = parse_market("s_src", _q("18°C"), rules=_RULES, close_time=_CLOSE)
    assert r.source_type == "Wunderground"


def test_wunderground_in_rules_does_not_set_unit_f():
    """Wunderground mention in rules must not cause unit=F when question says °C."""
    r = parse_market("s_uf", _q("18°C"), rules=_RULES, close_time=_CLOSE)
    assert r.unit == UNIT_C, f"Got unit={r.unit}"


# ── Mojibake degree-sign normalization ────────────────────────────────────────

def test_mojibake_a_degree_unit_c():
    """'Â°C' (Latin-1 mis-read of UTF-8 °) must be normalized to °C → unit=C."""
    q = "Will the highest temperature in Seoul on April 27 be 18Â°C?"
    r = parse_market("s_mb1", q, close_time=_CLOSE)
    assert r.unit == UNIT_C, f"unit={r.unit}"


def test_mojibake_box_degree_unit_c():
    """'┬░C' (CP437 mis-read of UTF-8 °) must be normalized to °C → unit=C."""
    q = "Will the highest temperature in Seoul on April 27 be 18┬░C?"
    r = parse_market("s_mb2", q, close_time=_CLOSE)
    assert r.unit == UNIT_C, f"unit={r.unit}"


def test_mojibake_bucket_parses():
    """'18Â°C' must parse to exact bucket 18.0."""
    q = "Will the highest temperature in Seoul on April 27 be 18Â°C?"
    r = parse_market("s_mb3", q, close_time=_CLOSE)
    assert r.parse_failed is False, r.parse_failure_reason
    assert r.bucket_low == 18.0
    assert r.bucket_high == 18.0


# ── Yes/No token mapping ──────────────────────────────────────────────────────

def test_yes_no_token_mapping():
    """First clobTokenId = Yes, second = No."""
    m = {
        "id": "mkt_seoul_14",
        "question": _q("14°C"),
        "active": True,
        "clobTokenIds": '["111111111111111", "222222222222222"]',
    }
    raw = _parse_raw_market(m)
    assert raw is not None
    assert raw.yes_token_id == "111111111111111"
    assert raw.no_token_id == "222222222222222"
    assert raw.token_mapping_failed is False


def test_yes_token_is_token_ids_first():
    """yes_token_id is always token_ids[0]."""
    m = {
        "id": "mkt_seoul_18",
        "question": _q("18°C"),
        "active": True,
        "clobTokenIds": ["999888777666555", "444333222111000"],
    }
    raw = _parse_raw_market(m)
    assert raw is not None
    assert raw.yes_token_id == raw.token_ids[0]
    assert raw.no_token_id == raw.token_ids[1]


# ── n_members=0 blocks PAPER signal ──────────────────────────────────────────

def test_no_paper_signal_when_n_members_zero():
    """EV calculator must not recommend PAPER when n_members=0."""
    from weatherbot.ev_calculator import calculate_ev, ACTION_PAPER_MAKER, ACTION_PAPER_TAKER

    ev = calculate_ev(
        model_probability=0.85,
        nowcast_probability=None,
        nowcast_confidence="none",
        best_bid=0.30,
        best_ask=0.35,
        bid_size=100.0,
        ask_size=100.0,
        spread=0.05,
        ask_depth_top_n=50.0,
        bid_depth_top_n=50.0,
        book_state="two_sided",
        ensemble_agreement=0.9,
        model_spread=1.0,
        settlement_safety=0.80,
        hours_to_close=48.0,
        n_members=0,           # BLOCKED
        deterministic_fallback_used=False,
        forecast_blocked_reason=None,
    )
    assert ev.action not in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER), (
        f"Got {ev.action} with n_members=0"
    )


def test_no_paper_signal_when_deterministic_fallback():
    """Deterministic fallback alone must block PAPER signal."""
    from weatherbot.ev_calculator import calculate_ev, ACTION_PAPER_MAKER, ACTION_PAPER_TAKER

    ev = calculate_ev(
        model_probability=0.85,
        nowcast_probability=None,
        nowcast_confidence="none",
        best_bid=0.30,
        best_ask=0.35,
        bid_size=100.0,
        ask_size=100.0,
        spread=0.05,
        ask_depth_top_n=50.0,
        bid_depth_top_n=50.0,
        book_state="two_sided",
        ensemble_agreement=0.9,
        model_spread=1.0,
        settlement_safety=0.80,
        hours_to_close=48.0,
        n_members=1,
        deterministic_fallback_used=True,   # BLOCKED
        forecast_blocked_reason=None,
    )
    assert ev.action not in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER), (
        f"Got {ev.action} with deterministic_fallback_used=True"
    )
