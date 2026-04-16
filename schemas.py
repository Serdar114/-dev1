"""
Data schemas for the Polymarket BTC observation logger.
All fields that cannot be computed are left as None — no dummy values.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MarketPair:
    family: str                    # "5m" | "15m"
    condition_id: str
    slug: str
    end_time_ms: int
    up_token_id: str
    down_token_id: str
    start_price: Optional[float]   # "Price to Beat" / barrier
    fee_rate: Optional[float]
    fee_c: Optional[float]


@dataclass(frozen=True)
class BookSnapshot:
    ts_local: int
    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    mid: Optional[float]
    spread: Optional[float]
    source: str                    # "ws" | "rest"


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
    basis_bps: Optional[float]     # ((chainlink-binance)/binance)*10000
    lag_ms: Optional[int]          # binance_source_ts - chainlink_source_ts


@dataclass
class JoinedObservation:
    # core
    ts_local: int
    family: str
    condition_id: str
    slug: str
    end_time_ms: int
    seconds_to_expiry: Optional[float]
    # up side
    up_token_id: Optional[str]
    up_best_bid: Optional[float]
    up_best_ask: Optional[float]
    up_mid: Optional[float]
    up_spread: Optional[float]
    up_book_source: Optional[str]
    # down side
    down_token_id: Optional[str]
    down_best_bid: Optional[float]
    down_best_ask: Optional[float]
    down_mid: Optional[float]
    down_spread: Optional[float]
    down_book_source: Optional[str]
    # dual reference (nullable defaults)
    external_btc_price_binance: Optional[float] = None
    external_btc_price_chainlink: Optional[float] = None
    external_basis_bps: Optional[float] = None
    external_lag_ms: Optional[int] = None
    stale_binance: Optional[bool] = None
    stale_chainlink: Optional[bool] = None
    # market metadata
    barrier_price: Optional[float] = None
    seconds_to_resolution: Optional[float] = None
    # vol & sigma
    realized_vol_60m: Optional[float] = None
    sigma_distance: Optional[float] = None
    fair_up_prob: Optional[float] = None
    # edge layer
    edge_up_raw: Optional[float] = None
    edge_down_raw: Optional[float] = None
    effective_fee_estimate: Optional[float] = None
    edge_up_net: Optional[float] = None
    edge_down_net: Optional[float] = None
