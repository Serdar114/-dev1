"""
Polymarket CLOB REST fallback for order book snapshots.

Used only when the WebSocket has no snapshot for a given token yet.
Endpoint: https://clob.polymarket.com/book?token_id=<id>
"""
import time
from typing import Optional

import requests

from schemas import BookSnapshot

CLOB_REST_BASE = "https://clob.polymarket.com"


def get_book(
    token_id: str,
    session: Optional[requests.Session] = None,
) -> Optional[BookSnapshot]:
    """
    Fetch the current order book for token_id via REST.
    Returns None on any error (network, parse, empty book).
    """
    url = f"{CLOB_REST_BASE}/book"
    params = {"token_id": token_id}
    s = session if session is not None else requests.Session()

    try:
        resp = s.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    now_ms = int(time.time() * 1000)

    bids: dict = {}
    asks: dict = {}

    for entry in data.get("bids", []):
        p, sv = entry.get("price"), entry.get("size")
        if p is not None and sv is not None:
            try:
                bids[str(p)] = float(sv)
            except (ValueError, TypeError):
                pass

    for entry in data.get("asks", []):
        p, sv = entry.get("price"), entry.get("size")
        if p is not None and sv is not None:
            try:
                asks[str(p)] = float(sv)
            except (ValueError, TypeError):
                pass

    live_bids = [float(p) for p, s in bids.items() if s > 0]
    live_asks = [float(p) for p, s in asks.items() if s > 0]

    best_bid: Optional[float] = max(live_bids) if live_bids else None
    best_ask: Optional[float] = min(live_asks) if live_asks else None

    mid: Optional[float] = None
    spread: Optional[float] = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid

    return BookSnapshot(
        ts_local=now_ms,
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        source="rest",
    )
