"""Tests for settlement_safety.py"""
import pytest
from weatherbot.settlement_safety import (
    compute_safety,
    is_candidate_eligible,
)


def _safety(
    city="Chicago",
    market_type="daily_high_temperature",
    source=None,
    risk_kws=None,
    station_risk="low",
    manipulation=False,
    is_precip=False,
    parse_failed=False,
    station_known=True,
):
    return compute_safety(
        city=city,
        market_type=market_type,
        resolution_source_text=source,
        risk_keywords_found=risk_kws or [],
        station_risk=station_risk,
        manipulation_flag=manipulation,
        is_precipitation=is_precip,
        parse_failed=parse_failed,
        station_known=station_known,
    )


def test_clean_market_high_score():
    r = _safety(city="Chicago", station_risk="low", source="aviationweather.gov")
    assert r.score >= 0.80
    assert not r.hard_reject


def test_paris_hard_reject():
    r = _safety(city="Paris")
    assert r.hard_reject is True
    assert r.is_blacklisted is True
    assert r.score <= 0.20


def test_cdg_hard_reject():
    r = _safety(city="CDG")
    assert r.hard_reject is True


def test_le_bourget_hard_reject():
    r = _safety(city="Le Bourget")
    assert r.hard_reject is True


def test_precipitation_hard_reject():
    r = _safety(is_precip=True, market_type="precipitation")
    assert r.hard_reject is True
    assert r.is_precipitation is True


def test_wunderground_hard_reject():
    r = _safety(risk_kws=["wunderground"])
    assert r.hard_reject is True


def test_weather_underground_hard_reject():
    r = _safety(risk_kws=["weather underground"])
    assert r.hard_reject is True


def test_parse_failed_hard_reject():
    r = _safety(parse_failed=True)
    assert r.hard_reject is True


def test_noaa_penalty_not_reject():
    r = _safety(risk_kws=["noaa"], station_risk="low")
    assert not r.hard_reject
    # Small penalty but not disqualifying
    assert r.score >= 0.60


def test_provisional_penalty():
    r1 = _safety()
    r2 = _safety(risk_kws=["provisional"])
    assert r2.score < r1.score


def test_unknown_station_penalty():
    r1 = _safety(station_known=True)
    r2 = _safety(station_known=False)
    assert r2.score < r1.score


def test_high_risk_station_penalty():
    r_low = _safety(station_risk="low")
    r_high = _safety(station_risk="high")
    assert r_high.score < r_low.score


def test_candidate_eligible_above_threshold():
    r = _safety(city="Miami", station_risk="low", source="aviationweather.gov")
    assert is_candidate_eligible(r, min_safety=0.65)


def test_candidate_ineligible_hard_reject():
    r = _safety(city="Paris")
    assert not is_candidate_eligible(r)


def test_candidate_ineligible_low_score():
    r = _safety(
        city="Unknown",
        station_risk="high",
        risk_kws=["provisional", "dispute"],
        station_known=False,
    )
    assert not is_candidate_eligible(r, min_safety=0.65)


def test_paper_ok_always_true():
    r = _safety(city="Paris")
    assert r.paper_ok is True


def test_multiple_risk_keywords_cumulative():
    r_none = _safety()
    r_few = _safety(risk_kws=["noaa"])
    r_many = _safety(risk_kws=["noaa", "provisional", "dispute"])
    assert r_many.score < r_few.score < r_none.score


def test_source_clarity_bonus():
    r_no_src = _safety(source=None)
    r_good_src = _safety(source="aviationweather.gov METAR data")
    r_bad_src = _safety(source="wunderground station KNYC0")
    # Good source better than no source
    assert r_good_src.score > r_no_src.score
    # Bad source: hard reject
    assert r_bad_src.hard_reject is True
