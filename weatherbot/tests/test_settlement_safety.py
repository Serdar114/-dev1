"""Tests for settlement_safety.py — updated for paper/live/blacklist semantics."""
import pytest
from weatherbot.settlement_safety import (
    compute_safety,
    is_candidate_eligible,
    is_live_eligible,
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
    source_type="Unknown",
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
        source_type=source_type,
    )


# ── Hard blacklist (Paris/CDG/Le Bourget) ─────────────────────────────────────

def test_clean_market_high_score():
    r = _safety(city="Chicago", station_risk="low", source="aviationweather.gov")
    assert r.score >= 0.80
    assert not r.hard_blacklist
    assert r.paper_eligible
    assert r.live_eligible


def test_paris_hard_blacklist():
    r = _safety(city="Paris")
    assert r.hard_blacklist is True
    assert r.is_blacklisted is True
    assert r.paper_eligible is False
    assert r.live_eligible is False
    assert r.score <= 0.20


def test_cdg_hard_blacklist():
    r = _safety(city="CDG")
    assert r.hard_blacklist is True
    assert r.paper_eligible is False
    assert r.live_eligible is False


def test_le_bourget_hard_blacklist():
    r = _safety(city="Le Bourget")
    assert r.hard_blacklist is True
    assert r.paper_eligible is False


# ── Precipitation: paper-only, NOT hard blacklist ─────────────────────────────

def test_precipitation_paper_only():
    r = _safety(is_precip=True, market_type="precipitation")
    assert r.hard_blacklist is False        # NOT hard blacklist
    assert r.paper_eligible is True         # paper-OK (log for observation)
    assert r.live_eligible is False         # but no live trade
    assert r.is_precipitation is True
    assert "precipitation" in (r.reject_reason or "")


# ── Wunderground: paper-OK, live-ineligible ───────────────────────────────────

def test_wunderground_paper_ok_live_ineligible():
    r = _safety(risk_kws=["wunderground"])
    assert r.hard_blacklist is False
    assert r.paper_eligible is True
    assert r.live_eligible is False
    assert any("wunderground" in reason for reason in r.live_reject_reasons)


def test_weather_underground_paper_ok_live_ineligible():
    r = _safety(risk_kws=["weather underground"])
    assert r.hard_blacklist is False
    assert r.paper_eligible is True
    assert r.live_eligible is False


# ── Parse failed: paper-OK (log the failure), live-ineligible ────────────────

def test_parse_failed_paper_ok_live_ineligible():
    r = _safety(parse_failed=True)
    assert r.hard_blacklist is False
    assert r.paper_eligible is True
    assert r.live_eligible is False
    assert "parse_failed" in (r.reject_reason or "")


# ── NOAA: penalty but not rejected ───────────────────────────────────────────

def test_noaa_penalty_not_reject():
    r = _safety(risk_kws=["noaa"], station_risk="low")
    assert not r.hard_blacklist
    assert r.score >= 0.60


# ── Score comparisons ─────────────────────────────────────────────────────────

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


# ── Candidate eligibility ─────────────────────────────────────────────────────

def test_candidate_eligible_above_threshold():
    r = _safety(city="Miami", station_risk="low", source="aviationweather.gov")
    assert is_candidate_eligible(r, min_safety=0.65)


def test_candidate_ineligible_hard_blacklist():
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


def test_live_eligible_clean_market():
    r = _safety(city="Chicago", station_risk="low", source="aviationweather.gov")
    assert is_live_eligible(r, min_safety=0.65)


def test_live_ineligible_wunderground():
    r = _safety(risk_kws=["wunderground"])
    assert not is_live_eligible(r)


def test_live_ineligible_precipitation():
    r = _safety(is_precip=True)
    assert not is_live_eligible(r)


# ── Paris: paper_eligible=False (hard blacklist) ──────────────────────────────

def test_paris_not_paper_eligible():
    r = _safety(city="Paris")
    assert r.paper_eligible is False   # hard blacklist → no paper either


# ── Cumulative keywords ───────────────────────────────────────────────────────

def test_multiple_risk_keywords_cumulative():
    r_none = _safety()
    r_few = _safety(risk_kws=["noaa"])
    r_many = _safety(risk_kws=["noaa", "provisional", "dispute"])
    assert r_many.score < r_few.score < r_none.score


# ── Source clarity ────────────────────────────────────────────────────────────

def test_source_clarity_bonus_aviationweather():
    r_no_src = _safety(source=None)
    r_good_src = _safety(source="aviationweather.gov METAR data")
    assert r_good_src.score > r_no_src.score


def test_wunderground_source_live_ineligible():
    r = _safety(source="wunderground station KNYC0")
    # wunderground in source → live ineligible, paper OK, NOT hard_blacklist
    assert r.hard_blacklist is False
    assert r.paper_eligible is True
    assert r.live_eligible is False
