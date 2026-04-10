"""
truth/resolution_truth.py — Chainlink-based window resolution.

At window end (or shortly after), query Chainlink for BTC/USD.
Compare to the price recorded at window start.
Return explicit resolution status — never guess.

Canonical rule:
  - price_at_end > price_at_start => Up wins
  - price_at_end < price_at_start => Down wins
  - price_at_end == price_at_start => undefined (log, no resolution)

If Chainlink data is stale or missing at resolution time => UNRESOLVED.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from feeds.chainlink_client import ChainlinkClient
from truth.freshness import is_fresh


@dataclass
class Resolution:
    window_start: int
    price_at_start: Optional[float]
    price_at_end: Optional[float]
    oracle_age_at_resolution: Optional[float]
    outcome: Optional[str]   # "Up" | "Down" | None
    # "resolved_canonical" | "stale_oracle" | "missing_oracle" | "equal_price" | "missing_start"
    status: str
    resolved_at: float


def resolve_window(
    window_start: int,
    price_at_start: Optional[float],
    chainlink: ChainlinkClient,
    max_oracle_age: float,
) -> Resolution:
    """
    Attempt to resolve a completed window.
    price_at_start: Chainlink price recorded at window open.
    chainlink: live client to fetch current price.
    max_oracle_age: reject if oracle older than this many seconds.
    """
    now = time.time()

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
        )

    if snap.price > price_at_start:
        outcome = "Up"
        status = "resolved_canonical"
    elif snap.price < price_at_start:
        outcome = "Down"
        status = "resolved_canonical"
    else:
        outcome = None
        status = "equal_price"

    return Resolution(
        window_start=window_start,
        price_at_start=price_at_start,
        price_at_end=snap.price,
        oracle_age_at_resolution=age,
        outcome=outcome,
        status=status,
        resolved_at=now,
    )
