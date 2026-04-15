"""
clob_rest.py — Polymarket CLOB REST fallback for order book snapshots.

Used when the WebSocket book state is unavailable (e.g. WS reconnecting).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from schemas import BookSnapshot

log = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"


def get_book(
    token_id: str,
    session: Optional[requests.Session] = None,
    timeout: int = 5,
) -> Optional[BookSnapshot]:
    """
    Fetch order book for token_id via REST.
    Returns None on any failure — callers must handle null.
    """
    sess = session or requests.Session()
    ts_local = int(time.time() * 1000)

    try:
        resp = sess.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("clob_rest: book fetch failed token=%.8s: %s", token_id, exc)
        return None

    bids = data.get("bids") or []
    asks = data.get("asks") or []

    def _best_bid(levels) -> Optional[float]:
        try:
            return max(float(l["price"]) for l in levels) if levels else None
        except Exception:
            return None

    def _best_ask(levels) -> Optional[float]:
        try:
            return min(float(l["price"]) for l in levels) if levels else None
        except Exception:
            return None

    best_bid = _best_bid(bids)
    best_ask = _best_ask(asks)
    mid = ((best_bid + best_ask) / 2.0) if (best_bid is not None and best_ask is not None) else None
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None

    return BookSnapshot(
        ts_local=ts_local,
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        source="rest",
    )
