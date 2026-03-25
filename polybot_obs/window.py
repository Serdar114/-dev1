# window.py — window boundary tracking and snapshot scheduling

import asyncio
import logging
import time
from typing import Callable

from config import (
    WINDOW_SECONDS,
    SNAPSHOT_OFFSETS,
    SNAPSHOT_POLL_INTERVAL,
    SLUG_PREFIX,
)

logger = logging.getLogger(__name__)

# Map offset → snapshot key used in JSONL
OFFSET_KEY = {
    60: "T60",
    30: "T30",
    15: "T15",
    10: "T10",
    5:  "T5",
    1:  "T1",
}


def current_window_ts() -> int:
    """Return the Unix timestamp of the start of the current 5-min window."""
    return (int(time.time()) // WINDOW_SECONDS) * WINDOW_SECONDS


def window_close_ts(open_ts: int) -> int:
    return open_ts + WINDOW_SECONDS


def slug_for(ts: int) -> str:
    return f"{SLUG_PREFIX}{ts}"


def time_remaining(open_ts: int) -> float:
    """Seconds remaining until this window closes."""
    close = window_close_ts(open_ts)
    return max(0.0, close - time.time())


class WindowState:
    """All mutable state for a single 5-minute window observation."""

    def __init__(self, open_ts: int):
        self.open_ts:       int          = open_ts
        self.slug:          str          = slug_for(open_ts)
        self.open_btc:      float | None = None
        self.yes_token_id:  str | None   = None
        self.min_order_size: float | None = None
        self.snapshots:     dict         = {}          # key → {btc, yes_ask, ts}
        self.paper_snipe:   dict         = {"triggered": False}
        self.snipe_done:    bool         = False       # only first trigger per window


async def snapshot_loop(
    state: WindowState,
    btc_price_fn: Callable[[], float | None],
    ask_fn: Callable[[], float | None],   # async coroutine factory
):
    """
    Runs during a window. Every SNAPSHOT_POLL_INTERVAL seconds it:
      1. Saves a rolling {btc, yes_ask, ts} snapshot.
      2. At each SNAPSHOT_OFFSET before close: stores the keyed snapshot.
    Returns when the window closes.
    """
    open_ts  = state.open_ts
    close_ts = window_close_ts(open_ts)

    # Track which offset snapshots have been captured
    captured = set()

    while True:
        now = time.time()
        if now >= close_ts:
            break

        remaining = close_ts - now
        btc  = btc_price_fn()
        ask  = await ask_fn()

        snap = {
            "btc":     btc,
            "yes_ask": ask,
            "ts":      int(now),
        }

        # Check all SNAPSHOT_OFFSETS
        for offset in SNAPSHOT_OFFSETS:
            key = OFFSET_KEY[offset]
            if key not in captured and remaining <= offset + SNAPSHOT_POLL_INTERVAL / 2:
                state.snapshots[key] = snap
                captured.add(key)
                logger.debug("Snapshot %s: btc=%.2f ask=%s tr=%.1fs",
                             key, btc or 0, ask, remaining)

        await asyncio.sleep(SNAPSHOT_POLL_INTERVAL)

    # Fill any missed snapshot keys with null
    for key in OFFSET_KEY.values():
        if key not in state.snapshots:
            state.snapshots[key] = None


def btc_delta_pct_at_T10(state: WindowState) -> float | None:
    """Return BTC delta% at T10 snapshot vs open price."""
    snap = state.snapshots.get("T10")
    if snap and state.open_btc and snap.get("btc"):
        return round((snap["btc"] - state.open_btc) / state.open_btc * 100, 4)
    return None
