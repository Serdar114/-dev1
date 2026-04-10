"""
metadata/fee_schedule.py — Fee schedule with explicit provenance.

Provides compute_net_payoff() used by hypothetical entry and paper executor.
Every call records provenance so logs are auditable.

Net payoff model (taker, hold-to-resolution):
  - Buy S units of UP at price P (taker)
  - Cost = S * P  [USDC]
  - If UP wins: receive S * 1.0, pay fee = S * fee_rate
    Net profit = S * (1 - P - fee_rate)
  - If UP loses: receive 0
    Net loss   = S * P

Per-unit (S=1):
  net_payoff_if_win  = 1.0 - entry_price - fee_rate
  net_payoff_if_lose = -entry_price
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from state import MarketMetadata


@dataclass
class FillEconomics:
    """Per-unit economics for a hypothetical taker fill."""
    entry_price: float
    fee_rate: float
    fee_provenance: str  # "canonical" | "config_default" | "missing"
    net_payoff_if_win: float
    net_payoff_if_lose: float
    size_usdc: float


def compute_economics(
    entry_price: float,
    metadata: Optional[MarketMetadata],
    fallback_fee_rate: float,
    size_usdc: float = 1.0,
) -> FillEconomics:
    """
    Compute fill economics for a taker entry.

    entry_price: best ask for the chosen side (0 < price < 1)
    metadata:    MarketMetadata (may be None if not yet fetched)
    fallback_fee_rate: used only if metadata is None or fee is missing
    size_usdc:   hypothetical notional in USDC (for scaling, not used in per-unit calcs)
    """
    if metadata is not None and metadata.taker_fee_rate is not None:
        fee_rate = metadata.taker_fee_rate
        fee_provenance = metadata.fee_provenance
    else:
        fee_rate = fallback_fee_rate
        fee_provenance = "config_default" if metadata is None else "missing"

    net_win = 1.0 - entry_price - fee_rate
    net_lose = -entry_price

    return FillEconomics(
        entry_price=entry_price,
        fee_rate=fee_rate,
        fee_provenance=fee_provenance,
        net_payoff_if_win=net_win,
        net_payoff_if_lose=net_lose,
        size_usdc=size_usdc,
    )
