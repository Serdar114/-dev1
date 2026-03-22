"""
feeds/intra_window_collector.py — Provisional intra-window YES price collector.

Collects YES probability snapshots during the post-decision portion of a window
by polling the CLOB REST API at a configurable interval.  Used to populate
intra_window_prices for the maker lane fill simulation.

Fill realism grades (from maker_lane.py):
    PROVISIONAL_NO_PATH      — no prices at all (collection failed or duration too short)
    PROVISIONAL_SINGLE_POINT — exactly 1 price collected; fill NOT evaluable
    PROVISIONAL_MULTI_POINT  — 2+ prices collected; fill IS evaluable
    OBSERVED_PATH            — real WebSocket book feed (not yet implemented)

Polling interval
----------------
The default interval (15 s) is intentionally short relative to the ~40 s
collection window (T-45 → T-5 before close).  At 15 s we expect ~2–3 polls,
giving a multi-point path and enabling conservative fill evaluation.

A 60 s interval would yield 0–1 polls in a 40 s window — making the path
single-point (non-evaluable) in every live window.  That is why the default
was reduced and the value is config-driven.

Usage (in bot.py alongside settlement sleep)
---------------------------------------------
    collector = IntraWindowYesPriceCollector(adapter, poll_interval_s=15.0)

    collect_result, _ = await asyncio.gather(
        collector.collect(token_id, duration_seconds=35.0),
        asyncio.sleep(40.0),
    )

    # collect_result is a CollectionResult
    # pass collect_result.prices to MakerLane.evaluate()
    # pass collect_result.point_count for grade determination
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .yes_price_adapter import CLOBYesPriceAdapter

logger = logging.getLogger(__name__)


@dataclass
class CollectionResult:
    """
    Result of one intra-window price collection run.

    prices        : List of YES probability values observed (each in (0, 1)).
    timestamps    : Unix epoch of each successful poll (parallel to prices).
    poll_interval_s : The configured poll interval for this collection run.
    """
    prices: List[float] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)
    poll_interval_s: float = 15.0

    @property
    def point_count(self) -> int:
        return len(self.prices)

    @property
    def first_ts(self) -> Optional[float]:
        return self.timestamps[0] if self.timestamps else None

    @property
    def last_ts(self) -> Optional[float]:
        return self.timestamps[-1] if self.timestamps else None


class IntraWindowYesPriceCollector:
    """
    Collect YES probability prices during a window via periodic REST polling.

    Parameters
    ----------
    adapter          : CLOBYesPriceAdapter instance to poll.
    poll_interval_s  : Seconds between polls.

                       IMPORTANT: this must be materially less than the
                       collection duration to yield multi-point paths.

                       At the default dw_start=45 s, collection runs for
                       ~40 s (T-45 to T-5 before close).  A 15 s interval
                       yields ~2–3 points; a 60 s interval yields 0–1.

                       Only paths with 2+ points are treated as evaluable
                       for maker fill purposes.
    """

    def __init__(
        self,
        adapter: CLOBYesPriceAdapter,
        poll_interval_s: float = 15.0,
    ) -> None:
        self._adapter = adapter
        self._interval = poll_interval_s

    async def collect(
        self,
        token_id: str,
        duration_seconds: float,
    ) -> CollectionResult:
        """
        Poll YES mid price every `poll_interval_s` seconds for `duration_seconds`.

        Returns
        -------
        CollectionResult with prices, timestamps, and poll_interval_s.

        CollectionResult.prices is empty if all polls failed or duration was
        too short for any poll.  Each price is in (0, 1) — validated by
        YesPriceSnapshot.

        Fill evaluability (enforced in MakerLane.evaluate):
            point_count == 0 → PROVISIONAL_NO_PATH  → fill blocked
            point_count == 1 → PROVISIONAL_SINGLE_POINT → fill blocked
            point_count >= 2 → PROVISIONAL_MULTI_POINT  → fill evaluable
        """
        result = CollectionResult(poll_interval_s=self._interval)
        start = time.monotonic()
        deadline = start + duration_seconds

        logger.debug(
            "[intra_collector] Starting: token_id=%s duration=%.1fs interval=%.1fs",
            token_id, duration_seconds, self._interval
        )

        while True:
            poll_unix_ts = time.time()
            snap = await self._adapter.get_yes_mid(token_id=token_id)
            if snap is not None:
                result.prices.append(snap.probability)
                result.timestamps.append(poll_unix_ts)
                logger.debug(
                    "[intra_collector] Poll #%d token=%s price=%.4f ts=%.3f",
                    result.point_count, token_id, snap.probability, poll_unix_ts
                )
            else:
                logger.warning(
                    "[intra_collector] Poll failed for token_id=%s", token_id
                )

            # Sleep until next poll or stop if deadline is past.
            elapsed = time.monotonic() - start
            remaining = deadline - (start + elapsed)
            if remaining <= self._interval:
                break
            await asyncio.sleep(self._interval)

        logger.info(
            "[intra_collector] Done: token_id=%s points=%d over %.1fs "
            "(interval=%.1fs evaluable=%s)",
            token_id, result.point_count,
            time.monotonic() - start,
            self._interval,
            result.point_count >= 2,
        )
        return result
