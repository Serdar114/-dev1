"""
Nowcast engine: use latest station observation to estimate current trajectory.

Conservative: if uncertain, return nowcast_confidence=low.

For daily high: if it's morning and current temp is already near the threshold,
the model gets a nudge. If it's afternoon and temp hasn't crossed, probability drops.

This module does NOT replace the forecast engine — it provides a signal modifier.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pytz

from .weather_fetch import fetch_station_observation, normalize_units

logger = logging.getLogger(__name__)

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_NONE = "none"


@dataclass
class NowcastResult:
    current_temp: Optional[float]     # in market unit (F or C)
    current_temp_c: Optional[float]   # always Celsius
    obs_time_utc: Optional[str]
    time_of_day_local: Optional[str]  # "morning" / "afternoon" / "evening" / "night" / None
    local_hour: Optional[int]
    threshold_crossed: bool           # has current temp crossed the bucket boundary?
    trajectory_supports: Optional[bool]  # True = trending toward bucket
    nowcast_probability: Optional[float]  # rough 0-1 adjustment
    nowcast_confidence: str
    reason: Optional[str] = None


def _get_local_hour(tz_name: Optional[str]) -> Optional[int]:
    if not tz_name:
        return None
    try:
        tz = pytz.timezone(tz_name)
        now_local = datetime.now(tz)
        return now_local.hour
    except Exception:
        return None


def _time_of_day_label(hour: Optional[int]) -> Optional[str]:
    if hour is None:
        return None
    if 5 <= hour < 10:
        return "morning"
    if 10 <= hour < 15:
        return "afternoon_early"
    if 15 <= hour < 19:
        return "afternoon_late"
    if 19 <= hour < 22:
        return "evening"
    return "night"


def _estimate_nowcast_probability(
    market_type: str,
    current_temp: float,
    bucket_low: Optional[float],
    bucket_high: Optional[float],
    open_ended_low: bool,
    open_ended_high: bool,
    local_hour: Optional[int],
) -> tuple[Optional[float], bool, Optional[bool], str]:
    """
    Returns (nowcast_probability, threshold_crossed, trajectory_supports, reason).

    Rough heuristic scoring:
    - For daily HIGH:
      - If current temp >= bucket_low (and morning/early afternoon): high probability still has time to peak
      - If current temp >= bucket_low (afternoon_late): already in or past threshold, probability high
      - If current temp is clearly below bucket_low with little time: probability low
    - For daily LOW:
      - If current temp <= bucket_high (morning or night): threshold already approached
    """
    threshold_crossed = False
    trajectory_supports = None
    reason = "no_assessment"
    prob = None

    if local_hour is None:
        return None, threshold_crossed, None, "unknown_local_time"

    tod = _time_of_day_label(local_hour)

    if market_type == "daily_high_temperature":
        if open_ended_high and bucket_low is not None:
            # ">= X" bucket
            if current_temp >= bucket_low:
                threshold_crossed = True
                trajectory_supports = True
                # If already crossed, probability depends on time of day
                if tod in ("morning", "afternoon_early"):
                    prob = 0.82
                    reason = "temp_above_threshold_morning"
                else:
                    prob = 0.90
                    reason = "temp_above_threshold_afternoon"
            else:
                diff = bucket_low - current_temp
                if tod == "morning":
                    # Still plenty of time to warm
                    if diff <= 3:
                        prob = 0.65
                        trajectory_supports = True
                        reason = "close_to_threshold_morning"
                    elif diff <= 8:
                        prob = 0.45
                        trajectory_supports = None
                        reason = "moderate_gap_morning"
                    else:
                        prob = 0.25
                        trajectory_supports = False
                        reason = "large_gap_morning"
                elif tod == "afternoon_early":
                    if diff <= 2:
                        prob = 0.55
                        trajectory_supports = True
                        reason = "close_to_threshold_early_afternoon"
                    elif diff <= 5:
                        prob = 0.35
                        trajectory_supports = None
                        reason = "moderate_gap_early_afternoon"
                    else:
                        prob = 0.15
                        trajectory_supports = False
                        reason = "large_gap_early_afternoon"
                else:
                    # Late/evening — hard to reach now
                    if diff <= 1:
                        prob = 0.40
                        reason = "marginal_gap_late"
                    else:
                        prob = 0.10
                        trajectory_supports = False
                        reason = "below_threshold_late_day"

        elif bucket_low is not None and bucket_high is not None:
            # Range bucket for daily high
            if current_temp >= bucket_low and current_temp <= bucket_high:
                threshold_crossed = True
                trajectory_supports = True
                prob = 0.70
                reason = "current_temp_in_bucket"
            elif current_temp < bucket_low:
                diff = bucket_low - current_temp
                if tod == "morning" and diff <= 5:
                    prob = 0.55
                    reason = "trending_toward_bucket_morning"
                else:
                    prob = 0.30
                    reason = "below_bucket"
            else:
                # Above bucket — less likely to come back down for daily HIGH
                prob = 0.20
                reason = "above_bucket_already"

        elif open_ended_low and bucket_high is not None:
            # "< X" bucket for daily high — unusual but handle
            if current_temp < bucket_high:
                threshold_crossed = True
                trajectory_supports = True
                prob = 0.70
                reason = "below_upper_bound"
            else:
                prob = 0.30
                reason = "above_upper_bound"

    elif market_type == "daily_low_temperature":
        if open_ended_low and bucket_high is not None:
            # "< X" — looking for low to be below threshold
            if current_temp <= bucket_high:
                threshold_crossed = True
                trajectory_supports = True
                if tod in ("morning", "night"):
                    prob = 0.80
                    reason = "below_threshold_early"
                else:
                    prob = 0.60
                    reason = "below_threshold_daytime"
            else:
                diff = current_temp - bucket_high
                if tod in ("evening", "night") and diff <= 3:
                    prob = 0.50
                    reason = "approaching_threshold_night"
                else:
                    prob = 0.25
                    reason = "above_threshold"

        elif bucket_low is not None and bucket_high is not None:
            # Range bucket for daily low
            if bucket_low <= current_temp <= bucket_high:
                threshold_crossed = True
                trajectory_supports = True
                prob = 0.65
                reason = "current_in_low_bucket"
            else:
                prob = 0.30
                reason = "outside_low_bucket"

    if prob is None:
        return None, False, None, "assessment_not_applicable"

    return prob, threshold_crossed, trajectory_supports, reason


def compute_nowcast(
    icao: Optional[str],
    market_type: str,
    unit: str,
    bucket_low: Optional[float],
    bucket_high: Optional[float],
    open_ended_low: bool,
    open_ended_high: bool,
    timezone_name: Optional[str] = None,
) -> NowcastResult:
    """
    Fetch latest station observation and compute nowcast signal.
    """
    if not icao:
        return NowcastResult(
            current_temp=None,
            current_temp_c=None,
            obs_time_utc=None,
            time_of_day_local=None,
            local_hour=None,
            threshold_crossed=False,
            trajectory_supports=None,
            nowcast_probability=None,
            nowcast_confidence=CONFIDENCE_NONE,
            reason="no_icao",
        )

    obs = fetch_station_observation(icao)
    if obs is None:
        return NowcastResult(
            current_temp=None,
            current_temp_c=None,
            obs_time_utc=None,
            time_of_day_local=None,
            local_hour=None,
            threshold_crossed=False,
            trajectory_supports=None,
            nowcast_probability=None,
            nowcast_confidence=CONFIDENCE_NONE,
            reason="obs_fetch_failed",
        )

    temp_c = obs.get("temp_c")
    if temp_c is None:
        return NowcastResult(
            current_temp=None,
            current_temp_c=None,
            obs_time_utc=obs.get("obs_time_utc"),
            time_of_day_local=None,
            local_hour=None,
            threshold_crossed=False,
            trajectory_supports=None,
            nowcast_probability=None,
            nowcast_confidence=CONFIDENCE_NONE,
            reason="no_temp_in_obs",
        )

    current_temp_unit = normalize_units(temp_c, "C", unit)
    local_hour = _get_local_hour(timezone_name)
    tod = _time_of_day_label(local_hour)

    prob, threshold_crossed, trajectory_supports, reason = _estimate_nowcast_probability(
        market_type=market_type,
        current_temp=current_temp_unit,
        bucket_low=bucket_low,
        bucket_high=bucket_high,
        open_ended_low=open_ended_low,
        open_ended_high=open_ended_high,
        local_hour=local_hour,
    )

    # Determine confidence
    if prob is None:
        confidence = CONFIDENCE_NONE
    elif local_hour is None:
        confidence = CONFIDENCE_LOW
    elif tod in ("morning", "afternoon_early") and reason != "assessment_not_applicable":
        confidence = CONFIDENCE_MEDIUM
    elif tod in ("afternoon_late", "evening") and threshold_crossed:
        confidence = CONFIDENCE_HIGH
    else:
        confidence = CONFIDENCE_LOW

    return NowcastResult(
        current_temp=current_temp_unit,
        current_temp_c=temp_c,
        obs_time_utc=obs.get("obs_time_utc"),
        time_of_day_local=tod,
        local_hour=local_hour,
        threshold_crossed=threshold_crossed,
        trajectory_supports=trajectory_supports,
        nowcast_probability=prob,
        nowcast_confidence=confidence,
        reason=reason,
    )
