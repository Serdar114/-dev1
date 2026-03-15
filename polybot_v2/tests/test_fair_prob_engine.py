"""Tests for fair_prob_engine.py"""

import sys
import math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fair_prob_engine import FairProbEngine
from models import FairProbResult


SIGMA_FLOOR = 0.0008
TAU_FLOOR = 0.05
ZSCORE_CLIP = 8.0
PROB_MIN = 0.01
PROB_MAX = 0.99


def make_engine() -> FairProbEngine:
    return FairProbEngine(
        sigma_floor=SIGMA_FLOOR,
        tau_floor=TAU_FLOOR,
        zscore_clip=ZSCORE_CLIP,
        prob_clip_min=PROB_MIN,
        prob_clip_max=PROB_MAX,
    )


class TestInit:
    def test_valid(self):
        e = make_engine()
        assert e.sigma_floor == SIGMA_FLOOR

    def test_invalid_sigma_floor(self):
        with pytest.raises(ValueError):
            FairProbEngine(0.0, TAU_FLOOR, ZSCORE_CLIP, PROB_MIN, PROB_MAX)

    def test_invalid_tau_floor(self):
        with pytest.raises(ValueError):
            FairProbEngine(SIGMA_FLOOR, 0.0, ZSCORE_CLIP, PROB_MIN, PROB_MAX)

    def test_invalid_prob_bounds(self):
        with pytest.raises(ValueError):
            FairProbEngine(SIGMA_FLOOR, TAU_FLOOR, ZSCORE_CLIP, 0.6, 0.5)


class TestFormula:
    def setup_method(self):
        self.engine = make_engine()

    def test_at_window_open_fair_yes_near_half(self):
        """When btc_mid == window_open, delta_pct=0, z=0, fair_yes≈0.5"""
        result = self.engine.compute(
            btc_mid=50000.0,
            window_open=50000.0,
            seconds_to_expiry=150.0,
            realized_vol_60s=0.001,
        )
        assert result.delta_pct == pytest.approx(0.0, abs=1e-12)
        assert result.z_score == pytest.approx(0.0, abs=1e-12)
        assert result.fair_yes_prob == pytest.approx(0.5, abs=0.001)
        assert result.fair_no_prob == pytest.approx(0.5, abs=0.001)
        assert result.fair_yes_prob + result.fair_no_prob == pytest.approx(1.0, abs=1e-9)

    def test_positive_move_yields_higher_yes(self):
        """BTC moved up 1% → YES more likely"""
        result = self.engine.compute(
            btc_mid=50500.0,
            window_open=50000.0,
            seconds_to_expiry=150.0,
            realized_vol_60s=0.001,
        )
        assert result.delta_pct == pytest.approx(0.01, rel=1e-6)
        assert result.z_score > 0
        assert result.fair_yes_prob > 0.5

    def test_negative_move_yields_lower_yes(self):
        """BTC moved down 1% → YES less likely"""
        result = self.engine.compute(
            btc_mid=49500.0,
            window_open=50000.0,
            seconds_to_expiry=150.0,
            realized_vol_60s=0.001,
        )
        assert result.delta_pct == pytest.approx(-0.01, rel=1e-6)
        assert result.z_score < 0
        assert result.fair_yes_prob < 0.5

    def test_sigma_floor_applied(self):
        """When realized_vol is below floor, sigma_eff = sigma_floor"""
        result = self.engine.compute(
            btc_mid=50100.0,
            window_open=50000.0,
            seconds_to_expiry=150.0,
            realized_vol_60s=0.00001,  # below floor
        )
        assert result.sigma_eff == pytest.approx(SIGMA_FLOOR, rel=1e-9)

    def test_tau_floor_applied(self):
        """When seconds_to_expiry is tiny, tau_eff = tau_floor"""
        result = self.engine.compute(
            btc_mid=50100.0,
            window_open=50000.0,
            seconds_to_expiry=0.1,  # nearly expired
            realized_vol_60s=0.001,
        )
        assert result.tau_eff == pytest.approx(TAU_FLOOR, rel=1e-9)

    def test_zscore_clamped_positive(self):
        """Extreme up move → z clamped to +zscore_clip"""
        result = self.engine.compute(
            btc_mid=60000.0,     # 20% up move
            window_open=50000.0,
            seconds_to_expiry=290.0,
            realized_vol_60s=SIGMA_FLOOR,  # minimal vol
        )
        assert result.z_score == pytest.approx(ZSCORE_CLIP, abs=0.001)

    def test_zscore_clamped_negative(self):
        """Extreme down move → z clamped to -zscore_clip"""
        result = self.engine.compute(
            btc_mid=40000.0,     # 20% down move
            window_open=50000.0,
            seconds_to_expiry=290.0,
            realized_vol_60s=SIGMA_FLOOR,
        )
        assert result.z_score == pytest.approx(-ZSCORE_CLIP, abs=0.001)

    def test_prob_clip_min(self):
        """Very extreme down → clipped to prob_clip_min"""
        result = self.engine.compute(
            btc_mid=40000.0,
            window_open=50000.0,
            seconds_to_expiry=290.0,
            realized_vol_60s=SIGMA_FLOOR,
        )
        assert result.fair_yes_prob >= PROB_MIN
        assert result.fair_no_prob <= PROB_MAX

    def test_prob_clip_max(self):
        """Very extreme up → clipped to prob_clip_max"""
        result = self.engine.compute(
            btc_mid=60000.0,
            window_open=50000.0,
            seconds_to_expiry=290.0,
            realized_vol_60s=SIGMA_FLOOR,
        )
        assert result.fair_yes_prob <= PROB_MAX
        assert result.fair_no_prob >= PROB_MIN

    def test_prob_sum_to_one(self):
        """fair_yes + fair_no == 1.0 always"""
        for delta in [-0.05, -0.01, 0.0, 0.01, 0.05]:
            result = self.engine.compute(
                btc_mid=50000.0 * (1 + delta),
                window_open=50000.0,
                seconds_to_expiry=100.0,
                realized_vol_60s=0.001,
            )
            assert result.fair_yes_prob + result.fair_no_prob == pytest.approx(1.0, abs=1e-10)

    def test_deterministic(self):
        """Same inputs → same outputs"""
        args = dict(
            btc_mid=50200.0,
            window_open=50000.0,
            seconds_to_expiry=180.0,
            realized_vol_60s=0.0012,
        )
        r1 = self.engine.compute(**args)
        r2 = self.engine.compute(**args)
        assert r1.fair_yes_prob == r2.fair_yes_prob
        assert r1.z_score == r2.z_score

    def test_tau_scaling(self):
        """More time remaining → smaller z (same delta_pct)"""
        r_early = self.engine.compute(50100.0, 50000.0, 290.0, 0.001)
        r_late = self.engine.compute(50100.0, 50000.0, 10.0, 0.001)
        # more time remaining → larger tau → smaller z → probability closer to 0.5
        assert abs(r_early.z_score) < abs(r_late.z_score)

    def test_invalid_window_open(self):
        with pytest.raises(ValueError):
            self.engine.compute(50000.0, 0.0, 150.0, 0.001)

    def test_invalid_btc_mid(self):
        with pytest.raises(ValueError):
            self.engine.compute(0.0, 50000.0, 150.0, 0.001)
