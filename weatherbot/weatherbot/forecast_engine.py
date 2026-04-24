"""
Forecast engine: compute bucket probability from Open-Meteo ensemble members.

Each ensemble member contributes one daily max or min temperature.
We count members inside the bucket, apply Laplace smoothing, and produce:
  - model_probability
  - ensemble_agreement
  - model_spread (std dev of ensemble member daily extremes)

Bias correction applied per station/city if configured.
"""
import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .weather_fetch import (
    extract_ensemble_members,
    fetch_open_meteo_ensemble,
    fetch_open_meteo_forecast,
    normalize_units,
)

logger = logging.getLogger(__name__)

# Laplace smoothing defaults
DEFAULT_LAPLACE_ALPHA = 0.5
DEFAULT_LAPLACE_N = 50  # pseudo total count for denominator


@dataclass
class ForecastResult:
    model_probability: float
    ensemble_agreement: float  # fraction of members in bucket / total members
    model_spread: float        # std dev of daily extremes across members
    n_members: int
    n_members_in_bucket: int
    daily_extreme_values: list[float]  # per-member daily max or min
    model_used: Optional[str]
    bias_applied: float         # degrees applied to raw values before bucketing
    error: Optional[str] = None


def _compute_daily_max(hourly_temps: list[float]) -> Optional[float]:
    if not hourly_temps:
        return None
    return max(hourly_temps)


def _compute_daily_min(hourly_temps: list[float]) -> Optional[float]:
    if not hourly_temps:
        return None
    return min(hourly_temps)


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def _in_bucket(
    value: float,
    bucket_low: Optional[float],
    bucket_high: Optional[float],
    open_ended_low: bool,
    open_ended_high: bool,
) -> bool:
    """
    Return True if value falls in the bucket.
    Bucket boundaries treated as inclusive by default.
    """
    if open_ended_low and open_ended_high:
        # Degenerate — everything in bucket
        return True

    if open_ended_low:
        # "< bucket_high": no lower bound
        if bucket_high is not None:
            return value <= bucket_high
        return True

    if open_ended_high:
        # ">= bucket_low": no upper bound
        if bucket_low is not None:
            return value >= bucket_low
        return True

    # Range bucket
    in_low = (bucket_low is None) or (value >= bucket_low)
    in_high = (bucket_high is None) or (value <= bucket_high)
    return in_low and in_high


def _laplace_probability(
    n_in_bucket: int,
    n_total: int,
    alpha: float = DEFAULT_LAPLACE_ALPHA,
    pseudo_n: float = DEFAULT_LAPLACE_N,
) -> float:
    """Laplace-smoothed probability to avoid 0/1 extremes."""
    # P = (n_in + alpha) / (n_total + alpha * n_buckets)
    # We treat as binary (in/out), so n_buckets=2
    return (n_in_bucket + alpha) / (n_total + alpha * 2)


def _apply_bias(
    values: list[float],
    icao: Optional[str],
    market_type: str,
    unit: str,
    bias_config: Optional[dict],
) -> tuple[list[float], float]:
    """Apply per-station bias correction. Returns (corrected_values, bias_applied)."""
    if not bias_config or not icao:
        return values, 0.0

    station_bias = bias_config.get(icao.upper(), {})
    if not station_bias:
        return values, 0.0

    if market_type == "daily_high_temperature":
        bias_key = f"daily_high_bias_{unit}"
    elif market_type == "daily_low_temperature":
        bias_key = f"daily_low_bias_{unit}"
    else:
        return values, 0.0

    bias = station_bias.get(bias_key, 0.0)
    if bias == 0.0:
        return values, 0.0

    corrected = [v + bias for v in values]
    return corrected, bias


def compute_forecast(
    lat: float,
    lon: float,
    target_date: date,
    market_type: str,
    unit: str,
    bucket_low: Optional[float],
    bucket_high: Optional[float],
    open_ended_low: bool,
    open_ended_high: bool,
    icao: Optional[str] = None,
    bias_config: Optional[dict] = None,
    laplace_alpha: float = DEFAULT_LAPLACE_ALPHA,
    laplace_n: float = DEFAULT_LAPLACE_N,
) -> ForecastResult:
    """
    Main forecast function.

    1. Fetch ensemble data from Open-Meteo.
    2. Compute daily extreme (max or min) for each member.
    3. Convert units if needed (ensemble is always in Celsius).
    4. Apply bias correction.
    5. Count members in bucket.
    6. Return Laplace-smoothed probability.
    """
    if lat is None or lon is None:
        return ForecastResult(
            model_probability=0.5,
            ensemble_agreement=0.0,
            model_spread=0.0,
            n_members=0,
            n_members_in_bucket=0,
            daily_extreme_values=[],
            model_used=None,
            bias_applied=0.0,
            error="no_coordinates",
        )

    ensemble_data = fetch_open_meteo_ensemble(lat, lon, target_date)

    if ensemble_data is None:
        # Fallback to deterministic
        logger.warning("Ensemble failed, falling back to deterministic for lat=%.3f lon=%.3f", lat, lon)
        det_data = fetch_open_meteo_forecast(lat, lon, target_date)
        if det_data is None:
            return ForecastResult(
                model_probability=0.5,
                ensemble_agreement=0.0,
                model_spread=0.0,
                n_members=0,
                n_members_in_bucket=0,
                daily_extreme_values=[],
                model_used=None,
                bias_applied=0.0,
                error="all_fetches_failed",
            )
        return _from_deterministic(det_data, market_type, unit, bucket_low, bucket_high,
                                   open_ended_low, open_ended_high, icao, bias_config,
                                   laplace_alpha, laplace_n)

    model_used = ensemble_data.get("_model_used", "unknown")
    members = extract_ensemble_members(ensemble_data)

    if not members:
        # Single-array fallback (non-ensemble endpoint)
        return _from_deterministic(ensemble_data, market_type, unit, bucket_low, bucket_high,
                                   open_ended_low, open_ended_high, icao, bias_config,
                                   laplace_alpha, laplace_n)

    # Compute daily extreme per member (ensemble is in Celsius)
    extremes_c: list[float] = []
    for member_id, hourly_temps in members.items():
        if not hourly_temps:
            continue
        if market_type == "daily_high_temperature":
            val = _compute_daily_max(hourly_temps)
        else:
            val = _compute_daily_min(hourly_temps)
        if val is not None:
            extremes_c.append(val)

    if not extremes_c:
        return ForecastResult(
            model_probability=0.5,
            ensemble_agreement=0.0,
            model_spread=0.0,
            n_members=0,
            n_members_in_bucket=0,
            daily_extreme_values=[],
            model_used=model_used,
            bias_applied=0.0,
            error="no_valid_member_data",
        )

    # Convert to target unit
    if unit == "F":
        extremes_unit = [normalize_units(v, "C", "F") for v in extremes_c]
    else:
        extremes_unit = extremes_c

    # Apply bias correction
    extremes_corrected, bias_applied = _apply_bias(extremes_unit, icao, market_type, unit, bias_config)

    n_total = len(extremes_corrected)
    n_in_bucket = sum(
        1 for v in extremes_corrected
        if _in_bucket(v, bucket_low, bucket_high, open_ended_low, open_ended_high)
    )

    model_prob = _laplace_probability(n_in_bucket, n_total, laplace_alpha, laplace_n)
    agreement = n_in_bucket / n_total if n_total > 0 else 0.0
    spread = _stddev(extremes_corrected)

    logger.info(
        "Forecast: %s %s lat=%.2f lon=%.2f date=%s bucket=[%s,%s] n=%d in_bucket=%d prob=%.3f spread=%.2f",
        market_type, unit, lat, lon, target_date, bucket_low, bucket_high,
        n_total, n_in_bucket, model_prob, spread,
    )

    return ForecastResult(
        model_probability=model_prob,
        ensemble_agreement=agreement,
        model_spread=spread,
        n_members=n_total,
        n_members_in_bucket=n_in_bucket,
        daily_extreme_values=extremes_corrected,
        model_used=model_used,
        bias_applied=bias_applied,
    )


def _from_deterministic(
    data: dict,
    market_type: str,
    unit: str,
    bucket_low: Optional[float],
    bucket_high: Optional[float],
    open_ended_low: bool,
    open_ended_high: bool,
    icao: Optional[str],
    bias_config: Optional[dict],
    laplace_alpha: float,
    laplace_n: float,
) -> ForecastResult:
    """Fallback: use deterministic daily max/min from Open-Meteo."""
    daily = data.get("daily") or {}
    model_used = data.get("_model_used", "deterministic")

    if market_type == "daily_high_temperature":
        val_list = daily.get("temperature_2m_max") or []
    else:
        val_list = daily.get("temperature_2m_min") or []

    if not val_list:
        return ForecastResult(
            model_probability=0.5,
            ensemble_agreement=0.0,
            model_spread=0.0,
            n_members=0,
            n_members_in_bucket=0,
            daily_extreme_values=[],
            model_used=model_used,
            bias_applied=0.0,
            error="deterministic_no_daily_data",
        )

    val_c = float(val_list[0])
    val_unit = normalize_units(val_c, "C", unit)
    corrected, bias = _apply_bias([val_unit], icao, market_type, unit, bias_config)
    val_final = corrected[0]

    in_bkt = _in_bucket(val_final, bucket_low, bucket_high, open_ended_low, open_ended_high)
    # Single deterministic point: strong conviction, but limit to [0.1, 0.9]
    raw_prob = 0.85 if in_bkt else 0.15
    model_prob = _laplace_probability(1 if in_bkt else 0, 1, laplace_alpha, laplace_n)

    return ForecastResult(
        model_probability=model_prob,
        ensemble_agreement=1.0 if in_bkt else 0.0,
        model_spread=0.0,
        n_members=1,
        n_members_in_bucket=1 if in_bkt else 0,
        daily_extreme_values=[val_final],
        model_used=model_used,
        bias_applied=bias,
        error="deterministic_fallback",
    )
