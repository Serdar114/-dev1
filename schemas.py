"""
schemas.py — shared dataclasses for the Polymarket BTC up/down feasibility logger.

PATCH additions (dual reference, vol/sigma, edge layer) are clearly marked.
All new fields carry Optional defaults so the base logger call-sites are
unaffected.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional


# ── market discovery ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MarketPair:
    """A locked Up/Down token pair for one resolution window."""
    family: str           # "5m" | "15m"
    condition_id: str
    slug: str
    end_time_ms: int
    up_token_id: str
    down_token_id: str
    start_price: Optional[float]   # "Price to Beat" / barrier; None if unavailable
    fee_rate: Optional[float]
    fee_c: Optional[float]


# ── order book ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BookSnapshot:
    ts_local: int
    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    mid: Optional[float]
    spread: Optional[float]
    source: str   # "ws" | "rest"


# ── legacy external price (kept for backward-compat with logger call-sites) ───

@dataclass(frozen=True)
class ExternalPriceSnapshot:
    ts_local: int
    price: Optional[float]
    source_ts: Optional[int]
    stale: bool


# ── PATCH: dual reference feed ────────────────────────────────────────────────

@dataclass(frozen=True)
class DualReferenceSnapshot:
    ts_local: int

    binance_price: Optional[float]
    binance_source_ts: Optional[int]
    binance_local_ts: Optional[int]
    binance_stale: bool

    chainlink_price: Optional[float]
    chainlink_source_ts: Optional[int]
    chainlink_local_ts: Optional[int]
    chainlink_stale: bool

    # ((chainlink - binance) / binance) * 10_000; None unless both feeds live
    basis_bps: Optional[float]
    # binance_source_ts - chainlink_source_ts; None unless both feeds live
    lag_ms: Optional[int]


# ── joined observation ────────────────────────────────────────────────────────

@dataclass
class JoinedObservation:
    # ── core ─────────────────────────────────────────────────────────────────
    ts_local: int
    family: str           # "5m" | "15m"
    condition_id: str
    slug: str
    end_time_ms: int
    seconds_to_expiry: Optional[float]

    # ── up side ───────────────────────────────────────────────────────────────
    up_token_id: Optional[str]
    up_best_bid: Optional[float]
    up_best_ask: Optional[float]
    up_mid: Optional[float]
    up_spread: Optional[float]
    up_book_source: Optional[str]

    # ── down side ─────────────────────────────────────────────────────────────
    down_token_id: Optional[str]
    down_best_bid: Optional[float]
    down_best_ask: Optional[float]
    down_mid: Optional[float]
    down_spread: Optional[float]
    down_book_source: Optional[str]

    # ── legacy external price ─────────────────────────────────────────────────
    external_btc_price: Optional[float] = None

    # ── PATCH: dual reference feed ────────────────────────────────────────────
    external_btc_price_binance: Optional[float] = None
    external_btc_price_chainlink: Optional[float] = None
    external_basis_bps: Optional[float] = None
    external_lag_ms: Optional[int] = None
    stale_binance: Optional[bool] = None
    stale_chainlink: Optional[bool] = None

    # ── PATCH: market metadata ────────────────────────────────────────────────
    barrier_price: Optional[float] = None
    seconds_to_resolution: Optional[float] = None

    # ── PATCH: realized vol / sigma distance ──────────────────────────────────
    realized_vol_60m: Optional[float] = None
    sigma_distance: Optional[float] = None
    fair_up_prob: Optional[float] = None

    # ── PATCH: edge layer ─────────────────────────────────────────────────────
    edge_up_raw: Optional[float] = None
    edge_down_raw: Optional[float] = None
    effective_fee_estimate: Optional[float] = None
    edge_up_net: Optional[float] = None
    edge_down_net: Optional[float] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
