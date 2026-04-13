"""
book/orderbook_state.py — REST-based orderbook initialisation.

Before WebSocket snapshots arrive, fetch an initial book snapshot
from the CLOB REST API to seed the state.
This is a one-shot call, not a polling loop.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

from state import OrderbookSide

log = logging.getLogger(__name__)
_TIMEOUT = 10


def fetch_initial_book(clob_base: str, token_id: str, outcome: str) -> Optional[OrderbookSide]:
    """
    Fetch initial orderbook snapshot from CLOB REST.
    Returns populated OrderbookSide or None on error.
    """
    url = f"{clob_base}/book"
    params = {"token_id": token_id}
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        side = OrderbookSide(token_id=token_id, outcome=outcome)
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        side.apply_snapshot(bids, asks)
        log.info(
            "Initial book fetched: token=%s bids=%d asks=%d",
            token_id[:12], len(side.bids), len(side.asks),
        )
        return side
    except Exception as exc:
        log.warning("Initial book fetch failed for %s: %s", token_id[:12], exc)
        return None


def fetch_and_seed(clob_base: str, up_token_id: str, down_token_id: str, state) -> None:
    """
    Fetch initial books for both sides and write into state.
    Called once after market discovery before WebSocket connects.
    """
    up_book = fetch_initial_book(clob_base, up_token_id, "Up")
    dn_book = fetch_initial_book(clob_base, down_token_id, "Down")
    log.info(
        "REST seed: up_seed asks=%d bids=%d, down_seed asks=%d bids=%d",
        len(up_book.asks) if up_book else 0, len(up_book.bids) if up_book else 0,
        len(dn_book.asks) if dn_book else 0, len(dn_book.bids) if dn_book else 0,
    )
    with state._lock:
        if up_book:
            state.up_book = up_book
        if dn_book:
            state.down_book = dn_book
