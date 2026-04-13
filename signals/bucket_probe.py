"""
signals/bucket_probe.py — Window bucket classification.

In Phase 1 (measurement-only), bucket labels are recorded alongside
hypothetical entries. No trading decisions are made from buckets yet.
Buckets are simple observable market conditions, not predictive labels.

Bucket dimensions (Phase 1):
  basis_dir:  "UP" (Binance > Chainlink), "DOWN", "NEUTRAL" (within 0.05%)
  spread_cat: "TIGHT" (< 0.01), "NORMAL" (< 0.03), "WIDE" (>= 0.03)
  hour_utc:   0–23 (UTC hour of window start)

Bucket label: "{basis_dir}_{spread_cat}_{hour_utc}"
Example: "UP_TIGHT_14"
"""
from __future__ import annotations

from typing import Optional

from signals.feature_builder import Features


def classify(features: Features) -> str:
    """
    Return a bucket label string for this feature snapshot.
    Returns "UNDEFINED" if critical features are missing.
    """
    basis_dir = _basis_direction(features.basis_pct)
    spread_cat = _spread_category(features)
    hour = _hour_utc(features.window_id)
    return f"{basis_dir}_{spread_cat}_{hour:02d}"


def _basis_direction(basis_pct: Optional[float]) -> str:
    if basis_pct is None:
        return "NOBASIS"
    if basis_pct > 0.05:
        return "UP"
    if basis_pct < -0.05:
        return "DOWN"
    return "NEUTRAL"


def _spread_category(features: Features) -> str:
    # Use average of up and down spread
    spreads = [s for s in (features.up_spread, features.dn_spread) if s is not None]
    if not spreads:
        return "NOSPREAD"
    avg = sum(spreads) / len(spreads)
    if avg < 0.01:
        return "TIGHT"
    if avg < 0.03:
        return "NORMAL"
    return "WIDE"


def _hour_utc(window_start: int) -> int:
    if window_start == 0:
        return -1
    import datetime
    dt = datetime.datetime.utcfromtimestamp(window_start)
    return dt.hour
