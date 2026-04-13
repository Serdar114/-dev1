"""
truth/resolution_truth.py — Buffer-based window resolution.

PATCH 2 — exact window-close capture.

Previous bug:
  resolve_window() used "current Chainlink snapshot at rollover time."
  Rollover happens 1-5s AFTER window_end. The "current" oracle reading may
  reflect an update that occurred AFTER the window closed, which means we
  were resolving with a price the market never saw during the window.

Fix:
  1. ChainlinkBuffer stores every oracle observation with its oracle_updated_at.
  2. resolve_window() queries buffer for best_close_capture(window_end).
  3. best_close_capture returns the last observation with oracle_updated_at <= window_end.
  4. Resolution logs: close_capture_timestamp, close_capture_source, seconds_before_window_end.
  5. If no buffer observation qualifies: UNRESOLVED — never falls through to live snapshot.

Fallback (buffer not provided or empty):
  Falls back to live snapshot with explicit source label "live_snapshot_fallback".
  This is NOT canonical close truth — labelled accordingly.

Resolution status values:
  "resolved_canonical"        — buffer capture with oracle_updated_at <= window_end
  "resolved_snapshot_fallback"— live snapshot used (buffer miss), less precise
  "stale_oracle"              — oracle age exceeded max_oracle_age
  "missing_oracle"            — no price available at all
  "missing_start"             — price_at_start was never recorded
  "equal_price"               — start == end price (undefined outcome)
  "buffer_miss"               — buffer had no observation within acceptable range
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from truth.freshness import is_fresh


@dataclass
class Resolution:
    window_start: int
    price_at_start: Optional[float]
    price_at_end: Optional[float]
    oracle_age_at_resolution: Optional[float]
    outcome: Optional[str]          # "Up" | "Down" | None
    status: str
    resolved_at: float
    # Timing metadata — explicit in every resolution record
    close_capture_timestamp: Optional[float] = None   # oracle_updated_at used
    close_capture_source: str = ""                    # "buffer_polygon_rpc" | "buffer_rtds" | "live_snapshot_fallback" | ""
    seconds_before_window_end: Optional[float] = None # window_end - close_capture_timestamp


def resolve_window(
    window_start: int,
    price_at_start: Optional[float],
    chainlink,                       # ChainlinkClient (for fallback snapshot)
    max_oracle_age: float,
    buffer=None,                     # Optional[ChainlinkBuffer]
) -> Resolution:
    """
    Resolve a completed window.

    Algorithm:
      1. If buffer provided: query best_close_capture(window_end, max_age=max_oracle_age)
         → if found: use that price as price_at_end (status: resolved_canonical)
         → if not found: status = buffer_miss → UNRESOLVED
      2. If no buffer: fall back to live chainlink snapshot
         → if fresh: use with status "resolved_snapshot_fallback" (less precise)
         → if stale/missing: UNRESOLVED

    Caller should treat "resolved_snapshot_fallback" as lower-quality truth.
    """
    now = time.time()
    window_end = window_start + 300

    if price_at_start is None:
        return Resolution(
            window_start=window_start,
            price_at_start=None,
            price_at_end=None,
            oracle_age_at_resolution=None,
            outcome=None,
            status="missing_start",
            resolved_at=now,
        )

    # -----------------------------------------------------------------------
    # Path A: buffer-based close capture (preferred)
    # -----------------------------------------------------------------------
    if buffer is not None:
        capture = buffer.best_close_capture(window_end, max_age_before_end=max_oracle_age)
        if capture is None:
            return Resolution(
                window_start=window_start,
                price_at_start=price_at_start,
                price_at_end=None,
                oracle_age_at_resolution=None,
                outcome=None,
                status="buffer_miss",
                resolved_at=now,
                close_capture_source="buffer",
                seconds_before_window_end=None,
            )

        price_at_end = capture.price
        seconds_before = capture.seconds_before_window_end
        src_label = f"buffer_{capture.source}"

        if price_at_end > price_at_start:
            outcome, status = "Up", "resolved_canonical"
        elif price_at_end < price_at_start:
            outcome, status = "Down", "resolved_canonical"
        else:
            outcome, status = None, "equal_price"

        return Resolution(
            window_start=window_start,
            price_at_start=price_at_start,
            price_at_end=price_at_end,
            oracle_age_at_resolution=seconds_before,
            outcome=outcome,
            status=status,
            resolved_at=now,
            close_capture_timestamp=capture.oracle_updated_at,
            close_capture_source=src_label,
            seconds_before_window_end=seconds_before,
        )

    # -----------------------------------------------------------------------
    # Path B: live snapshot fallback (no buffer)
    # -----------------------------------------------------------------------
    snap = chainlink.snapshot()
    if snap.price is None:
        return Resolution(
            window_start=window_start,
            price_at_start=price_at_start,
            price_at_end=None,
            oracle_age_at_resolution=None,
            outcome=None,
            status="missing_oracle",
            resolved_at=now,
        )

    age = snap.age_seconds()
    if not is_fresh(age, max_oracle_age):
        return Resolution(
            window_start=window_start,
            price_at_start=price_at_start,
            price_at_end=snap.price,
            oracle_age_at_resolution=age,
            outcome=None,
            status="stale_oracle",
            resolved_at=now,
            close_capture_source="live_snapshot_fallback",
        )

    if snap.price > price_at_start:
        outcome, status = "Up", "resolved_snapshot_fallback"
    elif snap.price < price_at_start:
        outcome, status = "Down", "resolved_snapshot_fallback"
    else:
        outcome, status = None, "equal_price"

    return Resolution(
        window_start=window_start,
        price_at_start=price_at_start,
        price_at_end=snap.price,
        oracle_age_at_resolution=age,
        outcome=outcome,
        status=status,
        resolved_at=now,
        close_capture_timestamp=snap.oracle_updated_at,
        close_capture_source="live_snapshot_fallback",
        seconds_before_window_end=(
            window_end - snap.oracle_updated_at
            if snap.oracle_updated_at else None
        ),
    )
