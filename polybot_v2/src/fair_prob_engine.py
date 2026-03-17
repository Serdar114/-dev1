"""
Fair probability engine for polybot_v2.

Formula:
    delta_pct = (btc_mid - window_open) / window_open
    tau_eff   = max(seconds_to_expiry / 300.0, tau_floor)
    sigma_eff = max(realized_vol_60s, sigma_floor)
    z         = delta_pct / (sigma_eff * sqrt(tau_eff))
    z         = clip(z, -zscore_clip, +zscore_clip)
    fair_yes  = Phi(z)          # standard normal CDF
    fair_yes  = clip(fair_yes, prob_clip_min, prob_clip_max)
    fair_no   = 1 - fair_yes

Interpretation:
  - YES token pays 1 if BTC ends higher than the window open.
  - z measures how many sigma-units the current drift is above zero.
  - Positive z → market already moved up → YES more likely.
"""

from __future__ import annotations

import math
from scipy.stats import norm  # type: ignore

from models import FairProbResult


class FairProbEngine:
    def __init__(
        self,
        sigma_floor: float,
        tau_floor: float,
        zscore_clip: float,
        prob_clip_min: float,
        prob_clip_max: float,
    ) -> None:
        if sigma_floor <= 0:
            raise ValueError("sigma_floor must be > 0")
        if tau_floor <= 0:
            raise ValueError("tau_floor must be > 0")
        if zscore_clip <= 0:
            raise ValueError("zscore_clip must be > 0")
        if not (0 < prob_clip_min < prob_clip_max < 1):
            raise ValueError("prob_clip bounds must satisfy 0 < min < max < 1")

        self.sigma_floor = sigma_floor
        self.tau_floor = tau_floor
        self.zscore_clip = zscore_clip
        self.prob_clip_min = prob_clip_min
        self.prob_clip_max = prob_clip_max

    def compute(
        self,
        btc_mid: float,
        window_open: float,
        seconds_to_expiry: float,
        realized_vol_60s: float,
    ) -> FairProbResult:
        """
        Compute fair YES/NO probability given current market state.

        Args:
            btc_mid: current BTC mid-price from Binance
            window_open: BTC price at the start of this 5m window
            seconds_to_expiry: seconds until market resolves
            realized_vol_60s: realised per-tick std of log returns (60s)

        Returns:
            FairProbResult with probabilities and debug fields
        """
        if window_open <= 0:
            raise ValueError(f"window_open must be positive, got {window_open}")
        if btc_mid <= 0:
            raise ValueError(f"btc_mid must be positive, got {btc_mid}")

        delta_pct = (btc_mid - window_open) / window_open

        tau_eff = max(seconds_to_expiry / 300.0, self.tau_floor)
        sigma_eff = max(realized_vol_60s, self.sigma_floor)

        denominator = sigma_eff * math.sqrt(tau_eff)
        # denominator is always > 0 because sigma_eff >= sigma_floor > 0
        z_raw = delta_pct / denominator
        z = max(-self.zscore_clip, min(self.zscore_clip, z_raw))

        fair_yes = float(norm.cdf(z))
        fair_yes = max(self.prob_clip_min, min(self.prob_clip_max, fair_yes))
        fair_no = 1.0 - fair_yes

        return FairProbResult(
            fair_yes_prob=fair_yes,
            fair_no_prob=fair_no,
            delta_pct=delta_pct,
            sigma_eff=sigma_eff,
            tau_eff=tau_eff,
            z_score=z,
            delta_pct_display=delta_pct * 100.0,
        )
