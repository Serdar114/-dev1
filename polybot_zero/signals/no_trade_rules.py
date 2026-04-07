"""
no_trade_rules.py — Explicit no-trade rule evaluation.

Design:
  Every rule is a named function that returns (True, None) = TRADE OK or
  (False, NoTradeReasonCode, details_dict) = NO TRADE.
  Rules are evaluated in order. First failing rule stops evaluation.
  All rules are logged by the caller.
  Rules are separated into canonical-truth rules vs auxiliary rules.
  A canonical-truth rule failure is always a hard no-trade.
  An auxiliary rule failure may be configurable (but defaults to no-trade).
  Missing fields are no-trade, never guessed.

  No rule may accept missing data and return TRADE OK.
  No rule may silently pass when the required data is None.
"""

from __future__ import annotations
import logging
from typing import Optional, Tuple, List, Dict, Any

from loggingx.schemas import FeatureVector, FreshnessState, NoTradeReasonCode

logger = logging.getLogger("polybot.no_trade_rules")

# Result type: (should_trade, reason_code_or_none, details_or_none)
TradeVerdict = Tuple[bool, Optional[str], Optional[Dict[str, Any]]]

TRADE_OK: TradeVerdict = (True, None, None)


def _no_trade(reason: str, details: Dict[str, Any], canonical: bool = True) -> TradeVerdict:
    return (False, reason, {**details, "_canonical": canonical})


# ─────────────────────────────────────────────────────────────
# CANONICAL TRUTH RULES (Chainlink / window)
# ─────────────────────────────────────────────────────────────

def rule_chainlink_not_missing(fv: FeatureVector) -> TradeVerdict:
    """Chainlink must not be missing (we must have received at least one update)."""
    if fv.chainlink_now is None:
        return _no_trade(
            NoTradeReasonCode.CHAINLINK_MISSING,
            {"chainlink_now": None, "freshness": fv.chainlink_freshness},
            canonical=True,
        )
    return TRADE_OK


def rule_chainlink_not_stale(fv: FeatureVector) -> TradeVerdict:
    """Chainlink must be FRESH — STALE is not acceptable for canonical truth."""
    if fv.chainlink_freshness != FreshnessState.FRESH:
        return _no_trade(
            NoTradeReasonCode.CHAINLINK_STALE,
            {
                "freshness": fv.chainlink_freshness,
                "chainlink_now": fv.chainlink_now,
            },
            canonical=True,
        )
    return TRADE_OK


def rule_window_open_captured(fv: FeatureVector) -> TradeVerdict:
    """Window open Chainlink price must be captured (from resolution truth tracker)."""
    if fv.chainlink_open is None:
        return _no_trade(
            NoTradeReasonCode.WINDOW_OPEN_NOT_CAPTURED,
            {"chainlink_open": None},
            canonical=True,
        )
    return TRADE_OK


def rule_window_not_expired(fv: FeatureVector, min_secs: float = 30.0) -> TradeVerdict:
    """Must have at least min_secs remaining before window close."""
    if fv.secs_to_expiry is None:
        return _no_trade(
            NoTradeReasonCode.MARKET_NOT_LIVE,
            {"secs_to_expiry": None},
            canonical=True,
        )
    if fv.secs_to_expiry <= min_secs:
        return _no_trade(
            NoTradeReasonCode.TOO_CLOSE_TO_EXPIRY,
            {"secs_to_expiry": fv.secs_to_expiry, "min_secs": min_secs},
            canonical=True,
        )
    return TRADE_OK


# ─────────────────────────────────────────────────────────────
# METADATA RULES
# ─────────────────────────────────────────────────────────────

def rule_metadata_complete(fv: FeatureVector) -> TradeVerdict:
    """All required metadata fields must be present."""
    missing = []
    if fv.tick_size is None:
        missing.append("tick_size")
    if fv.min_order_size is None:
        missing.append("min_order_size")
    if missing:
        return _no_trade(
            NoTradeReasonCode.METADATA_INCOMPLETE,
            {"missing_fields": missing},
            canonical=True,
        )
    return TRADE_OK


def rule_fee_provenance_known(fv: FeatureVector) -> TradeVerdict:
    """Fee rate must come from a known source, not guessed or defaulted."""
    if fv.fee_rate is None:
        return _no_trade(
            NoTradeReasonCode.FEE_PROVENANCE_UNCLEAR,
            {"fee_rate": None, "fee_source": fv.fee_source},
            canonical=True,
        )
    if fv.fee_source is None:
        return _no_trade(
            NoTradeReasonCode.FEE_PROVENANCE_UNCLEAR,
            {"fee_rate": fv.fee_rate, "fee_source": None, "note": "fee_rate present but no source"},
            canonical=True,
        )
    return TRADE_OK


# ─────────────────────────────────────────────────────────────
# ORDER BOOK RULES
# ─────────────────────────────────────────────────────────────

def rule_book_present(fv: FeatureVector) -> TradeVerdict:
    """Both up and down order books must have valid best ask."""
    if fv.up_best_ask is None:
        return _no_trade(
            NoTradeReasonCode.BOOK_INSUFFICIENT,
            {"token": "up", "up_best_ask": None},
            canonical=False,
        )
    if fv.down_best_ask is None:
        return _no_trade(
            NoTradeReasonCode.BOOK_INSUFFICIENT,
            {"token": "down", "down_best_ask": None},
            canonical=False,
        )
    return TRADE_OK


def rule_spread_acceptable(fv: FeatureVector, max_spread: float = 0.10) -> TradeVerdict:
    """Spread on the entry side must be within bounds."""
    if fv.up_spread is not None and fv.up_spread > max_spread:
        return _no_trade(
            NoTradeReasonCode.SPREAD_TOO_WIDE,
            {"token": "up", "spread": fv.up_spread, "max_spread": max_spread},
            canonical=False,
        )
    if fv.down_spread is not None and fv.down_spread > max_spread:
        return _no_trade(
            NoTradeReasonCode.SPREAD_TOO_WIDE,
            {"token": "down", "spread": fv.down_spread, "max_spread": max_spread},
            canonical=False,
        )
    return TRADE_OK


def rule_pair_sum_valid(
    fv: FeatureVector,
    pair_sum_min: float = 0.90,
    pair_sum_max: float = 1.20,
) -> TradeVerdict:
    """pair_sum must be within bounds. Outside = structural anomaly."""
    if fv.pair_sum_best_ask is None:
        return _no_trade(
            NoTradeReasonCode.PAIR_SUM_SUSPICIOUS,
            {"pair_sum": None, "reason": "cannot_compute"},
            canonical=False,
        )
    if not (pair_sum_min <= fv.pair_sum_best_ask <= pair_sum_max):
        return _no_trade(
            NoTradeReasonCode.PAIR_SUM_SUSPICIOUS,
            {
                "pair_sum": fv.pair_sum_best_ask,
                "min": pair_sum_min,
                "max": pair_sum_max,
            },
            canonical=False,
        )
    return TRADE_OK


# ─────────────────────────────────────────────────────────────
# RULE SET EVALUATION
# ─────────────────────────────────────────────────────────────

def evaluate_all(
    fv: FeatureVector,
    min_secs_to_expiry: float = 30.0,
    max_spread: float = 0.10,
    pair_sum_min: float = 0.90,
    pair_sum_max: float = 1.20,
) -> TradeVerdict:
    """
    Evaluate all no-trade rules in canonical-first order.
    Returns the first failing rule, or TRADE_OK if all pass.
    Caller is responsible for logging the result.
    """

    # Canonical rules — hard stops
    for rule_fn in [
        rule_chainlink_not_missing,
        rule_chainlink_not_stale,
        rule_window_open_captured,
        lambda fv: rule_window_not_expired(fv, min_secs_to_expiry),
        rule_metadata_complete,
        rule_fee_provenance_known,
    ]:
        result = rule_fn(fv)
        if not result[0]:
            return result

    # Structural rules
    for rule_fn in [
        rule_book_present,
        lambda fv: rule_spread_acceptable(fv, max_spread),
        lambda fv: rule_pair_sum_valid(fv, pair_sum_min, pair_sum_max),
    ]:
        result = rule_fn(fv)
        if not result[0]:
            return result

    return TRADE_OK


def format_verdict(verdict: TradeVerdict) -> str:
    """Human-readable verdict string for logging."""
    should_trade, reason, details = verdict
    if should_trade:
        return "TRADE_OK"
    return f"NO_TRADE reason={reason} details={details}"
