"""
truth/freshness.py — Feed freshness assessment helpers.

Returns structured labels used in no-trade rules and terminal UI.
Does not mutate state.
"""
from __future__ import annotations

import time
from typing import Optional


def freshness_label(age_seconds: Optional[float], max_age: float) -> str:
    """
    Return one of: "FRESH" | "STALE:{age}s" | "MISSING"
    """
    if age_seconds is None:
        return "MISSING"
    if age_seconds <= max_age:
        return f"FRESH({age_seconds:.0f}s)"
    return f"STALE({age_seconds:.0f}s)"


def is_fresh(age_seconds: Optional[float], max_age: float) -> bool:
    if age_seconds is None:
        return False
    return age_seconds <= max_age


def is_missing(age_seconds: Optional[float]) -> bool:
    return age_seconds is None
