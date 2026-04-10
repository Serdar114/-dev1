"""
paper/fill_model.py — Fill economics model for taker entries.

Imported by paper/hypothetical_entry.py and paper/paper_executor.py.
See metadata/fee_schedule.py for the economic formula.
"""
from __future__ import annotations

from metadata.fee_schedule import FillEconomics, compute_economics
from state import MarketMetadata
from typing import Optional


def taker_economics(
    side: str,
    entry_price: float,
    metadata: Optional[MarketMetadata],
    fallback_fee_rate: float,
    size_usdc: float = 1.0,
) -> FillEconomics:
    """
    Compute taker fill economics for given side and entry price.
    side: "Up" | "Down" (informational only, economics same formula)
    """
    return compute_economics(
        entry_price=entry_price,
        metadata=metadata,
        fallback_fee_rate=fallback_fee_rate,
        size_usdc=size_usdc,
    )
