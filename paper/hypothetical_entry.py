"""
paper/hypothetical_entry.py — Hypothetical (measurement-mode) entry recorder.

Records what a taker entry would look like if executed.
Does NOT execute anything. Does NOT alter system state.
Outputs a HypotheticalEntryEvent for logging.

Called by the runner when no-trade rules pass.
One record per tick (or per window, depending on runner policy).
"""
from __future__ import annotations

from typing import Optional

from loggingx.schemas import HypotheticalEntryEvent
from paper.fill_model import taker_economics
from signals.bucket_probe import classify
from signals.feature_builder import Features
from state import MarketMetadata


def record_hypothetical(
    features: Features,
    metadata: Optional[MarketMetadata],
    fallback_fee_rate: float,
    window_id: int,
) -> Optional[HypotheticalEntryEvent]:
    """
    Build a hypothetical entry event for both Up and Down sides.
    Returns the BETTER side (higher net_payoff_if_win) or None if
    no valid ask prices are available.

    In measurement mode, "better" is purely for logging priority.
    Both sides are still logged via the runner's double-call pattern.
    """
    if features.up_best_ask is None or features.dn_best_ask is None:
        return None

    up_econ = taker_economics(
        side="Up",
        entry_price=features.up_best_ask,
        metadata=metadata,
        fallback_fee_rate=fallback_fee_rate,
    )
    bucket = classify(features)

    return HypotheticalEntryEvent(
        window_id=window_id,
        side="Up",
        entry_price=up_econ.entry_price,
        entry_size_usdc=up_econ.size_usdc,
        fee_rate=up_econ.fee_rate,
        fee_provenance=up_econ.fee_provenance,
        net_payoff_if_win=up_econ.net_payoff_if_win,
        net_payoff_if_lose=up_econ.net_payoff_if_lose,
        chainlink_price=features.chainlink_price,
        binance_mid=features.binance_mid,
        basis_pct=features.basis_pct,
        pair_sum_ask=features.pair_sum_ask,
        bucket=bucket,
    )


def record_both_sides(
    features: Features,
    metadata: Optional[MarketMetadata],
    fallback_fee_rate: float,
    window_id: int,
) -> list:
    """
    Return a list of HypotheticalEntryEvents for both Up and Down sides.
    May be empty if ask prices are missing.
    """
    events = []
    bucket = classify(features)

    for side, ask_price in (("Up", features.up_best_ask), ("Down", features.dn_best_ask)):
        if ask_price is None:
            continue
        econ = taker_economics(
            side=side,
            entry_price=ask_price,
            metadata=metadata,
            fallback_fee_rate=fallback_fee_rate,
        )
        events.append(HypotheticalEntryEvent(
            window_id=window_id,
            side=side,
            entry_price=econ.entry_price,
            entry_size_usdc=econ.size_usdc,
            fee_rate=econ.fee_rate,
            fee_provenance=econ.fee_provenance,
            net_payoff_if_win=econ.net_payoff_if_win,
            net_payoff_if_lose=econ.net_payoff_if_lose,
            chainlink_price=features.chainlink_price,
            binance_mid=features.binance_mid,
            basis_pct=features.basis_pct,
            pair_sum_ask=features.pair_sum_ask,
            bucket=bucket,
        ))
    return events
