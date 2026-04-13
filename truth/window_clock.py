"""
truth/window_clock.py — 5-minute window time utilities.

All window timestamps are UTC Unix seconds.
Window boundaries: floor(t / 300) * 300.
No dependencies on external feeds.
"""
from __future__ import annotations

import time
from typing import Tuple


WINDOW_SECONDS = 300  # overrideable via config if needed


def current_window_start(now: float | None = None) -> int:
    """
    UTC Unix timestamp of the current 5-minute window start.
    E.g. at 16:37:23 UTC => 16:35:00 UTC.
    """
    t = now if now is not None else time.time()
    return int(t // WINDOW_SECONDS) * WINDOW_SECONDS


def current_window_end(now: float | None = None) -> int:
    return current_window_start(now) + WINDOW_SECONDS


def window_bounds(now: float | None = None) -> Tuple[int, int]:
    """Returns (window_start, window_end) for current window."""
    start = current_window_start(now)
    return start, start + WINDOW_SECONDS


def secs_to_expiry(now: float | None = None) -> int:
    end = current_window_end(now)
    t = now if now is not None else time.time()
    return max(0, int(end - t))


def prev_window_start(now: float | None = None) -> int:
    return current_window_start(now) - WINDOW_SECONDS


def is_same_window(ts_a: int, ts_b: int) -> bool:
    return current_window_start(ts_a) == current_window_start(ts_b)


def window_label(window_start: int) -> str:
    """Human-readable UTC label for a window."""
    import datetime
    s = datetime.datetime.utcfromtimestamp(window_start).strftime("%H:%M:%S")
    e = datetime.datetime.utcfromtimestamp(window_start + WINDOW_SECONDS).strftime("%H:%M:%S")
    return f"{s}→{e} UTC"
