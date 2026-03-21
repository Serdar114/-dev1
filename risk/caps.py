"""
risk/caps.py — Daily caps and position-at-a-time enforcement.

All caps are enforced in code, not just reported in summaries.

Caps (read from config → daily_caps):
    max_candidates_per_day : 50  — stop processing new candidates after this count
    max_entries_per_day    : 5   — hard cap on filled paper entries per calendar day
    one_position_at_a_time : True — no overlapping open positions allowed

Calendar day is determined by UTC date.  Caps reset at UTC midnight.

Usage in bot loop:
    caps = DailyCaps(config["daily_caps"])
    ...
    caps.reset_if_new_day()                    # call at start of each window
    if not caps.can_observe_candidate():       # check before evaluating signal
        skip_window()
    if not caps.can_enter():                   # check before placing paper entry
        skip_entry()
    caps.record_candidate()
    caps.record_entry()
    caps.record_exit()                         # when position settles
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DayCapsState:
    """Snapshot of caps state for logging / summary."""
    date: datetime.date
    candidates_today: int
    entries_today: int
    position_open: bool
    max_candidates: int
    max_entries: int
    one_at_a_time: bool


class DailyCaps:
    """
    Enforces daily candidate / entry limits and position overlap rules.

    All methods are synchronous and side-effect-free except the mutating
    record_* methods.
    """

    def __init__(self, config: dict) -> None:
        self._max_candidates = int(config.get("max_candidates_per_day", 50))
        self._max_entries = int(config.get("max_entries_per_day", 5))
        self._one_at_a_time = bool(config.get("one_position_at_a_time", True))

        self._today = datetime.datetime.utcnow().date()
        self._candidates_today = 0
        self._entries_today = 0
        self._position_open = False

    # ------------------------------------------------------------------
    # State reset
    # ------------------------------------------------------------------

    def reset_if_new_day(self) -> bool:
        """
        Check if UTC date has rolled over; reset daily counters if so.
        Returns True if a reset occurred.
        """
        today = datetime.datetime.utcnow().date()
        if today != self._today:
            logger.info(
                "[caps] UTC day rollover %s → %s: resetting daily caps "
                "(candidates=%d entries=%d)",
                self._today, today, self._candidates_today, self._entries_today
            )
            self._today = today
            self._candidates_today = 0
            self._entries_today = 0
            # NOTE: do NOT reset _position_open across day boundary — a position
            # open at midnight carries over until it settles.
            return True
        return False

    # ------------------------------------------------------------------
    # Gate checks (non-mutating)
    # ------------------------------------------------------------------

    def can_observe_candidate(self) -> tuple:
        """
        Returns (allowed: bool, reason: str).
        Checks daily candidate cap.
        """
        if self._candidates_today >= self._max_candidates:
            return (
                False,
                f"daily_candidate_cap_reached:{self._candidates_today}/{self._max_candidates}",
            )
        return (True, "ok")

    def can_enter(self) -> tuple:
        """
        Returns (allowed: bool, reason: str).
        Checks daily entry cap AND one-position-at-a-time rule.
        """
        if self._entries_today >= self._max_entries:
            return (
                False,
                f"daily_entry_cap_reached:{self._entries_today}/{self._max_entries}",
            )
        if self._one_at_a_time and self._position_open:
            return (False, "one_position_at_a_time:position_already_open")
        return (True, "ok")

    # ------------------------------------------------------------------
    # State mutations
    # ------------------------------------------------------------------

    def record_candidate(self) -> None:
        """Call when a window is evaluated as a candidate (signal emitted)."""
        self._candidates_today += 1
        logger.debug(
            "[caps] candidate recorded: %d/%d today",
            self._candidates_today, self._max_candidates
        )

    def record_entry(self) -> None:
        """Call when a paper fill is simulated (entry taken)."""
        self._entries_today += 1
        self._position_open = True
        logger.info(
            "[caps] entry recorded: %d/%d today | position_open=True",
            self._entries_today, self._max_entries
        )

    def record_exit(self) -> None:
        """Call when a paper position settles (trade closed)."""
        self._position_open = False
        logger.info("[caps] position settled: position_open=False")

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def snapshot(self) -> DayCapsState:
        return DayCapsState(
            date=self._today,
            candidates_today=self._candidates_today,
            entries_today=self._entries_today,
            position_open=self._position_open,
            max_candidates=self._max_candidates,
            max_entries=self._max_entries,
            one_at_a_time=self._one_at_a_time,
        )

    def log_status(self) -> None:
        s = self.snapshot()
        logger.info(
            "[caps] date=%s candidates=%d/%d entries=%d/%d position_open=%s",
            s.date, s.candidates_today, s.max_candidates,
            s.entries_today, s.max_entries, s.position_open,
        )
