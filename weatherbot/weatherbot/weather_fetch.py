"""
External weather data fetch layer.

V1 sources (all free, no scraping):
  - Open-Meteo Ensemble API for forecast probability distributions
  - AviationWeather.gov METAR for station observations
  - Open-Meteo historical for previous model runs

All calls: timeout + retry + error log. Never crash runner.
"""
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

OPEN_METEO_ENSEMBLE_BASE = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_FORECAST_BASE = "https://api.open-meteo.com/v1/forecast"
AVIATIONWEATHER_METAR_BASE = "https://aviationweather.gov/api/data/metar"

DEFAULT_TIMEOUT = 20
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2.0

ENSEMBLE_MODELS = ["icon_seamless", "gfs_seamless", "ecmwf_ifs025"]
ENSEMBLE_FALLBACK = ["icon_global", "gfs025"]


def _get(
    url: str,
    params: dict,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> Optional[Any]:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("GET %s attempt %d/%d: %s", url, attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    logger.error("All %d attempts failed for %s", retries, url)
    return None


def fetch_open_meteo_ensemble(
    lat: float,
    lon: float,
    target_date: date,
    models: Optional[list[str]] = None,
) -> Optional[dict]:
    """
    Fetch hourly ensemble temperature forecasts from Open-Meteo.

    Returns raw JSON or None on failure.
    """
    if models is None:
        models = ENSEMBLE_MODELS

    start = target_date.isoformat()
    end = target_date.isoformat()

    # Try primary models, fall back to alternatives
    for model in models + ENSEMBLE_FALLBACK:
        params = {
            "latitude": lat,
            "longitude": lon,
            "models": model,
            "hourly": "temperature_2m",
            "start_date": start,
            "end_date": end,
            "wind_speed_unit": "ms",
            "temperature_unit": "celsius",
            "timezone": "UTC",
        }
        data = _get(OPEN_METEO_ENSEMBLE_BASE, params=params)
        if data and "hourly" in data:
            data["_model_used"] = model
            logger.info("Ensemble fetch OK: model=%s lat=%.3f lon=%.3f date=%s", model, lat, lon, start)
            return data
        logger.warning("Ensemble fetch failed for model=%s, trying next", model)

    logger.error("All ensemble models failed for lat=%.3f lon=%.3f date=%s", lat, lon, start)
    return None


def fetch_open_meteo_forecast(
    lat: float,
    lon: float,
    target_date: date,
) -> Optional[dict]:
    """
    Fetch deterministic hourly forecast from Open-Meteo (non-ensemble).
    Used as fallback when ensemble fails.
    """
    start = target_date.isoformat()
    end = target_date.isoformat()
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "daily": "temperature_2m_max,temperature_2m_min",
        "start_date": start,
        "end_date": end,
        "temperature_unit": "celsius",
        "timezone": "UTC",
        "forecast_days": 1,
    }
    data = _get(OPEN_METEO_FORECAST_BASE, params=params)
    if data:
        data["_model_used"] = "open_meteo_deterministic"
    return data


def fetch_station_observation(icao: str) -> Optional[dict]:
    """
    Fetch latest METAR observation from AviationWeather API.

    Returns parsed observation dict or None.
    """
    if not icao:
        return None

    params = {
        "ids": icao.upper(),
        "format": "json",
        "hours": 3,
    }
    data = _get(AVIATIONWEATHER_METAR_BASE, params=params)
    if not data:
        return None

    if isinstance(data, list) and len(data) > 0:
        obs = data[0]
        return _normalize_metar_obs(obs)
    elif isinstance(data, dict):
        return _normalize_metar_obs(data)

    return None


def _normalize_metar_obs(obs: dict) -> dict:
    """Normalize AviationWeather METAR JSON to our schema."""
    temp_c = obs.get("temp")
    dewpoint_c = obs.get("dewp")

    if temp_c is None:
        # Some responses use different keys
        temp_c = obs.get("temperature") or obs.get("air_temp")

    try:
        temp_c = float(temp_c) if temp_c is not None else None
    except (TypeError, ValueError):
        temp_c = None

    return {
        "icao": obs.get("icaoId") or obs.get("station_id") or obs.get("stationId") or "",
        "obs_time_utc": obs.get("reportTime") or obs.get("obsTime") or obs.get("time") or "",
        "temp_c": temp_c,
        "dewpoint_c": float(dewpoint_c) if dewpoint_c is not None else None,
        "wind_speed_kt": obs.get("wspd"),
        "wind_dir": obs.get("wdir"),
        "visibility_sm": obs.get("visib"),
        "sky_cover": obs.get("sky") or obs.get("clouds"),
        "raw_metar": obs.get("rawOb") or obs.get("raw_text") or "",
        "_source": "aviationweather_metar",
    }


def fetch_previous_runs_if_available(
    lat: float,
    lon: float,
    target_date: date,
    lookback_days: int = 2,
) -> list[dict]:
    """
    Fetch historical Open-Meteo ensemble data for prior model run dates.
    Useful for tracking model consistency / stale-quote detection.

    Returns list of run results (may be empty on failure).
    """
    results = []
    for delta in range(1, lookback_days + 1):
        run_date = target_date - timedelta(days=delta)
        data = fetch_open_meteo_ensemble(lat, lon, target_date)
        if data:
            data["_run_date"] = run_date.isoformat()
            results.append(data)
    return results


def celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def fahrenheit_to_celsius(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def normalize_units(value: float, from_unit: str, to_unit: str) -> float:
    """Convert temperature between F and C."""
    if from_unit == to_unit:
        return value
    if from_unit == "C" and to_unit == "F":
        return celsius_to_fahrenheit(value)
    if from_unit == "F" and to_unit == "C":
        return fahrenheit_to_celsius(value)
    return value


def extract_hourly_temps_from_ensemble(data: dict) -> Optional[list[float]]:
    """
    Extract flattened list of temperature values from Open-Meteo ensemble JSON.

    Open-Meteo ensemble returns temperature_2m as either:
    - A list (deterministic) or
    - A dict of member arrays: {"member01": [...], "member02": [...], ...}
    """
    hourly = data.get("hourly") or {}
    temp_data = hourly.get("temperature_2m")

    if temp_data is None:
        return None

    if isinstance(temp_data, list):
        return [float(v) for v in temp_data if v is not None]

    if isinstance(temp_data, dict):
        all_vals = []
        for member_vals in temp_data.values():
            if isinstance(member_vals, list):
                all_vals.extend(v for v in member_vals if v is not None)
        return [float(v) for v in all_vals] if all_vals else None

    return None


def extract_ensemble_members(data: dict) -> Optional[dict[str, list[float]]]:
    """
    Extract per-member hourly temperature arrays.

    Returns {member_id: [temp_hour0, temp_hour1, ...]} or None.
    """
    hourly = data.get("hourly") or {}
    temp_data = hourly.get("temperature_2m")

    if temp_data is None:
        return None

    if isinstance(temp_data, dict):
        result = {}
        for k, v in temp_data.items():
            if isinstance(v, list):
                result[k] = [float(x) for x in v if x is not None]
        return result if result else None

    return None


def get_current_temp_c(icao: str) -> Optional[float]:
    """Convenience: fetch latest obs and return temp in Celsius."""
    obs = fetch_station_observation(icao)
    if obs:
        return obs.get("temp_c")
    return None
