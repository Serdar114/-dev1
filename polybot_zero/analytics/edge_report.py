"""
edge_report.py — After-fee edge evaluation and evidence table.

Design:
  Produces a sortable, auditable edge table from bucket statistics.
  Flags false-positive clusters explicitly.
  Requires minimum observations before any edge claim.
  Does NOT claim edge without evidence.
  Output is designed for ChatGPT review — explicit provenance, no narrative dressing.
"""

from __future__ import annotations
import json
import logging
from typing import Dict, List, Optional
from dataclasses import asdict

from loggingx.schemas import BucketStats

logger = logging.getLogger("polybot.edge_report")


class EdgeReport:
    """
    Produces a structured edge analysis from accumulated bucket stats.

    Job: turn BucketStats into a ranked, annotated table.
    Input: Dict[bucket_id, BucketStats]
    Output: list of report rows (dicts), sorted by avg_net_pnl descending
    """

    def __init__(self, min_obs_for_report: int = 5):
        self._min_obs = min_obs_for_report

    def generate(self, all_stats: Dict[str, BucketStats]) -> List[dict]:
        """
        Generate edge report rows.
        Only includes buckets with >= min_obs observations.
        Sorted by avg_net_pnl descending.
        """
        rows = []
        for bucket_id, stats in all_stats.items():
            if stats.n_observations < self._min_obs:
                continue

            row = {
                "bucket_id":       bucket_id,
                "n_observations":  stats.n_observations,
                "n_no_trade":      stats.n_no_trade,
                "n_resolved":      stats.n_observations - stats.n_no_trade,
                "n_up_correct":    stats.n_up_correct,
                "n_down_correct":  stats.n_down_correct,
                "win_rate":        round(stats.win_rate, 4),
                "total_net_pnl":   round(stats.total_net_pnl, 4),
                "avg_net_pnl":     round(stats.avg_net_pnl, 4),
                "has_edge":        stats.has_edge,
                "edge_confidence": stats.edge_confidence,
                "false_positive_flags": self._flag_false_positives(stats),
            }
            rows.append(row)

        rows.sort(key=lambda r: r["avg_net_pnl"], reverse=True)
        return rows

    def _flag_false_positives(self, stats: BucketStats) -> List[str]:
        """
        Identify patterns that indicate false-positive edge claims.
        Returns list of flag strings (empty = no red flags).
        """
        flags = []

        # Too few observations
        if stats.n_observations < 20:
            flags.append("LOW_OBS_EDGE_UNRELIABLE")

        # High win rate but negative PnL (pricing mismatch)
        if stats.win_rate > 0.55 and stats.avg_net_pnl < 0:
            flags.append("HIGH_WIN_RATE_NEGATIVE_PNL")

        # Positive PnL but very low win rate (outlier-driven)
        if stats.avg_net_pnl > 0 and stats.win_rate < 0.45:
            flags.append("LOW_WIN_RATE_POSITIVE_PNL_SUSPICIOUS")

        # All no-trades (no real measurement)
        if stats.n_no_trade >= stats.n_observations:
            flags.append("ALL_NO_TRADE_NO_MEASUREMENT")

        return flags

    def print_table(self, rows: List[dict]) -> str:
        """Render as text table for logs/ChatGPT review."""
        if not rows:
            return "No buckets with sufficient observations.\n"

        lines = [
            f"{'BUCKET':<45} {'N':>5} {'WIN%':>6} {'AVG_NET':>8} {'EDGE':>5} {'FLAGS'}",
            "-" * 100,
        ]
        for r in rows:
            flags = ",".join(r["false_positive_flags"]) if r["false_positive_flags"] else "-"
            lines.append(
                f"{r['bucket_id']:<45} {r['n_observations']:>5} "
                f"{r['win_rate']*100:>5.1f}% {r['avg_net_pnl']:>8.4f} "
                f"{'YES' if r['has_edge'] else 'NO':>5} {flags}"
            )
        return "\n".join(lines)

    def to_json(self, rows: List[dict]) -> str:
        return json.dumps(rows, indent=2)
