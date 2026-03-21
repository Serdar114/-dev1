"""
feeds/intra_window_collector.py — Provisional intra-window YES price collector.

Collects YES probability snapshots during a 5-minute window by polling the
CLOB REST API at a fixed interval.  Used to populate intra_window_prices for
the maker lane fill simulation.

Fill realism grade when using this collector: PROVISIONAL_OBSERVED_PROXY
    - "OBSERVED" because we actually see prices during the window
    - "PROXY" because REST polling has latency and may miss intra-tick extremes
    - Not as reliable as a live WebSocket book feed, but better than PROVISIONAL_NO_PATH

TODO: Upgrade to WebSocket CLOB book stream for OBSERVED_PATH grade.

Usage (in bot.py alongside settlement sleep)
---------------------------------------------
    collector = IntraWindowYesPriceCollector(adapter, poll_interval_seconds=60.0)

    # Run concurrently with the settlement sleep:
    prices, _ = await asyncio.gather(
        collector.collect(token_id, duration_seconds=280.0),
        asyncio.sleep(280.0),
    )

    # prices is a list of float (YES probabilities) or empty list if all failed.
    maker_result = maker_lane.evaluate(..., intra_window_prices=prices or None,
                                       fill_realism_source="PROVISIONAL_OBSERVED_PROXY")
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from .yes_price_adapter import CLOBYesPriceAdapter

logger = logging.getLogger(__name__)

# Fill realism grade for REST-polled intra-window prices.
# Distinct from PROVISIONAL_NO_PATH (no prices) and OBSERVED_PATH (WebSocket).
FILL_GRADE_PROVISIONAL_PROXY = "PROVISIONAL_OBSERVED_PROXY"


class IntraWindowYesPriceCollector:
    """
    Collect YES probability prices during a window via periodic REST polling.

    Parameters
    ----------
    adapter          : CLOBYesPriceAdapter instance to poll.
    poll_interval_s  : Seconds between polls (default 60 = once per minute).
    """

    def __init__(
        self,
        adapter: CLOBYesPriceAdapter,
        poll_interval_s: float = 60.0,
    ) -> None:
        self._adapter = adapter
        self._interval = poll_interval_s

    async def collect(
        self,
        token_id: str,
        duration_seconds: float,
    ) -> List[float]:
        """
        Poll YES mid price every `poll_interval_s` seconds for `duration_seconds`.

        Returns
        -------
        List of float YES probability values observed during the window.
        Empty list if all polls failed or duration was too short for any poll.

        Each price in the list is in (0, 1) — validated by YesPriceSnapshot.
        """
        prices: List[float] = []
        start = time.monotonic()
        deadline = start + duration_seconds

        logger.debug(
            "[intra_collector] Starting collection: token_id=%s duration=%.1fs interval=%.1fs",
            token_id, duration_seconds, self._interval
        )

        while True:
            snap = await self._adapter.get_yes_mid(token_id=token_id)
            if snap is not None:
                prices.append(snap.probability)
                logger.debug(
                    "[intra_collector] Poll token_id=%s price=%.4f [PROVISIONAL_PROXY]",
                    token_id, snap.probability
                )
            else:
                logger.warning(
                    "[intra_collector] Poll failed for token_id=%s", token_id
                )

            # Sleep until next poll, or stop if deadline is past.
            elapsed = time.monotonic() - start
            remaining = deadline - (start + elapsed)
            if remaining <= self._interval:
                break
            await asyncio.sleep(self._interval)

        logger.info(
            "[intra_collector] Collected %d price(s) for token_id=%s over %.1fs",
            len(prices), token_id, time.monotonic() - start
        )
        return prices
