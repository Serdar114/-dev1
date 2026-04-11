"""
metadata/fee_schedule.py — Official Polymarket taker fee formula.

PATCH 1 — corrected from flat-fee model to price-dependent formula.

Official Polymarket CLOB taker fee (Crypto markets):
  fee_per_unit = feeRate * p * (1 - p)

Where:
  p         = entry price (0 < p < 1)
  feeRate   = from market feeSchedule (e.g. 0.02 for 2%)
  fee is charged at order execution, not at resolution

Per-unit economics (size = 1 token):
  effective_cost     = p + fee_per_unit
  net_payoff_if_win  = 1.0 - p - fee_per_unit      = (1-p)(1 - feeRate*p)
  net_payoff_if_lose = -(p + fee_per_unit)

Why this matters vs the old flat model (1 - p - feeRate):
  Old at p=0.50, rate=0.02:  net_win = 0.48,  fee = 0.02  (WRONG — overestimates fee 4x)
  New at p=0.50, rate=0.02:  net_win = 0.495, fee = 0.005 (CORRECT)

  At p=0.50 the fee is feeRate * 0.25 = 0.5% for a 2% rate.
  At p=0.10 the fee is feeRate * 0.09 = 0.18% — extreme prices are cheaper.
  The maximum fee always occurs at p=0.50 (maximum price uncertainty).

Note on net_payoff_if_lose:
  Old model: net_lose = -p  (ignores that fee is also paid at entry)
  New model: net_lose = -(p + fee_per_unit)  (fee is sunk cost even on a loss)
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
    fee_per_unit: float        # feeRate * p * (1 - p) — the actual fee charged
    fee_provenance: str        # "gamma_fee_schedule" | "clob_response" | "config_default" | "missing"
    net_payoff_if_win: float   # 1 - p - fee_per_unit
    net_payoff_if_lose: float  # -(p + fee_per_unit)
    size_usdc: float


def compute_economics(
    entry_price: float,
    metadata: Optional[MarketMetadata],
    fallback_fee_rate: float,
    size_usdc: float = 1.0,
) -> FillEconomics:
    """
    Compute per-unit taker fill economics using official Polymarket fee formula.

    entry_price:       best ask for chosen side (0 < p < 1)
    metadata:          MarketMetadata — canonical fee source when available
    fallback_fee_rate: used only if metadata is None or fee is missing;
                       provenance is explicitly marked "config_default"
    size_usdc:         notional size (informational; per-unit math is independent)
    """
    if metadata is not None and metadata.taker_fee_rate is not None:
        fee_rate = metadata.taker_fee_rate
        fee_provenance = metadata.fee_provenance
    else:
        fee_rate = fallback_fee_rate
        fee_provenance = "config_default" if metadata is None else "missing"

    # Official formula: fee is price-dependent, not flat
    fee_per_unit = fee_rate * entry_price * (1.0 - entry_price)

    net_win = 1.0 - entry_price - fee_per_unit
    net_lose = -(entry_price + fee_per_unit)

    return FillEconomics(
        entry_price=entry_price,
        fee_rate=fee_rate,
        fee_per_unit=round(fee_per_unit, 8),
        fee_provenance=fee_provenance,
        net_payoff_if_win=round(net_win, 8),
        net_payoff_if_lose=round(net_lose, 8),
        size_usdc=size_usdc,
    )
