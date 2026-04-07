"""
bucket_probe.py — Assigns state vectors to discrete feature buckets.

Design:
  A "bucket" is a discretized region of feature space.
  Bucket assignment is deterministic given the feature vector.
  Buckets are narrow — we prefer sparse high-quality evidence over broad noise.
  Bucket ID is a string encoding the discrete feature values.
  In phase 1 (measurement-only), we observe bucket distributions.
  In phase 2, only buckets with sufficient evidence are eligible for paper trades.

  Discretization scheme (explicit, not hidden):
    chainlink_delta_bps: [-inf,-20), [-20,-5), [-5,5), [5,20), [20,+inf)
      → labels: "strong_down", "mild_down", "flat", "mild_up", "strong_up"
    secs_to_expiry: [0,60), [60,120), [120,180), [180,240), [240,300)
      → labels: "t0", "t1", "t2", "t3", "t4"
    pair_sum bucket: <1.0, [1.0,1.05), [1.05,1.10), >=1.10
      → labels: "low", "normal", "elevated", "high"

  These are illustrative starting bins. They will be refined based on data.
  DO NOT over-engineer this. The first job is measurement, not optimization.
"""

from __future__ import annotations
import logging
from typing import Optional

from loggingx.schemas import FeatureVector

logger = logging.getLogger("polybot.bucket_probe")


def discretize_delta_bps(delta_bps: Optional[float]) -> str:
    """
    Discretize chainlink_delta_bps into a movement label.
    None → "unknown"
    """
    if delta_bps is None:
        return "unknown"
    if delta_bps < -20.0:
        return "strong_down"
    if delta_bps < -5.0:
        return "mild_down"
    if delta_bps <= 5.0:
        return "flat"
    if delta_bps <= 20.0:
        return "mild_up"
    return "strong_up"


def discretize_secs_to_expiry(secs: Optional[float]) -> str:
    """
    Discretize time-to-expiry into a timing label.
    None → "unknown"
    """
    if secs is None:
        return "unknown"
    if secs < 60.0:
        return "t0"     # very late
    if secs < 120.0:
        return "t1"
    if secs < 180.0:
        return "t2"
    if secs < 240.0:
        return "t3"
    return "t4"          # early in window


def discretize_pair_sum(pair_sum: Optional[float]) -> str:
    """
    Discretize pair_sum (up_ask + down_ask) into a spread label.
    None → "unknown"
    """
    if pair_sum is None:
        return "unknown"
    if pair_sum < 1.00:
        return "low"
    if pair_sum < 1.05:
        return "normal"
    if pair_sum < 1.10:
        return "elevated"
    return "high"


def assign_bucket(fv: FeatureVector) -> str:
    """
    Assign a bucket ID to a feature vector.
    Bucket ID is a deterministic string encoding of discrete feature values.
    Format: "d:{delta}|t:{timing}|s:{sum}"

    This is the primary bucket used for measurement aggregation.
    Additional dimensions may be added in later phases.
    """
    delta_label   = discretize_delta_bps(fv.chainlink_delta_bps)
    timing_label  = discretize_secs_to_expiry(fv.secs_to_expiry)
    sum_label     = discretize_pair_sum(fv.pair_sum_best_ask)

    return f"d:{delta_label}|t:{timing_label}|s:{sum_label}"


def bucket_features(fv: FeatureVector) -> dict:
    """
    Return the feature values used to assign the bucket.
    Useful for logging and debugging bucket membership.
    """
    return {
        "chainlink_delta_bps":       fv.chainlink_delta_bps,
        "delta_label":               discretize_delta_bps(fv.chainlink_delta_bps),
        "secs_to_expiry":            fv.secs_to_expiry,
        "timing_label":              discretize_secs_to_expiry(fv.secs_to_expiry),
        "pair_sum_best_ask":         fv.pair_sum_best_ask,
        "pair_sum_label":            discretize_pair_sum(fv.pair_sum_best_ask),
    }
