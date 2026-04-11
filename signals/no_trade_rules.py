"""
signals/no_trade_rules.py — Explicit no-trade condition evaluation.

Every condition is checked independently.
Every active condition is returned as a labelled string.
No condition silently passes. No condition is suppressed.
Empty list means all conditions passed.

Called each tick by the runner.
"""
from __future__ import annotations

from typing import List, Optional

from state import MarketMetadata, SystemState
from truth.freshness import is_fresh


def evaluate(state: SystemState, config: dict) -> List[str]:
    """
    Evaluate all no-trade conditions against current system state.
    Returns list of active no-trade reason strings.
    Empty list = no active no-trade conditions.

    Caller must acquire state._lock or pass in a snapshot.
    """
    reasons: List[str] = []
    cl_cfg = config["chainlink"]
    meas_cfg = config["measurement"]

    # -----------------------------------------------------------------------
    # 1. Chainlink truth
    # -----------------------------------------------------------------------
    cl_age = state.chainlink.age_seconds()
    if state.chainlink.price is None:
        reasons.append("chainlink_missing")
    elif not is_fresh(cl_age, float(cl_cfg["max_age_seconds"])):
        reasons.append(f"chainlink_stale:{cl_age:.0f}s")

    # -----------------------------------------------------------------------
    # 2. Market / metadata
    # -----------------------------------------------------------------------
    if state.market is None:
        reasons.append("market_not_found")
    else:
        if state.window.start == 0:
            reasons.append("window_not_set")

    if state.metadata is None:
        reasons.append("metadata_missing")
    else:
        meta: MarketMetadata = state.metadata
        if meta.tick_size is None:
            reasons.append("tick_size_missing")
        if meta.min_order_size is None:
            reasons.append("min_order_size_missing")
        if meta.taker_fee_rate is None:
            reasons.append("fee_rate_missing")
        elif meta.fee_provenance == "missing":
            reasons.append("fee_provenance_missing")
        # Note: "fallback_config" is allowed but flagged in the hypothetical entry log.
        # "canonical_market_object" is preferred. "missing" blocks.

    # -----------------------------------------------------------------------
    # 3. Orderbook availability
    # -----------------------------------------------------------------------
    if not state.up_book.snapshot_received:
        reasons.append("up_book_no_snapshot")
    elif not state.up_book.has_asks():
        reasons.append("up_book_empty_asks")

    if not state.down_book.snapshot_received:
        reasons.append("down_book_no_snapshot")
    elif not state.down_book.has_asks():
        reasons.append("down_book_empty_asks")

    # -----------------------------------------------------------------------
    # 4. Basis stability (warning, not hard block in measurement mode)
    # -----------------------------------------------------------------------
    basis = state.basis_pct()
    max_basis = float(meas_cfg.get("basis_warn_pct", 0.5))
    if basis is None:
        reasons.append("basis_unavailable")
    elif abs(basis) > max_basis:
        reasons.append(f"basis_unstable:{basis:+.3f}%")

    # -----------------------------------------------------------------------
    # 5. Spread check
    # -----------------------------------------------------------------------
    max_spread = float(meas_cfg.get("max_spread", 0.05))
    up_spread = state.up_book.spread()
    dn_spread = state.down_book.spread()
    if up_spread is not None and up_spread > max_spread:
        reasons.append(f"up_spread_wide:{up_spread:.4f}")
    if dn_spread is not None and dn_spread > max_spread:
        reasons.append(f"down_spread_wide:{dn_spread:.4f}")

    # -----------------------------------------------------------------------
    # 6. Pair sum check
    # -----------------------------------------------------------------------
    ps = state.pair_sum_ask()
    ps_min = float(meas_cfg.get("pair_sum_min", 0.98))
    ps_max = float(meas_cfg.get("pair_sum_max", 1.06))
    if ps is None:
        reasons.append("pair_sum_unavailable")
    elif ps < ps_min or ps > ps_max:
        reasons.append(f"pair_sum_bad:{ps:.4f}")

    # -----------------------------------------------------------------------
    # 7. Window timing
    # -----------------------------------------------------------------------
    min_secs = int(meas_cfg.get("min_secs_to_expiry", 60))
    secs = state.window.secs_to_expiry()
    if secs < min_secs:
        reasons.append(f"window_expiring_soon:{secs}s")

    return reasons
