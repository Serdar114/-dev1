"""
freshness.py — Explicit staleness/freshness evaluation.

Design:
  Three states only: FRESH, STALE, MISSING.
  MISSING = we have never received data.
  STALE   = we have data but it is too old.
  FRESH   = data is recent enough to be used canonically.
  No ambiguity. No silent defaults.
  Callers must handle all three states.
"""

from __future__ import annotations
import time
from typing import Optional

from loggingx.schemas import FreshnessState


def check_freshness(
    last_updated_ts: Optional[float],
    max_age_secs: float,
) -> str:
    """
    Evaluate freshness of a data point.

    Args:
        last_updated_ts: UTC unix timestamp of last update, or None if never received.
        max_age_secs:    Maximum acceptable age in seconds.

    Returns:
        FreshnessState.FRESH | STALE | MISSING
    """
    if last_updated_ts is None:
        return FreshnessState.MISSING

    age = time.time() - last_updated_ts
    if age > max_age_secs:
        return FreshnessState.STALE

    return FreshnessState.FRESH


def age_secs(last_updated_ts: Optional[float]) -> Optional[float]:
    """Return age in seconds, or None if never updated."""
    if last_updated_ts is None:
        return None
    return time.time() - last_updated_ts


def is_fresh(last_updated_ts: Optional[float], max_age_secs: float) -> bool:
    """Convenience: True only if FRESH. STALE and MISSING both return False."""
    return check_freshness(last_updated_ts, max_age_secs) == FreshnessState.FRESH
