"""
window_clock.py — Tracks window timing and fires open/close events.

Design:
  A market window has a start_ts and end_ts.
  We track where we are in that window.
  We emit events at open and close boundaries.
  We do NOT guess what time is "close enough" — that is config-driven tolerance.
  secs_to_expiry is computed on demand, not cached.
"""

from __future__ import annotations
import time
import logging
from typing import Optional

logger = logging.getLogger("polybot.window_clock")


class WindowClock:
    """
    Tracks timing state for a single market window.

    Job: tell callers where we are in the window lifecycle.
    Input: window_start_ts, window_end_ts (from market metadata)
    Output: phase, secs_to_expiry, is_open_boundary, is_close_boundary
    Failure: if times are inconsistent, report explicitly.
    """

    def __init__(
        self,
        condition_id: str,
        window_start_ts: float,
        window_end_ts: float,
        open_capture_tolerance_secs: float = 8.0,
        close_capture_tolerance_secs: float = 8.0,
    ):
        if window_end_ts <= window_start_ts:
            raise ValueError(
                f"window_end_ts ({window_end_ts}) must be > window_start_ts ({window_start_ts})"
            )
        self.condition_id = condition_id
        self.window_start_ts = window_start_ts
        self.window_end_ts = window_end_ts
        self.open_tol = open_capture_tolerance_secs
        self.close_tol = close_capture_tolerance_secs

        self._open_fired = False
        self._close_fired = False

    def now(self) -> float:
        return time.time()

    def secs_to_expiry(self) -> float:
        """Seconds remaining until window close. Negative means closed."""
        return self.window_end_ts - self.now()

    def secs_since_open(self) -> float:
        """Seconds since window opened. Negative means not yet open."""
        return self.now() - self.window_start_ts

    def is_before_open(self) -> bool:
        return self.now() < self.window_start_ts

    def is_after_close(self) -> bool:
        return self.now() > self.window_end_ts

    def is_live(self) -> bool:
        now = self.now()
        return self.window_start_ts <= now <= self.window_end_ts

    def phase(self) -> str:
        """
        PENDING  = before window_start
        LIVE     = inside window
        CLOSED   = after window_end
        """
        if self.is_before_open():
            return "PENDING"
        if self.is_after_close():
            return "CLOSED"
        return "LIVE"

    def should_fire_open(self) -> bool:
        """
        True once, when now is within [start - tol, start + tol] and not yet fired.
        Fires within the open tolerance window around window_start.
        """
        if self._open_fired:
            return False
        now = self.now()
        in_window = (self.window_start_ts - self.open_tol) <= now <= (self.window_start_ts + self.open_tol)
        # Also fire if we are already past start (late start scenario)
        already_past = now > self.window_start_ts
        if in_window or already_past:
            self._open_fired = True
            return True
        return False

    def should_fire_close(self) -> bool:
        """
        True once, when now >= window_end_ts (or within close tolerance before).
        We capture close at the boundary, not after.
        """
        if self._close_fired:
            return False
        now = self.now()
        approaching_close = now >= (self.window_end_ts - self.close_tol)
        if approaching_close:
            self._close_fired = True
            return True
        return False

    def open_was_missed(self) -> bool:
        """True if we are past open but open was never fired (e.g. market appeared late)."""
        return not self._open_fired and self.is_live()

    def window_duration_secs(self) -> float:
        return self.window_end_ts - self.window_start_ts

    def is_five_minute_window(self, tolerance_secs: float = 60.0) -> bool:
        """Sanity check: confirm this is approximately a 5-minute window."""
        return abs(self.window_duration_secs() - 300.0) <= tolerance_secs
