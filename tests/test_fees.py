"""
tests/test_fees.py

Explicit tests for the corrected taker fee formula:
    fee_per_share = C * p * 0.25 * (p * (1 - p))^2

Verifies:
  - Exact numeric outputs for p=0.50 and p=0.85 (C=0.02)
  - New formula outputs differ from old formula (p term was missing)
  - Formula is monotone in the expected direction
  - break_even_win_rate accounts for the fee correctly
  - TakerFeeResult.formula_str contains the new formula tokens
"""
import math
import pytest
from execution.fees import compute_taker_fee, break_even_win_rate, DEFAULT_TAKER_C


# ---------------------------------------------------------------------------
# Reference values (computed by hand from corrected formula)
# ---------------------------------------------------------------------------

def _expected_fee(p: float, C: float = DEFAULT_TAKER_C) -> float:
    """Reference implementation of corrected formula."""
    inner = p * (1.0 - p)
    return C * p * 0.25 * (inner ** 2)


# ---------------------------------------------------------------------------
# Explicit numeric correctness
# ---------------------------------------------------------------------------

class TestFeeFormulaCorrectness:
    def test_p050_fee_per_share_exact(self):
        """
        p=0.50, C=0.02:
          inner = 0.50 * 0.50 = 0.25
          fee = 0.02 * 0.50 * 0.25 * (0.25)^2
              = 0.02 * 0.50 * 0.25 * 0.0625
              = 0.000156250
        """
        result = compute_taker_fee(0.50, 1, C=0.02)
        assert result.fee_per_share == pytest.approx(0.00015625, rel=1e-6)

    def test_p050_total_fee_five_shares(self):
        result = compute_taker_fee(0.50, 5, C=0.02)
        assert result.total_fee == pytest.approx(0.00015625 * 5, rel=1e-6)

    def test_p085_fee_per_share_exact(self):
        """
        p=0.85, C=0.02:
          inner = 0.85 * 0.15 = 0.1275
          fee = 0.02 * 0.85 * 0.25 * (0.1275)^2
              = 0.02 * 0.85 * 0.25 * 0.016256
              = 0.02 * 0.85 * 0.004064
              = 0.02 * 0.003454
              ≈ 0.00006909
        """
        expected = _expected_fee(0.85)
        result = compute_taker_fee(0.85, 1, C=0.02)
        assert result.fee_per_share == pytest.approx(expected, rel=1e-6)

    def test_p085_total_fee_five_shares(self):
        expected_total = _expected_fee(0.85) * 5
        result = compute_taker_fee(0.85, 5, C=0.02)
        assert result.total_fee == pytest.approx(expected_total, rel=1e-6)

    def test_p087_fee_per_share(self):
        """Canary: the trade-zone typical price p=0.87."""
        expected = _expected_fee(0.87)
        result = compute_taker_fee(0.87, 1, C=0.02)
        assert result.fee_per_share == pytest.approx(expected, rel=1e-6)


# ---------------------------------------------------------------------------
# Verify corrected formula differs from old (missing-p) formula
# ---------------------------------------------------------------------------

class TestFeeFormulaIsNotOldFormula:
    """
    Old (wrong) formula: fee = C * 0.25 * (p*(1-p))^2
    New (correct) formula: fee = C * p * 0.25 * (p*(1-p))^2

    At any p in (0, 1), the new formula is strictly smaller (multiplied by p < 1).
    """

    def _old_fee(self, p: float, C: float = 0.02) -> float:
        """Reference OLD formula — should NOT match the module output."""
        inner = p * (1.0 - p)
        return C * 0.25 * (inner ** 2)

    def test_p050_new_is_less_than_old(self):
        new_fee = compute_taker_fee(0.50, 1).fee_per_share
        old_fee = self._old_fee(0.50)
        # old = 0.02 * 0.25 * (0.25)^2 = 0.0003125
        # new = 0.50 * old = 0.00015625
        assert new_fee == pytest.approx(old_fee * 0.50, rel=1e-6)
        assert new_fee < old_fee

    def test_p085_new_is_less_than_old(self):
        new_fee = compute_taker_fee(0.85, 1).fee_per_share
        old_fee = self._old_fee(0.85)
        assert new_fee == pytest.approx(old_fee * 0.85, rel=1e-6)
        assert new_fee < old_fee

    def test_p050_old_formula_is_not_current_output(self):
        old_fee = self._old_fee(0.50)
        current = compute_taker_fee(0.50, 1).fee_per_share
        assert current != pytest.approx(old_fee), (
            "Module appears to be using the OLD formula (missing leading p term). "
            f"old={old_fee:.8f} current={current:.8f}"
        )

    def test_p085_old_formula_is_not_current_output(self):
        old_fee = self._old_fee(0.85)
        current = compute_taker_fee(0.85, 1).fee_per_share
        assert current != pytest.approx(old_fee), (
            "Module appears to be using the OLD formula (missing leading p term). "
            f"old={old_fee:.8f} current={current:.8f}"
        )


# ---------------------------------------------------------------------------
# Formula structure and audit trail
# ---------------------------------------------------------------------------

class TestFeeAuditTrail:
    def test_formula_str_contains_leading_p(self):
        """formula_str must show the leading p term for audit visibility."""
        result = compute_taker_fee(0.87, 5)
        # New formula_str format: "fee = C * p * 0.25 * ..."
        assert "0.87" in result.formula_str, (
            "formula_str must include the price p for audit. Got: " + result.formula_str
        )

    def test_result_attributes_populated(self):
        result = compute_taker_fee(0.87, 5)
        assert result.price == 0.87
        assert result.shares == 5
        assert result.C == DEFAULT_TAKER_C
        assert result.fee_per_share > 0
        assert result.total_fee == pytest.approx(result.fee_per_share * 5)

    def test_fee_monotone_in_shares(self):
        """total_fee scales linearly with shares."""
        r1 = compute_taker_fee(0.87, 1)
        r5 = compute_taker_fee(0.87, 5)
        assert r5.total_fee == pytest.approx(r1.total_fee * 5, rel=1e-9)

    def test_fee_zero_at_boundary_approached(self):
        """Fee should approach 0 as p → 0 or p → 1 (inner → 0)."""
        r_low = compute_taker_fee(0.01, 1)
        r_high = compute_taker_fee(0.99, 1)
        r_mid = compute_taker_fee(0.50, 1)
        assert r_low.fee_per_share < r_mid.fee_per_share
        assert r_high.fee_per_share < r_mid.fee_per_share

    def test_invalid_price_raises(self):
        with pytest.raises(ValueError):
            compute_taker_fee(0.0, 5)
        with pytest.raises(ValueError):
            compute_taker_fee(1.0, 5)
        with pytest.raises(ValueError):
            compute_taker_fee(1.5, 5)

    def test_invalid_shares_raises(self):
        with pytest.raises(ValueError):
            compute_taker_fee(0.87, 0)


# ---------------------------------------------------------------------------
# Break-even win rate reflects new fee
# ---------------------------------------------------------------------------

class TestBreakEvenWithCorrectedFee:
    def test_break_even_wr_p050(self):
        """break_even_wr = price + fee_per_share."""
        p = 0.50
        fee_ps = compute_taker_fee(p, 1).fee_per_share
        expected_be = p + fee_ps
        assert break_even_win_rate(p, fee_ps) == pytest.approx(expected_be)

    def test_break_even_wr_p085(self):
        p = 0.85
        fee_ps = compute_taker_fee(p, 1).fee_per_share
        expected_be = p + fee_ps
        assert break_even_win_rate(p, fee_ps) == pytest.approx(expected_be)

    def test_break_even_wr_is_above_price(self):
        """Taker always needs WR > price due to non-zero fee."""
        p = 0.87
        fee_ps = compute_taker_fee(p, 1).fee_per_share
        be = break_even_win_rate(p, fee_ps)
        assert be > p

    def test_break_even_wr_maker_equals_price(self):
        """Maker fee=0 → break_even_wr == price."""
        p = 0.87
        assert break_even_win_rate(p, 0.0) == pytest.approx(p)
