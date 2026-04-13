"""
book/price_views.py — Derived price views from orderbook state.

All functions are pure (no side effects).
Input: SystemState snapshot.
Output: floats or None — never raises.
"""
from __future__ import annotations

from typing import Optional

from state import OrderbookSide, SystemState


def best_ask(book: OrderbookSide) -> Optional[float]:
    ba = book.best_ask()
    return ba[0] if ba else None


def best_bid(book: OrderbookSide) -> Optional[float]:
    bb = book.best_bid()
    return bb[0] if bb else None


def spread(book: OrderbookSide) -> Optional[float]:
    return book.spread()


def pair_sum_ask(state: SystemState) -> Optional[float]:
    return state.pair_sum_ask()


def pair_sum_bid(state: SystemState) -> Optional[float]:
    return state.pair_sum_bid()


def basis_pct(state: SystemState) -> Optional[float]:
    return state.basis_pct()


def format_price(p: Optional[float]) -> str:
    if p is None:
        return "--"
    return f"{p:.4f}"


def format_usd(p: Optional[float]) -> str:
    if p is None:
        return "--"
    return f"${p:,.2f}"
