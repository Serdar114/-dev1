"""
tests/test_fee_schedule.py — Unit tests for fee economics.

Tests that:
  - Canonical fee is used when available.
  - Fallback fee is used when metadata is None or fee is missing.
  - Provenance is correctly labelled in all cases.
  - net_payoff formula is correct.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from metadata.fee_schedule import compute_economics
from state import MarketMetadata


def test_canonical_fee_used_when_available():
    meta = MarketMetadata(
        condition_id="0x1",
        tick_size=0.001,
        min_order_size=5.0,
        taker_fee_rate=0.02,
        fee_provenance="canonical",
    )
    econ = compute_economics(entry_price=0.50, metadata=meta, fallback_fee_rate=0.99)
    assert econ.fee_rate == 0.02
    assert econ.fee_provenance == "canonical"


def test_fallback_fee_used_when_metadata_none():
    econ = compute_economics(entry_price=0.50, metadata=None, fallback_fee_rate=0.02)
    assert econ.fee_rate == 0.02
    assert econ.fee_provenance == "config_default"


def test_fallback_fee_used_when_fee_missing_in_metadata():
    meta = MarketMetadata(
        condition_id="0x2",
        tick_size=0.001,
        min_order_size=5.0,
        taker_fee_rate=None,
        fee_provenance="missing",
    )
    econ = compute_economics(entry_price=0.50, metadata=meta, fallback_fee_rate=0.02)
    assert econ.fee_rate == 0.02
    assert econ.fee_provenance == "missing"


def test_net_payoff_formula_canonical():
    """net_payoff_if_win = 1.0 - price - fee_rate."""
    meta = MarketMetadata(
        condition_id="0x3",
        taker_fee_rate=0.02,
        fee_provenance="canonical",
    )
    econ = compute_economics(entry_price=0.50, metadata=meta, fallback_fee_rate=0.0)
    assert abs(econ.net_payoff_if_win - (1.0 - 0.50 - 0.02)) < 1e-9
    assert abs(econ.net_payoff_if_lose - (-0.50)) < 1e-9


def test_net_payoff_low_entry_price():
    """Entry at 0.10 with 2% fee."""
    meta = MarketMetadata(taker_fee_rate=0.02, fee_provenance="canonical")
    econ = compute_economics(entry_price=0.10, metadata=meta, fallback_fee_rate=0.0)
    assert abs(econ.net_payoff_if_win - 0.88) < 1e-9
    assert abs(econ.net_payoff_if_lose - (-0.10)) < 1e-9


def test_net_payoff_high_entry_price():
    """Entry at 0.99 with 2% fee => win payoff is negative (bad trade: 1-0.99-0.02=-0.01)."""
    meta = MarketMetadata(taker_fee_rate=0.02, fee_provenance="canonical")
    econ = compute_economics(entry_price=0.99, metadata=meta, fallback_fee_rate=0.0)
    assert econ.net_payoff_if_win < 0


def test_provenance_config_default_not_canonical():
    """config_default provenance is not canonical."""
    econ = compute_economics(entry_price=0.50, metadata=None, fallback_fee_rate=0.02)
    assert econ.fee_provenance != "canonical"
