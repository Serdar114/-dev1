"""
signals/feature_builder.py — Feature extraction from system state snapshot.

Produces a Features dict used for bucket classification and hypothetical entry logging.
All fields are Optional — missing data is explicit, never imputed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from state import SystemState


@dataclass
class Features:
    # Time
    ts: float
    window_id: int
    secs_to_expiry: int

    # Chainlink
    chainlink_price: Optional[float]
    chainlink_age: Optional[float]

    # Binance
    binance_bid: Optional[float]
    binance_ask: Optional[float]
    binance_mid: Optional[float]
    binance_age: Optional[float]

    # Basis
    basis_abs: Optional[float]
    basis_pct: Optional[float]

    # Orderbook — Up side
    up_best_ask: Optional[float]
    up_best_bid: Optional[float]
    up_spread: Optional[float]
    up_ask_size: Optional[float]
    up_bid_size: Optional[float]

    # Orderbook — Down side
    dn_best_ask: Optional[float]
    dn_best_bid: Optional[float]
    dn_spread: Optional[float]
    dn_ask_size: Optional[float]
    dn_bid_size: Optional[float]

    # Pair
    pair_sum_ask: Optional[float]
    pair_sum_bid: Optional[float]


def build(state: SystemState) -> Features:
    """
    Extract features from SystemState.
    Must be called under state._lock or on a snapshot copy.
    """
    now = time.time()

    up_ba = state.up_book.best_ask()
    up_bb = state.up_book.best_bid()
    dn_ba = state.down_book.best_ask()
    dn_bb = state.down_book.best_bid()

    cl = state.chainlink
    bn = state.binance
    bn_mid = bn.mid()

    basis_abs: Optional[float] = None
    basis_pct: Optional[float] = None
    if cl.price is not None and bn_mid is not None and cl.price > 0:
        basis_abs = bn_mid - cl.price
        basis_pct = basis_abs / cl.price * 100.0

    return Features(
        ts=now,
        window_id=state.window.start,
        secs_to_expiry=state.window.secs_to_expiry(),

        chainlink_price=cl.price,
        chainlink_age=cl.age_seconds(),

        binance_bid=bn.bid,
        binance_ask=bn.ask,
        binance_mid=bn_mid,
        binance_age=bn.age_seconds(),

        basis_abs=basis_abs,
        basis_pct=basis_pct,

        up_best_ask=up_ba[0] if up_ba else None,
        up_best_bid=up_bb[0] if up_bb else None,
        up_spread=state.up_book.spread(),
        up_ask_size=up_ba[1] if up_ba else None,
        up_bid_size=up_bb[1] if up_bb else None,

        dn_best_ask=dn_ba[0] if dn_ba else None,
        dn_best_bid=dn_bb[0] if dn_bb else None,
        dn_spread=state.down_book.spread(),
        dn_ask_size=dn_ba[1] if dn_ba else None,
        dn_bid_size=dn_bb[1] if dn_bb else None,

        pair_sum_ask=state.pair_sum_ask(),
        pair_sum_bid=state.pair_sum_bid(),
    )
