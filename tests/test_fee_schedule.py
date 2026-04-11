"""
tests/test_fee_schedule.py — Unit tests for fee economics (post-PATCH 1).

Official Polymarket formula:
  fee_per_unit = feeRate * p * (1 - p)
  net_payoff_if_win  = 1 - p - fee_per_unit
  net_payoff_if_lose = -(p + fee_per_unit)

Key numeric checks at p=0.50, rate=0.02:
  fee_per_unit = 0.02 * 0.50 * 0.50 = 0.005
  net_win      = 1 - 0.50 - 0.005   = 0.495   (old flat model gave 0.48 — 4x overestimate)
  net_lose     = -(0.50 + 0.005)    = -0.505   (old flat model gave -0.50 — ignored fee on loss)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from metadata.fee_schedule import compute_economics, FillEconomics
from state import MarketMetadata


# ---------------------------------------------------------------------------
# Provenance routing
# ---------------------------------------------------------------------------

def test_canonical_fee_used_when_available():
    meta = MarketMetadata(taker_fee_rate=0.02, fee_provenance="gamma_fee_schedule",
                          fee_schedule_present=True)
    econ = compute_economics(entry_price=0.50, metadata=meta, fallback_fee_rate=0.99)
    assert econ.fee_rate == 0.02
    assert econ.fee_provenance == "gamma_fee_schedule"


def test_fallback_fee_used_when_metadata_none():
    econ = compute_economics(entry_price=0.50, metadata=None, fallback_fee_rate=0.02)
    assert econ.fee_rate == 0.02
    assert econ.fee_provenance == "config_default"


def test_fallback_fee_used_when_fee_missing_in_metadata():
    meta = MarketMetadata(taker_fee_rate=None, fee_provenance="missing")
    econ = compute_economics(entry_price=0.50, metadata=meta, fallback_fee_rate=0.02)
    assert econ.fee_rate == 0.02
    assert econ.fee_provenance == "missing"


def test_provenance_config_default_not_canonical():
    econ = compute_economics(entry_price=0.50, metadata=None, fallback_fee_rate=0.02)
    assert econ.fee_provenance not in ("canonical", "gamma_fee_schedule", "clob_response")


# ---------------------------------------------------------------------------
# Official fee formula: fee_per_unit = rate * p * (1-p)
# ---------------------------------------------------------------------------

def test_fee_per_unit_at_mid_price():
    """At p=0.50, fee_per_unit = 0.02 * 0.25 = 0.005 (not flat 0.02)."""
    meta = MarketMetadata(taker_fee_rate=0.02, fee_provenance="gamma_fee_schedule")
    econ = compute_economics(entry_price=0.50, metadata=meta, fallback_fee_rate=0.0)
    assert abs(econ.fee_per_unit - 0.005) < 1e-9


def test_fee_per_unit_lower_at_extreme_price():
    """At p=0.10, fee = 0.02 * 0.09 = 0.0018 — much lower than at p=0.50."""
    meta = MarketMetadata(taker_fee_rate=0.02, fee_provenance="gamma_fee_schedule")
    econ_mid = compute_economics(entry_price=0.50, metadata=meta, fallback_fee_rate=0.0)
    econ_low = compute_economics(entry_price=0.10, metadata=meta, fallback_fee_rate=0.0)
    assert econ_low.fee_per_unit < econ_mid.fee_per_unit


def test_fee_maximised_at_p_half():
    """p=0.50 is where fee_per_unit = rate/4 (global maximum of p*(1-p))."""
    meta = MarketMetadata(taker_fee_rate=0.02, fee_provenance="gamma_fee_schedule")
    prices = [0.10, 0.25, 0.40, 0.50, 0.60, 0.75, 0.90]
    fees = [compute_economics(p, meta, 0.0).fee_per_unit for p in prices]
    max_idx = fees.index(max(fees))
    assert prices[max_idx] == 0.50


# ---------------------------------------------------------------------------
# Net payoff values at p=0.50, rate=0.02
# ---------------------------------------------------------------------------

def test_net_payoff_formula_at_mid():
    meta = MarketMetadata(taker_fee_rate=0.02, fee_provenance="gamma_fee_schedule")
    econ = compute_economics(entry_price=0.50, metadata=meta, fallback_fee_rate=0.0)
    # fee_per_unit = 0.005
    assert abs(econ.net_payoff_if_win  - 0.495) < 1e-7
    assert abs(econ.net_payoff_if_lose - (-0.505)) < 1e-7


def test_net_lose_includes_fee():
    """net_payoff_if_lose must be -(p + fee_per_unit), not just -p."""
    meta = MarketMetadata(taker_fee_rate=0.02, fee_provenance="gamma_fee_schedule")
    econ = compute_economics(entry_price=0.50, metadata=meta, fallback_fee_rate=0.0)
    assert econ.net_payoff_if_lose < -0.50  # strictly worse than -p alone


def test_net_payoff_formula_at_low_price():
    """p=0.10, rate=0.02: fee=0.0018, net_win=0.8982, net_lose=-0.1018."""
    meta = MarketMetadata(taker_fee_rate=0.02, fee_provenance="gamma_fee_schedule")
    econ = compute_economics(entry_price=0.10, metadata=meta, fallback_fee_rate=0.0)
    expected_fee = 0.02 * 0.10 * 0.90
    assert abs(econ.fee_per_unit - expected_fee) < 1e-9
    assert abs(econ.net_payoff_if_win  - (1 - 0.10 - expected_fee)) < 1e-9
    assert abs(econ.net_payoff_if_lose - (-(0.10 + expected_fee))) < 1e-9


def test_net_win_always_positive_for_valid_prices():
    """
    With the price-dependent formula, net_win = (1-p)(1 - rate*p) > 0
    for any 0 < p < 1 and 0 < rate < 1.
    This differs from the flat model where high prices gave negative net_win.
    """
    meta = MarketMetadata(taker_fee_rate=0.02, fee_provenance="gamma_fee_schedule")
    for p in [0.05, 0.10, 0.50, 0.90, 0.95, 0.99]:
        econ = compute_economics(entry_price=p, metadata=meta, fallback_fee_rate=0.0)
        assert econ.net_payoff_if_win > 0, f"net_win negative at p={p}"


def test_fill_economics_has_fee_per_unit_field():
    """FillEconomics must expose fee_per_unit explicitly."""
    econ = compute_economics(entry_price=0.50, metadata=None, fallback_fee_rate=0.02)
    assert hasattr(econ, "fee_per_unit")
    assert econ.fee_per_unit >= 0
