"""
feeds/yes_price_adapter.py — Provisional YES probability source via CLOB REST API.

This adapter polls the Polymarket CLOB midpoint endpoint to obtain the current
YES-side probability for a given market token.

Provisional vs. confirmed
--------------------------
REST polling is PROVISIONAL.  The midpoint endpoint returns a point-in-time
snapshot, not a live stream.  Prices may be slightly stale (seconds old) and
do NOT have the same latency characteristics as a WebSocket book subscription.

All prices returned are tagged YesPriceSnapshot(is_provisional=True) to ensure
they are correctly flagged in fill realism grading and signal logging.

TODO: Upgrade to CLOB WebSocket book subscription once the channel is confirmed.
      When live, set is_provisional=False on YesPriceSnapshot.

Usage
-----
    adapter = CLOBYesPriceAdapter(clob_base_url="https://clob.polymarket.com")
    snap = await adapter.get_yes_mid(token_id="0xabc...", timestamp=time.time())
    if snap is not None:
        # snap.probability is a YES probability in (0, 1)
        ...
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .price_types import YesPriceSnapshot

logger = logging.getLogger(__name__)

# CLOB midpoint endpoint: GET /midpoint?token_id={id}
# Returns JSON: {"mid": "0.87"}
_MIDPOINT_PATH = "/midpoint"


class CLOBYesPriceAdapter:
    """
    Provisional YES probability adapter using CLOB REST API polling.

    This is the interim data source until a WebSocket CLOB book feed is live.
    All returned YesPriceSnapshot objects are marked is_provisional=True.

    Parameters
    ----------
    clob_base_url : Base URL for the CLOB REST API.
                    e.g. "https://clob.polymarket.com"
    timeout_seconds : HTTP request timeout.
    """

    def __init__(
        self,
        clob_base_url: str = "https://clob.polymarket.com",
        timeout_seconds: float = 3.0,
    ) -> None:
        self._base_url = clob_base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def get_yes_mid(
        self,
        token_id: str,
        timestamp: Optional[float] = None,
    ) -> Optional[YesPriceSnapshot]:
        """
        Fetch the current YES mid probability from the CLOB REST API.

        Returns
        -------
        YesPriceSnapshot with is_provisional=True, or None on any error.

        Note: Returns None on network failure, bad JSON, or price outside (0,1).
        Callers must handle None and treat it as YES probability unavailable.
        """
        import aiohttp  # lazy import — optional dependency

        ts = timestamp if timestamp is not None else time.time()
        url = f"{self._base_url}{_MIDPOINT_PATH}?token_id={token_id}"

        try:
            connector = aiohttp.TCPConnector(
                resolver=aiohttp.resolver.ThreadedResolver()
            )
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=self._timeout)) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "[clob_yes] HTTP %d for token_id=%s url=%s",
                            resp.status, token_id, url
                        )
                        return None
                    data = await resp.json()
        except asyncio.TimeoutError:
            logger.warning(
                "[clob_yes] Timeout (%.1fs) for token_id=%s url=%s",
                self._timeout, token_id, url
            )
            return None
        except aiohttp.ClientConnectorError as exc:
            cause = str(exc)
            is_dns = "dns" in cause.lower() or "name or service not known" in cause.lower() or "could not contact" in cause.lower()
            logger.warning(
                "[clob_yes] %s for token_id=%s url=%s — %s: %s",
                "DNS resolution failed" if is_dns else "Connection error",
                token_id, url, type(exc).__name__, exc
            )
            return None
        except Exception as exc:
            logger.warning(
                "[clob_yes] Unexpected error for token_id=%s url=%s — %s: %s",
                token_id, url, type(exc).__name__, exc
            )
            return None

        raw_mid = data.get("mid")
        if raw_mid is None:
            logger.warning("[clob_yes] No 'mid' key in response for token_id=%s", token_id)
            return None

        try:
            probability = float(raw_mid)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[clob_yes] Cannot parse mid=%r for token_id=%s: %s",
                raw_mid, token_id, exc
            )
            return None

        if not (0.0 < probability < 1.0):
            logger.warning(
                "[clob_yes] mid=%f outside (0,1) for token_id=%s — skipping",
                probability, token_id
            )
            return None

        snap = YesPriceSnapshot(
            probability=probability,
            timestamp=ts,
            source="clob_midpoint",
            is_provisional=True,
            token_id=token_id,
        )
        logger.debug(
            "[clob_yes] token_id=%s mid=%.4f ts=%.3f [PROVISIONAL]",
            token_id, probability, ts
        )
        return snap
