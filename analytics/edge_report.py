"""
analytics/edge_report.py — Edge analysis from bucket stats.

Reads bucket stats and ranks buckets by hypothetical edge.
Used to decide which buckets to trade in Phase 2 (paper mode).
"""
from __future__ import annotations

from typing import Dict, List

from analytics.bucket_stats import BucketRecord, compute_bucket_stats


def generate_report(log_path: str, min_samples: int = 10) -> List[dict]:
    """
    Return list of bucket summaries sorted by hypothetical edge (avg net payoff).
    Only buckets with >= min_samples resolved windows are included.
    """
    stats = compute_bucket_stats(log_path)
    rows = []
    for bucket, rec in stats.items():
        if rec.resolved_count < min_samples:
            continue
        avg = rec.avg_net_payoff_up()
        if avg is None:
            continue
        rows.append({
            "bucket": bucket,
            "resolved": rec.resolved_count,
            "win_rate": rec.win_rate(),
            "avg_net_payoff_up": avg,
            "hypothetical_entries": rec.hypothetical_entries,
        })
    rows.sort(key=lambda r: r["avg_net_payoff_up"], reverse=True)
    return rows


def print_report(log_path: str, min_samples: int = 10) -> None:
    rows = generate_report(log_path, min_samples)
    if not rows:
        print(f"No buckets with >= {min_samples} resolved windows.")
        return
    print(f"{'Bucket':<30} {'Resolved':>8} {'WinRate':>8} {'AvgNetUp':>10}")
    print("-" * 60)
    for r in rows:
        wr = f"{r['win_rate']:.3f}" if r["win_rate"] is not None else "--"
        an = f"{r['avg_net_payoff_up']:+.4f}"
        print(f"{r['bucket']:<30} {r['resolved']:>8} {wr:>8} {an:>10}")
