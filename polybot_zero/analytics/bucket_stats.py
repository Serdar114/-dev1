"""
bucket_stats.py — Aggregate hypothetical measurement results by bucket.

Design:
  Accumulates HypotheticalEntry records grouped by bucket_id.
  After resolution, updates each bucket's win counts and PnL.
  Provides per-bucket statistics for edge report.
  UNRESOLVED outcomes are counted but excluded from win rate.
  No edge is claimed without minimum observations.
"""

from __future__ import annotations
import logging
from collections import defaultdict
from typing import Dict, List, Optional

from loggingx.schemas import HypotheticalEntry, BucketStats, ResolutionOutcome

logger = logging.getLogger("polybot.bucket_stats")


class BucketAccumulator:
    """
    Accumulates and summarizes hypothetical entries by bucket.

    Job: maintain running stats per bucket for later edge analysis.
    Input: HypotheticalEntry records (after resolution)
    Output: Dict[bucket_id, BucketStats]
    """

    def __init__(self, min_observations: int = 5):
        self._min_obs = min_observations
        # Per-bucket: list of resolved HypotheticalEntry
        self._entries: Dict[str, List[HypotheticalEntry]] = defaultdict(list)

    def record(self, entry: HypotheticalEntry) -> None:
        """
        Record a resolved hypothetical entry.
        Only call after entry.outcome_known is True (or explicitly UNRESOLVED).
        """
        bucket_id = entry.bucket_id or "no_bucket"
        self._entries[bucket_id].append(entry)

    def compute_stats(self, bucket_id: str, net_edge_threshold: float = 0.02) -> BucketStats:
        """
        Compute stats for one bucket from accumulated entries.
        Returns BucketStats with edge flag.
        """
        entries = self._entries.get(bucket_id, [])
        stats = BucketStats(bucket_id=bucket_id)
        stats.n_observations = len(entries)

        if stats.n_observations < self._min_obs:
            stats.edge_confidence = "INSUFFICIENT_DATA"
            return stats

        resolved = [e for e in entries if e.outcome_known and e.actual_outcome != ResolutionOutcome.UNRESOLVED]
        stats.n_no_trade = sum(1 for e in entries if e.no_trade_reason is not None)

        if not resolved:
            stats.edge_confidence = "ALL_UNRESOLVED"
            return stats

        # Count correct UP and DOWN hypotheticals
        up_entries   = [e for e in resolved if e.side == "UP"]
        down_entries = [e for e in resolved if e.side == "DOWN"]

        stats.n_up_correct   = sum(1 for e in up_entries if e.hypothetical_correct)
        stats.n_down_correct = sum(1 for e in down_entries if e.hypothetical_correct)

        # PnL from resolved entries (both sides)
        pnls = [e.net_pnl_usdc for e in resolved if e.net_pnl_usdc is not None]
        if pnls:
            stats.total_net_pnl = sum(pnls)
            stats.avg_net_pnl   = sum(pnls) / len(pnls)

        # Win rate: correct out of resolved (one entry per side per window, pick best)
        n_correct = stats.n_up_correct + stats.n_down_correct
        n_total   = len(resolved)
        stats.win_rate = n_correct / n_total if n_total > 0 else 0.0

        # Edge: positive avg net PnL above threshold
        stats.has_edge = stats.avg_net_pnl >= net_edge_threshold
        stats.edge_confidence = "MEASURED" if stats.n_observations >= self._min_obs else "INSUFFICIENT_DATA"

        return stats

    def all_stats(self, net_edge_threshold: float = 0.02) -> Dict[str, BucketStats]:
        return {
            bid: self.compute_stats(bid, net_edge_threshold)
            for bid in self._entries
        }

    def proven_buckets(self, min_obs: Optional[int] = None, net_edge_threshold: float = 0.02) -> set:
        """
        Return bucket IDs that have sufficient evidence and positive net edge.
        These are eligible for selective paper mode.
        """
        required_obs = min_obs or self._min_obs
        proven = set()
        for bid, stats in self.all_stats(net_edge_threshold).items():
            if (
                stats.n_observations >= required_obs
                and stats.has_edge
                and stats.edge_confidence == "MEASURED"
            ):
                proven.add(bid)
        return proven

    def bucket_ids(self) -> List[str]:
        return list(self._entries.keys())

    def entry_count(self, bucket_id: str) -> int:
        return len(self._entries.get(bucket_id, []))
