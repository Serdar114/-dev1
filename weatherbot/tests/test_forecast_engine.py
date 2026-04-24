"""Tests for forecast_engine.py"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import date

from weatherbot.forecast_engine import (
    ForecastResult,
    _compute_daily_max,
    _compute_daily_min,
    _in_bucket,
    _laplace_probability,
    _stddev,
    compute_forecast,
)


def test_compute_daily_max():
    assert _compute_daily_max([10, 20, 15, 5]) == 20.0
    assert _compute_daily_max([-5, -10, -2]) == -2.0
    assert _compute_daily_max([]) is None


def test_compute_daily_min():
    assert _compute_daily_min([10, 20, 15, 5]) == 5.0
    assert _compute_daily_min([-5, -10, -2]) == -10.0
    assert _compute_daily_min([]) is None


def test_in_bucket_range():
    assert _in_bucket(75.0, 70.0, 79.0, False, False) is True
    assert _in_bucket(70.0, 70.0, 79.0, False, False) is True
    assert _in_bucket(79.0, 70.0, 79.0, False, False) is True
    assert _in_bucket(80.0, 70.0, 79.0, False, False) is False
    assert _in_bucket(69.9, 70.0, 79.0, False, False) is False


def test_in_bucket_open_high():
    # ">= 90"
    assert _in_bucket(90.0, 90.0, None, False, True) is True
    assert _in_bucket(95.0, 90.0, None, False, True) is True
    assert _in_bucket(89.9, 90.0, None, False, True) is False


def test_in_bucket_open_low():
    # "< 32"
    assert _in_bucket(31.0, None, 32.0, True, False) is True
    assert _in_bucket(32.0, None, 32.0, True, False) is True
    assert _in_bucket(33.0, None, 32.0, True, False) is False


def test_laplace_probability_smoothing():
    # With Laplace smoothing, should never be 0 or 1
    p_all_in = _laplace_probability(50, 50, alpha=0.5, pseudo_n=50)
    p_none_in = _laplace_probability(0, 50, alpha=0.5, pseudo_n=50)
    assert 0 < p_none_in < 0.05   # very low but not 0
    assert 0.95 < p_all_in < 1.0  # very high but not 1


def test_laplace_symmetry():
    p_half = _laplace_probability(25, 50, alpha=0.5, pseudo_n=50)
    assert abs(p_half - 0.5) < 0.05


def test_stddev():
    assert abs(_stddev([2, 4, 4, 4, 5, 5, 7, 9]) - 2.0) < 0.01
    assert _stddev([5]) == 0.0
    assert _stddev([]) == 0.0


def test_compute_forecast_no_coordinates():
    result = compute_forecast(
        lat=None,
        lon=None,
        target_date=date(2025, 7, 4),
        market_type="daily_high_temperature",
        unit="F",
        bucket_low=85.0,
        bucket_high=94.0,
        open_ended_low=False,
        open_ended_high=False,
    )
    assert result.error == "no_coordinates"
    assert result.model_probability == 0.5


def test_compute_forecast_fetch_failure():
    """When both ensemble and deterministic fetch fail, return 0.5 probability."""
    with patch("weatherbot.forecast_engine.fetch_open_meteo_ensemble", return_value=None), \
         patch("weatherbot.forecast_engine.fetch_open_meteo_forecast", return_value=None):
        result = compute_forecast(
            lat=41.9742,
            lon=-87.9073,
            target_date=date(2025, 7, 4),
            market_type="daily_high_temperature",
            unit="F",
            bucket_low=85.0,
            bucket_high=94.0,
            open_ended_low=False,
            open_ended_high=False,
        )
    assert result.error == "all_fetches_failed"
    assert result.model_probability == 0.5


def test_compute_forecast_with_mock_ensemble():
    """Test full forecast pipeline with mocked ensemble data."""
    mock_data = {
        "_model_used": "icon_seamless",
        "hourly": {
            "temperature_2m": {
                "member01": [25.0, 27.0, 29.5, 31.0, 30.0, 28.0, 26.0, 24.0] * 3,
                "member02": [24.0, 26.0, 28.5, 30.0, 29.0, 27.0, 25.0, 23.0] * 3,
                "member03": [26.0, 28.0, 30.5, 32.0, 31.0, 29.0, 27.0, 25.0] * 3,
                "member04": [23.0, 25.0, 27.5, 29.0, 28.0, 26.0, 24.0, 22.0] * 3,
                "member05": [27.0, 29.0, 31.5, 33.0, 32.0, 30.0, 28.0, 26.0] * 3,
            }
        },
    }
    with patch("weatherbot.forecast_engine.fetch_open_meteo_ensemble", return_value=mock_data):
        result = compute_forecast(
            lat=35.5533,
            lon=139.7811,
            target_date=date(2025, 7, 4),
            market_type="daily_high_temperature",
            unit="C",
            bucket_low=29.0,
            bucket_high=33.0,
            open_ended_low=False,
            open_ended_high=False,
        )
    assert result.error is None
    assert result.n_members == 5
    assert 0 < result.model_probability < 1
    assert result.model_spread >= 0
    assert result.ensemble_agreement >= 0


def test_forecast_unit_conversion():
    """Ensemble is in Celsius; F markets need conversion."""
    mock_data = {
        "_model_used": "gfs_seamless",
        "hourly": {
            "temperature_2m": {
                "member01": [26.7] * 24,  # 26.7°C ≈ 80°F
                "member02": [27.2] * 24,  # 27.2°C ≈ 81°F
                "member03": [26.1] * 24,  # 26.1°C ≈ 79°F
            }
        },
    }
    with patch("weatherbot.forecast_engine.fetch_open_meteo_ensemble", return_value=mock_data):
        result = compute_forecast(
            lat=41.8,
            lon=-87.9,
            target_date=date(2025, 6, 1),
            market_type="daily_high_temperature",
            unit="F",
            bucket_low=78.0,
            bucket_high=84.0,
            open_ended_low=False,
            open_ended_high=False,
        )
    assert result.n_members == 3
    # All ~80°F values should be in [78, 84] bucket
    assert result.n_members_in_bucket == 3
    assert result.model_probability > 0.85


def test_bias_correction_applied():
    mock_data = {
        "_model_used": "icon_seamless",
        "hourly": {
            "temperature_2m": {
                "member01": [26.0] * 24,
                "member02": [26.5] * 24,
            }
        },
    }
    bias_cfg = {
        "KLGA": {
            "daily_high_bias_F": 3.0,  # model runs 3°F cold → add 3°F
        }
    }
    with patch("weatherbot.forecast_engine.fetch_open_meteo_ensemble", return_value=mock_data):
        result_no_bias = compute_forecast(
            lat=40.78, lon=-73.88,
            target_date=date(2025, 8, 1),
            market_type="daily_high_temperature",
            unit="F",
            bucket_low=80.0, bucket_high=85.0,
            open_ended_low=False, open_ended_high=False,
            icao="KLGA",
            bias_config=None,
        )
        result_with_bias = compute_forecast(
            lat=40.78, lon=-73.88,
            target_date=date(2025, 8, 1),
            market_type="daily_high_temperature",
            unit="F",
            bucket_low=80.0, bucket_high=85.0,
            open_ended_low=False, open_ended_high=False,
            icao="KLGA",
            bias_config=bias_cfg,
        )
    assert result_with_bias.bias_applied == 3.0
    assert result_no_bias.bias_applied == 0.0
